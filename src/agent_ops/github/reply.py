"""Exact-thread reply via GraphQL mutation + readback."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from agent_ops.github.client import GitHubClient, GitHubError


ADD_REPLY_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment {
      id
      body
      url
    }
  }
}
"""

THREAD_READBACK_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      id
      isResolved
      isOutdated
      comments(last: 20) {
        nodes {
          id
          author { login }
          body
          createdAt
        }
      }
    }
  }
}
"""


def build_reply_body(*, resulting_sha: str, named_checks: list) -> str:
    checks = ", ".join(named_checks) if named_checks else "none"
    short = resulting_sha[:7] if resulting_sha else "unknown"
    return (
        f"Agent Ops Kit applied a bounded fix in `{short}`.\n"
        f"Verification: {checks}.\n"
        f"Thread left unresolved for human confirmation."
    )


def reply_on_thread(
    client: GitHubClient,
    *,
    thread_node_id: str,
    body: str,
) -> str:
    data = client.graphql(
        ADD_REPLY_MUTATION,
        {"threadId": thread_node_id, "body": body},
    )
    comment = (
        ((data.get("data") or {}).get("addPullRequestReviewThreadReply") or {}).get("comment")
        or {}
    )
    cid = comment.get("id")
    if not cid:
        raise GitHubError("reply mutation returned no comment id")
    return str(cid)


def readback_thread(client: GitHubClient, thread_node_id: str) -> Dict[str, Any]:
    data = client.graphql(THREAD_READBACK_QUERY, {"id": thread_node_id})
    node = (data.get("data") or {}).get("node")
    if not node:
        raise GitHubError("thread readback returned no node")
    return node


def verify_reply_present(thread: Dict[str, Any], reply_node_id: str) -> bool:
    comments = ((thread.get("comments") or {}).get("nodes")) or []
    return any(str(c.get("id")) == reply_node_id for c in comments)


def thread_still_actionable(
    thread: Dict[str, Any],
    *,
    expected_latest_comment_id: str,
) -> Tuple[bool, str]:
    if thread.get("isResolved"):
        return False, "thread_resolved"
    if thread.get("isOutdated"):
        return False, "thread_outdated"
    comments = ((thread.get("comments") or {}).get("nodes")) or []
    if not comments:
        return False, "thread_empty"
    latest = comments[-1]
    # If our reply is already latest, not newly actionable
    if str(latest.get("id")) != expected_latest_comment_id:
        # Comment changed - stale relative to claim
        return False, "latest_comment_changed"
    return True, "ok"
