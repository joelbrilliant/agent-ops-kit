"""Issue discovery for maintainer-labelled open issues."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from agent_ops.config import Config, IssueAutomationConfig
from agent_ops.contracts import IssueSignalV1, body_digest
from agent_ops.github.client import GitHubClient, GitHubError


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class IssueSkip:
    repository: str
    issue_number: int
    reason: str
    issue_node_id: str = ""


@dataclass
class IssueDiscoveryResult:
    signals: List[IssueSignalV1]
    skips: List[IssueSkip]
    inspected_issues: int


ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    isPrivate
    url
    issue(number: $number) {
      id
      number
      url
      title
      body
      state
      updatedAt
      author { login }
      labels(first: 100) {
        nodes { name }
        pageInfo { hasNextPage }
      }
      comments(first: 100) {
        nodes {
          id
          body
          createdAt
          author { login }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ISSUE_COMMENTS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 100, after: $after) {
        nodes {
          id
          body
          createdAt
          author { login }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

REF_OID_QUERY = """
query($owner: String!, $name: String!, $qualifiedName: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $qualifiedName) {
      target {
        oid
      }
    }
  }
}
"""


def conversation_digest(title: str, body: str, comments: Sequence[Dict[str, Any]]) -> str:
    parts = [title or "", body or ""]
    for comment in comments:
        cid = str(comment.get("id") or "")
        cbody = str(comment.get("body") or "")
        parts.append(f"{cid}|{cbody}")
    return body_digest("\n".join(parts))


def _search_query(repository: str, require_labels: Sequence[str]) -> str:
    # Quote labels so multi-token labels stay exact. Repo is owner/name from config.
    parts = [f"is:issue is:open repo:{repository}"]
    for label in require_labels:
        parts.append(f'label:"{label}"')
    return " ".join(parts)


def _paginate_issue_search(client: GitHubClient, query: str) -> List[Dict[str, Any]]:
    page = 1
    per_page = 100
    items: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    expected_total: Optional[int] = None
    while True:
        payload = client.rest_search_issues(query, page=page, per_page=per_page)
        if not isinstance(payload, dict):
            raise GitHubError("issue search returned non-object payload")
        incomplete = payload.get("incomplete_results")
        if incomplete is not False:
            raise GitHubError("issue search pagination completeness unproved")
        total_count = payload.get("total_count")
        if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            raise GitHubError("issue search total_count invalid")
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise GitHubError("issue search total_count changed during pagination")
        batch = payload.get("items")
        if not isinstance(batch, list):
            raise GitHubError("issue search missing items array")
        if len(batch) > per_page:
            raise GitHubError("issue search page exceeded per_page")
        for item in batch:
            if not isinstance(item, dict):
                raise GitHubError("issue search item is not an object")
            key = str(item.get("node_id") or item.get("id") or "")
            if not key:
                raise GitHubError("issue search item missing stable identity")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            items.append(item)
        if len(items) == expected_total:
            break
        if len(items) > expected_total or len(batch) < per_page:
            raise GitHubError("issue search pagination ended before total_count")
        page += 1
        if page > 50:
            raise GitHubError("issue search exceeded page safety limit")
    if expected_total is None or len(items) != expected_total:
        raise GitHubError("issue search pagination count mismatch")
    return items


def _owner_name(repository: str) -> Tuple[str, str]:
    owner, name = repository.split("/", 1)
    return owner, name


def fetch_base_sha(client: GitHubClient, repository: str, base_ref: str) -> str:
    owner, name = _owner_name(repository)
    qualified = base_ref if base_ref.startswith("refs/") else f"refs/heads/{base_ref}"
    data = client.graphql(
        REF_OID_QUERY,
        {"owner": owner, "name": name, "qualifiedName": qualified},
    )
    ref = (((data or {}).get("data") or {}).get("repository") or {}).get("ref")
    if not isinstance(ref, dict):
        raise GitHubError(f"base ref not found: {repository}@{base_ref}")
    oid = ((ref.get("target") or {}).get("oid")) if isinstance(ref.get("target"), dict) else None
    if not isinstance(oid, str) or not _SHA_RE.fullmatch(oid.lower()):
        raise GitHubError(f"base ref oid missing: {repository}@{base_ref}")
    return oid.lower()


def _fetch_all_comments(
    client: GitHubClient,
    *,
    owner: str,
    name: str,
    number: int,
    first_page: Dict[str, Any],
) -> List[Dict[str, Any]]:
    first_nodes = first_page.get("nodes")
    page_info = first_page.get("pageInfo")
    if not isinstance(first_nodes, list) or not isinstance(page_info, dict):
        raise GitHubError("issue comments first page malformed")
    if not isinstance(page_info.get("hasNextPage"), bool):
        raise GitHubError("issue comments pagination completeness unproved")
    nodes: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for node in first_nodes:
        if not isinstance(node, dict) or not str(node.get("id") or ""):
            raise GitHubError("issue comment node malformed")
        comment_id = str(node["id"])
        if comment_id in seen_ids:
            raise GitHubError("duplicate issue comment node")
        seen_ids.add(comment_id)
        nodes.append(node)
    seen_cursors: Set[str] = set()
    if page_info.get("hasNextPage") is True and not page_info.get("endCursor"):
        raise GitHubError("issue comments pagination missing endCursor")
    while page_info.get("hasNextPage"):
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise GitHubError("issue comments pagination incomplete")
        seen_cursors.add(cursor)
        data = client.graphql(
            ISSUE_COMMENTS_PAGE_QUERY,
            {"owner": owner, "name": name, "number": number, "after": cursor},
        )
        issue = (((data or {}).get("data") or {}).get("repository") or {}).get("issue") or {}
        comments = issue.get("comments")
        if not isinstance(comments, dict):
            raise GitHubError("issue comments page missing")
        batch = comments.get("nodes")
        if not isinstance(batch, list):
            raise GitHubError("issue comments page missing nodes")
        for node in batch:
            if not isinstance(node, dict) or not str(node.get("id") or ""):
                raise GitHubError("issue comment node malformed")
            comment_id = str(node["id"])
            if comment_id in seen_ids:
                raise GitHubError("duplicate issue comment node")
            seen_ids.add(comment_id)
            nodes.append(node)
        page_info = comments.get("pageInfo")
        if not isinstance(page_info, dict):
            raise GitHubError("issue comments pagination metadata missing")
        if not isinstance(page_info.get("hasNextPage"), bool):
            raise GitHubError("issue comments pagination completeness unproved")
        if page_info.get("hasNextPage") is True and not page_info.get("endCursor"):
            raise GitHubError("issue comments pagination missing endCursor")
    return nodes


def fetch_issue_snapshot(
    client: GitHubClient,
    *,
    repository: str,
    issue_number: int,
    base_ref: str,
    require_labels: Sequence[str],
    ignore_labels: Sequence[str] = (),
    trigger_label: str = "",  # backward compat
) -> Tuple[Optional[IssueSignalV1], Optional[IssueSkip], Optional[Dict[str, Any]]]:
    owner, name = _owner_name(repository)
    data = client.graphql(
        ISSUE_QUERY,
        {"owner": owner, "name": name, "number": issue_number},
    )
    repo = ((data or {}).get("data") or {}).get("repository")
    if not isinstance(repo, dict):
        return None, IssueSkip(repository, issue_number, "repository_missing"), None
    issue = repo.get("issue")
    if not isinstance(issue, dict):
        return None, IssueSkip(repository, issue_number, "issue_missing"), None
    if int(issue.get("number") or 0) != issue_number:
        raise GitHubError("issue snapshot number mismatch")
    if not isinstance(issue.get("id"), str) or not issue.get("id"):
        raise GitHubError("issue snapshot node id missing")
    if not isinstance(issue.get("updatedAt"), str) or not issue.get("updatedAt"):
        raise GitHubError("issue snapshot updatedAt missing")

    if str(issue.get("state") or "").upper() != "OPEN":
        return None, IssueSkip(repository, issue_number, "issue_not_open", str(issue.get("id") or "")), None

    labels_payload = issue.get("labels")
    if not isinstance(labels_payload, dict):
        raise GitHubError("issue labels payload malformed")
    labels_page_info = labels_payload.get("pageInfo")
    if (
        not isinstance(labels_page_info, dict)
        or not isinstance(labels_page_info.get("hasNextPage"), bool)
    ):
        raise GitHubError("issue labels pagination completeness unproved")
    if labels_page_info.get("hasNextPage"):
        return (
            None,
            IssueSkip(repository, issue_number, "labels_pagination_incomplete", str(issue.get("id") or "")),
            None,
        )
    label_names = {
        str(node.get("name") or "")
        for node in (labels_payload.get("nodes") or [])
        if isinstance(node, dict)
    }
    required = list(require_labels) if require_labels else ([trigger_label] if trigger_label else [])
    missing_required = [label for label in required if label not in label_names]
    if missing_required:
        return (
            None,
            IssueSkip(repository, issue_number, "missing_require_label", str(issue.get("id") or "")),
            None,
        )
    ignored_hit = [label for label in ignore_labels if label in label_names]
    if ignored_hit:
        return (
            None,
            IssueSkip(repository, issue_number, "ignore_label_present", str(issue.get("id") or "")),
            None,
        )

    comments_payload = issue.get("comments")
    if not isinstance(comments_payload, dict):
        raise GitHubError("issue comments payload malformed")
    comments = _fetch_all_comments(
        client,
        owner=owner,
        name=name,
        number=issue_number,
        first_page=comments_payload,
    )
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    latest_comment_id = ""
    if comments:
        latest_comment_id = str(comments[-1].get("id") or "")

    try:
        base_sha = fetch_base_sha(client, repository, base_ref)
    except GitHubError:
        return None, IssueSkip(repository, issue_number, "base_ref_missing", str(issue.get("id") or "")), None

    # Prefer repository URL from GraphQL. Local bare paths must not gain a .git suffix.
    head_url = str(repo.get("url") or "").strip()
    if head_url.startswith(("http://", "https://", "git@", "ssh://")):
        clone_url = head_url if head_url.endswith(".git") else head_url + ".git"
    elif head_url:
        clone_url = head_url
    else:
        clone_url = f"https://github.com/{repository}.git"

    public_comments = tuple(
        {
            "id": str(c.get("id") or ""),
            "body": str(c.get("body") or ""),
            "createdAt": str(c.get("createdAt") or ""),
            "author": str(((c.get("author") or {}) if isinstance(c.get("author"), dict) else {}).get("login") or ""),
        }
        for c in comments
        if isinstance(c, dict)
    )

    signal = IssueSignalV1(
        repository=repository,
        issue_number=int(issue.get("number") or issue_number),
        issue_node_id=str(issue.get("id") or ""),
        issue_url=str(issue.get("url") or f"https://github.com/{repository}/issues/{issue_number}"),
        title_digest=body_digest(title),
        body_digest=body_digest(body),
        conversation_digest=conversation_digest(title, body, public_comments),
        labels=sorted(label_names),
        author_login=str(
            ((issue.get("author") or {}) if isinstance(issue.get("author"), dict) else {}).get(
                "login"
            )
            or ""
        ),
        observed_updated_at=str(issue.get("updatedAt") or ""),
        latest_comment_node_id=latest_comment_id,
        base_ref=base_ref,
        observed_base_sha=base_sha,
        head_repository=repository,
        untrusted=True,
        _raw_title=title,
        _raw_body=body,
        _raw_comments=list(public_comments),
        clone_url=clone_url,
    )
    meta = {
        "is_private": repo.get("isPrivate"),
        "labels": sorted(label_names),
    }
    return signal, None, meta


def discover_issue_signals(
    client: GitHubClient,
    config: Config,
    *,
    issue_cfg: Optional[IssueAutomationConfig] = None,
) -> IssueDiscoveryResult:
    """Discover open labelled issues in explicitly enabled repositories."""
    cfg = issue_cfg or config.issue_automation
    if cfg is None:
        return IssueDiscoveryResult(signals=[], skips=[], inspected_issues=0)

    signals: List[IssueSignalV1] = []
    skips: List[IssueSkip] = []
    inspected = 0
    seen: Set[Tuple[str, int]] = set()

    for repository, base_ref in sorted(cfg.enabled_repositories.items()):
        if config.is_excluded(repository):
            skips.append(IssueSkip(repository, 0, "repository_excluded"))
            continue
        if config.exact_policy_for(repository) is None:
            skips.append(IssueSkip(repository, 0, "missing_exact_repository_policy"))
            continue

        query = _search_query(repository, cfg.require_labels)
        items = _paginate_issue_search(client, query)

        for item in items:
            # Search can still surface PRs if query is wrong; fail closed.
            if item.get("pull_request") is not None:
                num = int(item.get("number") or 0)
                skips.append(IssueSkip(repository, num, "is_pull_request"))
                continue
            if str(item.get("state") or "").lower() not in ("open", ""):
                num = int(item.get("number") or 0)
                skips.append(IssueSkip(repository, num, "issue_not_open"))
                continue

            number = int(item.get("number") or 0)
            if number <= 0:
                skips.append(IssueSkip(repository, 0, "invalid_issue_number"))
                continue
            key = (repository.lower(), number)
            if key in seen:
                continue
            seen.add(key)
            inspected += 1

            signal, skip, _meta = fetch_issue_snapshot(
                client,
                repository=repository,
                issue_number=number,
                base_ref=base_ref,
                require_labels=cfg.require_labels,
                ignore_labels=cfg.ignore_labels,
            )
            if skip is not None:
                skips.append(skip)
                continue
            if signal is None:
                skips.append(IssueSkip(repository, number, "snapshot_failed"))
                continue
            signals.append(signal)

    return IssueDiscoveryResult(signals=signals, skips=skips, inspected_issues=inspected)
