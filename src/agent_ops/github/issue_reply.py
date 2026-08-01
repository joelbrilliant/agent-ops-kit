"""Issue comment post and exact body readback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from agent_ops.github.client import GitHubClient, GitHubError


ADD_ISSUE_COMMENT = """
mutation($subjectId: ID!, $body: String!) {
  addComment(input: {subjectId: $subjectId, body: $body}) {
    commentEdge {
      node {
        id
        body
        url
      }
    }
  }
}
"""

NODE_COMMENT_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on IssueComment {
      id
      body
      url
    }
  }
}
"""


@dataclass(frozen=True)
class IssueCommentResult:
    node_id: str
    body: str
    url: str


def post_issue_comment(
    client: GitHubClient,
    *,
    issue_node_id: str,
    body: str,
) -> IssueCommentResult:
    data = client.graphql(
        ADD_ISSUE_COMMENT,
        {"subjectId": issue_node_id, "body": body},
    )
    edge = (
        ((data or {}).get("data") or {})
        .get("addComment", {})
        .get("commentEdge")
        or {}
    )
    node = edge.get("node") if isinstance(edge, dict) else None
    if not isinstance(node, dict):
        raise GitHubError("addComment returned no comment node")
    node_id = str(node.get("id") or "")
    posted_body = str(node.get("body") or "")
    if not node_id:
        raise GitHubError("addComment missing comment id")
    if posted_body != body:
        raise GitHubError("addComment body mismatch on immediate response")
    return IssueCommentResult(
        node_id=node_id,
        body=posted_body,
        url=str(node.get("url") or ""),
    )


def read_issue_comment(client: GitHubClient, node_id: str) -> IssueCommentResult:
    data = client.graphql(NODE_COMMENT_QUERY, {"id": node_id})
    node = ((data or {}).get("data") or {}).get("node")
    if not isinstance(node, dict):
        raise GitHubError("issue comment node missing on readback")
    return IssueCommentResult(
        node_id=str(node.get("id") or ""),
        body=str(node.get("body") or ""),
        url=str(node.get("url") or ""),
    )


def post_and_verify_issue_comment(
    client: GitHubClient,
    *,
    issue_node_id: str,
    body: str,
) -> IssueCommentResult:
    posted = post_issue_comment(client, issue_node_id=issue_node_id, body=body)
    readback = read_issue_comment(client, posted.node_id)
    if readback.node_id != posted.node_id:
        raise GitHubError("issue comment node id mismatch on readback")
    if readback.body != body:
        raise GitHubError("issue comment body mismatch on readback")
    return readback
