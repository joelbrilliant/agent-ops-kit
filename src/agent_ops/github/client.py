"""GitHub API via authenticated gh CLI (injectable for tests)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence
from urllib.parse import quote, urlencode

from agent_ops.process import ProcResult, run_argv


class GitHubError(RuntimeError):
    pass


class GitHubClient(Protocol):
    def rest_search_issues(self, query: str, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        ...

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ...

    def rest_get(self, path: str) -> Dict[str, Any]:
        ...

    def viewer_login(self) -> str:
        ...

    def required_checks(self, repository: str, pr_number: int) -> List[Dict[str, Any]]:
        ...

    def list_notifications(
        self,
        *,
        all_notifications: bool = False,
        participating: bool = True,
        per_page: int = 50,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        ...

    def mark_notification_read(self, thread_id: str) -> None:
        ...

    def create_pr_comment(self, repository: str, pr_number: int, body: str) -> int:
        ...

    def get_issue_comment(self, repository: str, comment_id: int) -> Dict[str, Any]:
        ...


@dataclass
class GhClient:
    gh_command: str = "gh"

    def _run(self, argv: Sequence[str], *, input_data: Optional[str] = None) -> ProcResult:
        result = run_argv([self.gh_command, *argv], stdin_data=input_data, timeout=120)
        if not result.ok:
            raise GitHubError(result.stderr.strip() or result.stdout.strip() or "gh failed")
        return result

    def rest_search_issues(self, query: str, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        path = f"search/issues?q={quote(query)}&per_page={per_page}&page={page}"
        result = self._run(["api", path])
        return json.loads(result.stdout)

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        result = self._run(
            ["api", "graphql", "--input", "-"],
            input_data=json.dumps(payload),
        )
        data = json.loads(result.stdout)
        if data.get("errors"):
            raise GitHubError(json.dumps(data["errors"]))
        return data

    def rest_get(self, path: str) -> Dict[str, Any]:
        result = self._run(["api", path])
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise GitHubError("rest_get expected object")
        return data

    def rest_get_any(self, path: str) -> Any:
        result = self._run(["api", path])
        return json.loads(result.stdout)

    def viewer_login(self) -> str:
        data = self.rest_get("user")
        login = data.get("login")
        if not login:
            raise GitHubError("unable to resolve authenticated user")
        return str(login)

    def required_checks(self, repository: str, pr_number: int) -> List[Dict[str, Any]]:
        result = run_argv(
            [
                self.gh_command,
                "pr",
                "checks",
                str(pr_number),
                "--repo",
                repository,
                "--required",
                "--json",
                "bucket,name,state",
            ],
            timeout=120,
            check=False,
        )
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise GitHubError("required checks returned invalid shape") from exc
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise GitHubError("required checks returned invalid shape")
        if not result.ok and not rows:
            message = (result.stderr or result.stdout).lower()
            if "no required checks" not in message:
                raise GitHubError("required checks query failed")
        return [dict(row) for row in rows]

    def all_checks(self, repository: str, pr_number: int) -> List[Dict[str, Any]]:
        result = run_argv(
            [
                self.gh_command,
                "pr",
                "checks",
                str(pr_number),
                "--repo",
                repository,
                "--json",
                "bucket,name,state",
            ],
            timeout=120,
            check=False,
        )
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise GitHubError("all checks returned invalid shape") from exc
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise GitHubError("all checks returned invalid shape")
        if not result.ok and not rows:
            raise GitHubError("all checks query failed")
        return [dict(row) for row in rows]

    def list_notifications(
        self,
        *,
        all_notifications: bool = False,
        participating: bool = True,
        per_page: int = 50,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        query = urlencode(
            {
                "all": "true" if all_notifications else "false",
                "participating": "true" if participating else "false",
                "per_page": str(per_page),
                "page": str(page),
            }
        )
        result = self._run(["api", f"notifications?{query}"])
        data = json.loads(result.stdout or "[]")
        if not isinstance(data, list):
            raise GitHubError("notifications returned invalid shape")
        return [dict(item) for item in data if isinstance(item, dict)]

    def mark_notification_read(self, thread_id: str) -> None:
        result = run_argv(
            [
                self.gh_command,
                "api",
                "-X",
                "PATCH",
                f"notifications/threads/{thread_id}",
                "--silent",
            ],
            timeout=60,
            check=False,
        )
        if not result.ok:
            raise GitHubError(result.stderr.strip() or "mark notification read failed")

    def get_notification_thread(self, thread_id: str) -> Dict[str, Any]:
        result = run_argv(
            [
                self.gh_command,
                "api",
                f"notifications/threads/{thread_id}",
            ],
            timeout=60,
            check=False,
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise GitHubError("notification thread returned invalid JSON") from exc
        if not result.ok or not isinstance(payload, dict):
            raise GitHubError(f"get notification thread failed: {thread_id}")
        return dict(payload)

    def create_pr_comment(self, repository: str, pr_number: int, body: str) -> int:
        if not body.strip():
            raise GitHubError("PR comment body is empty")
        result = self._run(
            [
                "api",
                "-X",
                "POST",
                f"repos/{repository}/issues/{int(pr_number)}/comments",
                "-f",
                f"body={body}",
            ]
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise GitHubError("create PR comment returned invalid JSON") from exc
        comment_id = int(payload.get("id") or 0) if isinstance(payload, dict) else 0
        if comment_id <= 0:
            raise GitHubError("create PR comment returned no id")
        return comment_id

    def get_issue_comment(self, repository: str, comment_id: int) -> Dict[str, Any]:
        return self.rest_get(f"repos/{repository}/issues/comments/{int(comment_id)}")


class FakeGitHub:
    """In-memory GitHub for synthetic tests.

    search_pages maps query string -> list of pages, each page a list of items.
    """

    def __init__(self) -> None:
        self.search_pages: Dict[str, List[List[Dict[str, Any]]]] = {}
        self.graphql_handlers: List[Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = []
        self.rest_handlers: Dict[str, Any] = {}
        self.login = "operator"
        self.replies: List[Dict[str, Any]] = []
        self.mutations: List[Dict[str, Any]] = []
        self.required_check_rows: Dict[str, Any] = {}
        self.all_check_rows: Dict[str, Any] = {}
        self.notifications: List[Dict[str, Any]] = []
        self.marked_read: List[str] = []
        self.pr_comments: List[Dict[str, Any]] = []

    def rest_search_issues(self, query: str, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        pages: List[List[Dict[str, Any]]] = self.search_pages.get(query, [])
        if page < 1 or page > len(pages):
            return {"total_count": 0, "incomplete_results": False, "items": []}
        items = pages[page - 1]
        total = sum(len(p) for p in pages)
        return {"total_count": total, "incomplete_results": False, "items": items}

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        variables = variables or {}
        for handler in self.graphql_handlers:
            result = handler(query, variables)
            if result is not None:
                return result
        raise GitHubError(f"no fake graphql handler for query vars={variables}")

    def rest_get(self, path: str) -> Dict[str, Any]:
        if path in self.rest_handlers:
            val = self.rest_handlers[path]
            out = val() if callable(val) else val
            if not isinstance(out, dict):
                raise GitHubError(f"rest_get expected object for {path}")
            return dict(out)
        for key, val in self.rest_handlers.items():
            if key in path:
                out = val() if callable(val) else val
                if not isinstance(out, dict):
                    raise GitHubError(f"rest_get expected object for {path}")
                return dict(out)
        raise GitHubError(f"no fake rest handler for {path}")

    def rest_get_any(self, path: str) -> Any:
        if path in self.rest_handlers:
            val = self.rest_handlers[path]
            out = val() if callable(val) else val
            return out
        for key, val in self.rest_handlers.items():
            if key in path:
                out = val() if callable(val) else val
                return out
        raise GitHubError(f"no fake rest handler for {path}")

    def viewer_login(self) -> str:
        return self.login

    def required_checks(self, repository: str, pr_number: int) -> List[Dict[str, Any]]:
        key = f"{repository.lower()}#{pr_number}"
        value = self.required_check_rows.get(
            key,
            [{"bucket": "pass", "name": "synthetic-required", "state": "SUCCESS"}],
        )
        rows = value() if callable(value) else value
        return [dict(row) for row in rows]

    def all_checks(self, repository: str, pr_number: int) -> List[Dict[str, Any]]:
        key = f"{repository.lower()}#{pr_number}"
        value = getattr(self, "all_check_rows", {}).get(key)
        if value is None:
            value = self.required_check_rows.get(key, [])
        rows = value() if callable(value) else value
        return [dict(row) for row in rows]

    def list_notifications(
        self,
        *,
        all_notifications: bool = False,
        participating: bool = True,
        per_page: int = 50,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        del all_notifications, participating, per_page, page
        return [dict(item) for item in self.notifications]

    def mark_notification_read(self, thread_id: str) -> None:
        if getattr(self, "mark_read_error", None):
            err = self.mark_read_error
            if callable(err):
                err = err(thread_id)
            if isinstance(err, Exception):
                raise err
            raise GitHubError(str(err or "mark failed"))
        self.marked_read.append(str(thread_id))

    def get_notification_thread(self, thread_id: str) -> Dict[str, Any]:
        threads = getattr(self, "notification_threads", {}) or {}
        if thread_id in threads:
            value = threads[thread_id]
            return dict(value() if callable(value) else value)
        for row in self.notifications:
            if str(row.get("id") or "") == str(thread_id):
                return dict(row)
        raise GitHubError(f"notification thread not found: {thread_id}")

    def create_pr_comment(self, repository: str, pr_number: int, body: str) -> int:
        comment_id = len(self.pr_comments) + 1
        self.pr_comments.append(
            {
                "id": comment_id,
                "repository": repository,
                "pr_number": int(pr_number),
                "body": body,
            }
        )
        return comment_id

    def get_issue_comment(self, repository: str, comment_id: int) -> Dict[str, Any]:
        for comment in self.pr_comments:
            if (
                comment["repository"].lower() == repository.lower()
                and int(comment["id"]) == int(comment_id)
            ):
                return dict(comment)
        raise GitHubError(f"issue comment not found: {repository}#{comment_id}")
