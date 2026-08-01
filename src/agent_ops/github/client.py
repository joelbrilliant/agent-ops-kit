"""GitHub API via authenticated gh CLI (injectable for tests)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

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

    def rest_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def viewer_login(self) -> str:
        ...

    def required_checks(self, repository: str, pr_number: int) -> List[Dict[str, Any]]:
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
        # gh api accepts query params as field flags - pass path with encoded query carefully
        # Use -f for form; for GET search use path only.
        from urllib.parse import quote

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
        return json.loads(result.stdout)

    def rest_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._run(
            ["api", "--method", "POST", path, "--input", "-"],
            input_data=json.dumps(payload),
        )
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
            raise GitHubError("required checks returned invalid JSON") from exc
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise GitHubError("required checks returned invalid shape")
        if not result.ok and not rows:
            message = (result.stderr or result.stdout).lower()
            if "no required checks" not in message:
                raise GitHubError("required checks query failed")
        return [dict(row) for row in rows]


class FakeGitHub:
    """In-memory GitHub for synthetic tests.

    search_pages maps query string -> list of pages, each page a list of items.
    """

    def __init__(self) -> None:
        self.search_pages: Dict[str, List[List[Dict[str, Any]]]] = {}
        self.graphql_handlers: List[Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = []
        self.rest_handlers: Dict[str, Any] = {}
        self.rest_post_handlers: Dict[str, Any] = {}
        self.login = "operator"
        self.replies: List[Dict[str, Any]] = []
        self.mutations: List[Dict[str, Any]] = []
        self.required_check_rows: Dict[str, Any] = {}
        self.created_pulls: List[Dict[str, Any]] = []
        self.created_issue_comments: List[Dict[str, Any]] = []

    def rest_search_issues(self, query: str, page: int = 1, per_page: int = 100) -> Dict[str, Any]:
        pages: List[List[Dict[str, Any]]] = self.search_pages.get(query, [])
        unique_keys = {
            str(item.get("node_id") or item.get("id") or f"{page_index}:{item_index}")
            for page_index, search_page in enumerate(pages)
            for item_index, item in enumerate(search_page)
        }
        total = len(unique_keys)
        # Exact key only - avoid cross-query contamination between author/user searches
        if page < 1 or page > len(pages):
            return {"total_count": total, "incomplete_results": False, "items": []}
        items = pages[page - 1]
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
            if isinstance(out, list):
                return out  # type: ignore[return-value]
            return dict(out)  # type: ignore[arg-type]
        for key, val in self.rest_handlers.items():
            if key in path:
                out = val() if callable(val) else val
                if isinstance(out, list):
                    return out  # type: ignore[return-value]
                return dict(out)  # type: ignore[arg-type]
        raise GitHubError(f"no fake rest handler for {path}")

    def rest_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if path in self.rest_post_handlers:
            val = self.rest_post_handlers[path]
            out = val(payload) if callable(val) else val
            return dict(out)  # type: ignore[arg-type]
        for key, val in self.rest_post_handlers.items():
            if key in path:
                out = val(payload) if callable(val) else val
                return dict(out)  # type: ignore[arg-type]
        # Default draft PR creator for tests.
        if path.endswith("/pulls"):
            number = 9000 + len(self.created_pulls) + 1
            head = str(payload.get("head") or "branch")
            if ":" in head:
                head = head.split(":", 1)[1]
            row = {
                "number": number,
                "html_url": f"https://github.com/example/pull/{number}",
                "node_id": f"PR_NODE_{number}",
                "draft": bool(payload.get("draft", True)),
                "title": payload.get("title"),
                "body": payload.get("body"),
                "head": {"ref": head, "sha": payload.get("_head_sha") or "headsha"},
                "base": {"ref": payload.get("base")},
            }
            self.created_pulls.append({"path": path, "payload": dict(payload), "response": row})
            return dict(row)
        raise GitHubError(f"no fake rest_post handler for {path}")

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
