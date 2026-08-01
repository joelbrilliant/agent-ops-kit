"""Draft pull request creation and verified readback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent_ops.github.client import GitHubClient, GitHubError


CREATE_DRAFT_PR_MUTATION = """
mutation($repositoryId: ID!, $baseRefName: String!, $headRefName: String!, $title: String!, $body: String!) {
  createPullRequest(input: {
    repositoryId: $repositoryId
    baseRefName: $baseRefName
    headRefName: $headRefName
    title: $title
    body: $body
    draft: true
  }) {
    pullRequest {
      id
      number
      url
      isDraft
      baseRefName
      headRefName
      headRefOid
      state
    }
  }
}
"""

REPO_ID_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
  }
}
"""

PR_READBACK_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
      url
      isDraft
      baseRefName
      headRefName
      headRefOid
      state
      merged
    }
  }
}
"""


@dataclass(frozen=True)
class DraftPullRequest:
    node_id: str
    number: int
    url: str
    is_draft: bool
    base_ref: str
    head_ref: str
    head_oid: str
    state: str


def _owner_name(repository: str) -> tuple[str, str]:
    owner, name = repository.split("/", 1)
    return owner, name


def repository_node_id(client: GitHubClient, repository: str) -> str:
    owner, name = _owner_name(repository)
    data = client.graphql(REPO_ID_QUERY, {"owner": owner, "name": name})
    repo = ((data or {}).get("data") or {}).get("repository") or {}
    rid = repo.get("id")
    if not isinstance(rid, str) or not rid:
        raise GitHubError(f"repository node id missing for {repository}")
    return rid


def create_draft_pull_request(
    client: GitHubClient,
    *,
    repository: str,
    base_ref: str,
    head_ref: str,
    title: str,
    body: str,
) -> DraftPullRequest:
    repo_id = repository_node_id(client, repository)
    data = client.graphql(
        CREATE_DRAFT_PR_MUTATION,
        {
            "repositoryId": repo_id,
            "baseRefName": base_ref,
            "headRefName": head_ref,
            "title": title,
            "body": body,
        },
    )
    pr = (
        ((data or {}).get("data") or {})
        .get("createPullRequest", {})
        .get("pullRequest")
    )
    if not isinstance(pr, dict):
        raise GitHubError("createPullRequest returned no pullRequest")
    return DraftPullRequest(
        node_id=str(pr.get("id") or ""),
        number=int(pr.get("number") or 0),
        url=str(pr.get("url") or ""),
        is_draft=bool(pr.get("isDraft")),
        base_ref=str(pr.get("baseRefName") or ""),
        head_ref=str(pr.get("headRefName") or ""),
        head_oid=str(pr.get("headRefOid") or ""),
        state=str(pr.get("state") or ""),
    )


def read_pull_request(
    client: GitHubClient,
    *,
    repository: str,
    number: int,
) -> DraftPullRequest:
    owner, name = _owner_name(repository)
    data = client.graphql(
        PR_READBACK_QUERY,
        {"owner": owner, "name": name, "number": number},
    )
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest")
    if not isinstance(pr, dict):
        raise GitHubError(f"pull request {repository}#{number} missing on readback")
    if pr.get("merged") is True:
        raise GitHubError("pull request is merged on readback")
    return DraftPullRequest(
        node_id=str(pr.get("id") or ""),
        number=int(pr.get("number") or 0),
        url=str(pr.get("url") or ""),
        is_draft=bool(pr.get("isDraft")),
        base_ref=str(pr.get("baseRefName") or ""),
        head_ref=str(pr.get("headRefName") or ""),
        head_oid=str(pr.get("headRefOid") or ""),
        state=str(pr.get("state") or ""),
    )


def verify_draft_pr_readback(
    pr: DraftPullRequest,
    *,
    expected_base_ref: str,
    expected_head_ref: str,
    expected_head_oid: str,
) -> Optional[str]:
    if pr.state != "OPEN":
        return "pr_not_open"
    if not pr.is_draft:
        return "pr_not_draft"
    if pr.base_ref != expected_base_ref:
        return "pr_base_ref_mismatch"
    if pr.head_ref != expected_head_ref:
        return "pr_head_ref_mismatch"
    if pr.head_oid != expected_head_oid:
        return "pr_head_oid_mismatch"
    if pr.number <= 0 or not pr.url or not pr.node_id:
        return "pr_identity_incomplete"
    return None
