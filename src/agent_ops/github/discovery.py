"""Thread-aware PR discovery via search + GraphQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from agent_ops.contracts import SignalV1, body_digest
from agent_ops.github.client import GitHubClient, GitHubError


PR_THREADS_QUERY = """
query PrThreadsPage($owner: String!, $name: String!, $number: Int!, $threadCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      url
      isDraft
      baseRefName
      headRefName
      headRefOid
      headRepository {
        nameWithOwner
        url
        isFork
      }
      baseRepository {
        nameWithOwner
      }
      author {
        login
      }
      reviewThreads(first: 100, after: $threadCursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
        }
      }
    }
  }
}
"""


THREAD_COMMENTS_QUERY = """
query ReviewThreadCommentsPage($id: ID!, $commentCursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $commentCursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          databaseId
          author { login }
          authorAssociation
          body
          createdAt
          viewerDidAuthor
        }
      }
    }
  }
}
"""


@dataclass
class DiscoverSkip:
    repository: str
    pr_number: int
    reason: str
    thread_node_id: Optional[str] = None


@dataclass
class DiscoverResult:
    signals: List[SignalV1]
    skips: List[DiscoverSkip]
    inspected_prs: List[str]


def _parse_repo(full_name: str) -> Tuple[str, str]:
    parts = full_name.split("/")
    if len(parts) != 2:
        raise GitHubError(f"invalid repository name: {full_name}")
    return parts[0], parts[1]


def search_open_prs(
    client: GitHubClient,
    *,
    operator_logins: Sequence[str],
    owned_namespaces: Sequence[str],
    max_pages: int = 10,
    per_page: int = 100,
) -> List[Dict[str, Any]]:
    """Account-wide open PR discovery; dedupe by pull request URL/id."""
    seen: Set[str] = set()
    items: List[Dict[str, Any]] = []
    page_size = max(1, min(int(per_page), 100))

    queries: List[str] = []
    for login in operator_logins:
        queries.append(f"is:pr is:open author:{login}")
    for ns in owned_namespaces:
        # user: or org: both work as qualifiers for owned namespace search
        queries.append(f"is:pr is:open user:{ns}")

    for q in queries:
        for page in range(1, max_pages + 1):
            data = client.rest_search_issues(q, page=page, per_page=page_size)
            if data.get("incomplete_results"):
                raise GitHubError(f"search results incomplete for query: {q}")
            total_count = int(data.get("total_count") or 0)
            if total_count > 1000:
                raise GitHubError(f"search result exceeds GitHub's 1000-item window: {q}")
            batch = data.get("items") or []
            if not batch:
                break
            for item in batch:
                key = str(item.get("pull_request", {}).get("url") or item.get("html_url") or item.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
            if len(batch) < page_size:
                break
            if page == max_pages and page * page_size < total_count:
                raise GitHubError(f"search pagination limit reached for query: {q}")
    return items


def _repo_from_search_item(item: Dict[str, Any]) -> str:
    # repository_url: https://api.github.com/repos/owner/name
    repo_url = item.get("repository_url") or ""
    if "/repos/" in repo_url:
        return repo_url.split("/repos/", 1)[1]
    html = item.get("html_url") or ""
    # https://github.com/owner/name/pull/1
    parts = html.split("github.com/")
    if len(parts) == 2:
        segs = parts[1].split("/")
        if len(segs) >= 2:
            return f"{segs[0]}/{segs[1]}"
    raise GitHubError(f"cannot derive repository from search item {item.get('id')}")


def _pr_number_from_item(item: Dict[str, Any]) -> int:
    if item.get("number") is not None:
        return int(item["number"])
    raise GitHubError("search item missing number")


def _fetch_thread_comments(client: GitHubClient, thread: Dict[str, Any]) -> List[Dict[str, Any]]:
    existing = thread.get("comments")
    if isinstance(existing, dict) and "pageInfo" not in existing:
        return [dict(node) for node in (existing.get("nodes") or [])]

    comments: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    seen_cursors: Set[str] = set()
    while True:
        data = client.graphql(
            THREAD_COMMENTS_QUERY,
            {"id": str(thread.get("id") or ""), "commentCursor": cursor},
        )
        node = (data.get("data") or {}).get("node") or {}
        connection = node.get("comments") or {}
        comments.extend(dict(comment) for comment in (connection.get("nodes") or []))
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return comments
        next_cursor = str(page_info.get("endCursor") or "")
        if not next_cursor or next_cursor in seen_cursors:
            raise GitHubError("comment pagination cursor missing or repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def fetch_pr_threads(client: GitHubClient, repository: str, pr_number: int) -> Dict[str, Any]:
    owner, name = _parse_repo(repository)
    cursor: Optional[str] = None
    seen_cursors: Set[str] = set()
    result: Optional[Dict[str, Any]] = None
    threads: List[Dict[str, Any]] = []

    while True:
        data = client.graphql(
            PR_THREADS_QUERY,
            {
                "owner": owner,
                "name": name,
                "number": pr_number,
                "threadCursor": cursor,
            },
        )
        repo = (data.get("data") or {}).get("repository") or {}
        pr = repo.get("pullRequest")
        if not pr:
            raise GitHubError(f"pull request not found: {repository}#{pr_number}")
        if result is None:
            result = dict(pr)
        connection = pr.get("reviewThreads") or {}
        for raw_thread in connection.get("nodes") or []:
            thread = dict(raw_thread)
            thread["comments"] = {"nodes": _fetch_thread_comments(client, thread)}
            threads.append(thread)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = str(page_info.get("endCursor") or "")
        if not next_cursor or next_cursor in seen_cursors:
            raise GitHubError("review-thread pagination cursor missing or repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    assert result is not None
    result["reviewThreads"] = {"nodes": threads}
    return result


def extract_signals_from_pr(
    pr: Dict[str, Any],
    *,
    base_repository: str,
    trusted_reviewer_logins: Sequence[str],
    operator_logins: Sequence[str],
    trusted_reviewer_associations: Sequence[str] = (),
) -> Tuple[List[SignalV1], List[DiscoverSkip]]:
    trusted = {x.lower() for x in trusted_reviewer_logins}
    trusted_associations = {x.upper() for x in trusted_reviewer_associations}
    operators = {x.lower() for x in operator_logins}
    signals: List[SignalV1] = []
    skips: List[DiscoverSkip] = []
    head_sha = str(pr.get("headRefOid") or "")
    head_repo = (pr.get("headRepository") or {}).get("nameWithOwner") or base_repository
    base_repo = (pr.get("baseRepository") or {}).get("nameWithOwner") or base_repository
    head_ref = str(pr.get("headRefName") or "")
    head_url = (pr.get("headRepository") or {}).get("url") or ""
    is_fork = bool((pr.get("headRepository") or {}).get("isFork"))
    pr_number = int(pr.get("number") or 0)
    # number may be absent in nested GraphQL - caller should pass if needed
    threads = ((pr.get("reviewThreads") or {}).get("nodes")) or []

    if not head_sha or not head_ref or not head_repo:
        skips.append(DiscoverSkip(base_repository, pr_number, "pr_head_incomplete"))
        return signals, skips

    for thread in threads:
        tid = str(thread.get("id") or "")
        if thread.get("isResolved"):
            skips.append(
                DiscoverSkip(base_repository, pr_number, "thread_resolved", tid)
            )
            continue
        if thread.get("isOutdated"):
            skips.append(
                DiscoverSkip(base_repository, pr_number, "thread_outdated", tid)
            )
            continue
        comments = ((thread.get("comments") or {}).get("nodes")) or []
        if not comments:
            skips.append(
                DiscoverSkip(base_repository, pr_number, "thread_empty", tid)
            )
            continue
        latest_external_index: Optional[int] = None
        for index in range(len(comments) - 1, -1, -1):
            comment = comments[index]
            author_login = ((comment.get("author") or {}).get("login") or "").lower()
            if comment.get("viewerDidAuthor") or author_login in operators:
                continue
            latest_external_index = index
            break
        if latest_external_index is None:
            skips.append(
                DiscoverSkip(base_repository, pr_number, "no_external_comment", tid)
            )
            continue
        if any(
            comment.get("viewerDidAuthor")
            or ((comment.get("author") or {}).get("login") or "").lower() in operators
            for comment in comments[latest_external_index + 1 :]
        ):
            skips.append(
                DiscoverSkip(base_repository, pr_number, "operator_replied_after_review", tid)
            )
            continue
        latest = comments[latest_external_index]
        author = ((latest.get("author") or {}).get("login") or "").lower()
        association = str(latest.get("authorAssociation") or "").upper()
        if author not in trusted and association not in trusted_associations:
            skips.append(
                DiscoverSkip(base_repository, pr_number, "untrusted_author", tid)
            )
            continue
        body = str(latest.get("body") or "")
        path = str(thread.get("path") or "")
        if not tid or not latest.get("id") or not path:
            skips.append(
                DiscoverSkip(base_repository, pr_number, "thread_contract_incomplete", tid)
            )
            continue
        line = thread.get("line")
        line_i = int(line) if line is not None else None
        signals.append(
            SignalV1(
                repository=base_repository,
                pr_number=pr_number,
                thread_node_id=tid,
                latest_comment_node_id=str(latest.get("id") or ""),
                trusted_author_login=author,
                path=path,
                line=line_i,
                observed_head_sha=head_sha,
                body_digest=body_digest(body),
                untrusted=True,
                base_repository=base_repo,
                head_repository=str(head_repo),
                head_ref=head_ref,
                head_clone_url=str(head_url) + ".git" if head_url and not str(head_url).endswith(".git") else str(head_url),
                is_fork=is_fork,
                pr_url=str(pr.get("url") or ""),
                _raw_body=body,
            )
        )
    return signals, skips


def discover_actionable_signals(
    client: GitHubClient,
    *,
    operator_logins: Sequence[str],
    owned_namespaces: Sequence[str],
    trusted_reviewer_logins: Sequence[str],
    excluded_repositories: Sequence[str],
    trusted_reviewer_associations: Sequence[str] = (),
    pr_number_override: Optional[Dict[str, int]] = None,
) -> DiscoverResult:
    excluded = {x.lower() for x in excluded_repositories}
    items = search_open_prs(
        client, operator_logins=operator_logins, owned_namespaces=owned_namespaces
    )
    all_signals: List[SignalV1] = []
    all_skips: List[DiscoverSkip] = []
    inspected: List[str] = []
    seen_pr: Set[str] = set()

    for item in items:
        repo = _repo_from_search_item(item)
        num = _pr_number_from_item(item)
        key = f"{repo}#{num}".lower()
        if key in seen_pr:
            continue
        seen_pr.add(key)
        if repo.lower() in excluded:
            all_skips.append(DiscoverSkip(repo, num, "repository_excluded"))
            continue
        inspected.append(f"{repo}#{num}")
        pr = fetch_pr_threads(client, repo, num)
        # Inject number if GraphQL omitted
        if "number" not in pr:
            pr = dict(pr)
            pr["number"] = num
        signals, skips = extract_signals_from_pr(
            pr,
            base_repository=repo,
            trusted_reviewer_logins=trusted_reviewer_logins,
            operator_logins=operator_logins,
            trusted_reviewer_associations=trusted_reviewer_associations,
        )
        all_signals.extend(signals)
        all_skips.extend(skips)

    return DiscoverResult(signals=all_signals, skips=all_skips, inspected_prs=inspected)
