"""Fixed-argv GitHub CLI calls and notification parsing."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from .config import Config

if TYPE_CHECKING:
    from .worker import WorkerResult


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_THREAD = re.compile(r"^[A-Za-z0-9_-]+$")
_PULL_PATH = re.compile(r"^/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pulls/(\d+)$")


class GitHubError(RuntimeError):
    pass
@dataclass(frozen=True)
class Notification:
    notification_id: str
    updated_at: str
    repository: str
    pull_number: int | None
    subject_type: str
    reason: str
    kind: str
    source_head_sha: str | None
    subject_url: str | None = None
    subject_title: str = ""
    latest_comment_url: str | None = None
@dataclass(frozen=True)
class PullRequest:
    repository: str
    number: int
    state: str
    merged: bool
    head_sha: str
    url: str
    author: str
    title: str = ""
    body: str = ""
@dataclass(frozen=True)
class ResolvedNotification:
    notification: Notification
    pull: PullRequest
    source_head_sha: str | None = None
    mutation_allowed: bool = False
    latest_comment_body: str | None = None

    def __post_init__(self) -> None:
        if self.source_head_sha is None:
            object.__setattr__(self, "source_head_sha", self.notification.source_head_sha)
Runner = Callable[..., subprocess.CompletedProcess[str]]
class GitHub:
    """Uses `gh api` with fixed argv and no shell interpolation."""

    def __init__(self, config: Config | None = None, runner: Runner = subprocess.run) -> None:
        self.config = config
        self.runner = runner

    def list_notifications(self, limit: int) -> list[Notification]:
        payload = self._get(f"/notifications?all=false&participating=false&per_page={limit}")
        if not isinstance(payload, list):
            raise GitHubError("notifications response was not a list")
        return [item for raw in payload if (item := self._notification(raw)) is not None]
    def resolve(self, item: Notification) -> ResolvedNotification | None:
        source_head = item.source_head_sha
        number = item.pull_number
        if number is None:
            details = self._get_subject(item.subject_url)
            number = self._check_pull_number(details)
            source_head = self._text(details.get("head_sha"))
        if number is None or self.config is None:
            return None
        pull = self._pull(item.repository, number)
        mutation_allowed = pull.author == self.config.github_login or (
            pull.repository.split("/", 1)[0] in self.config.allowed_namespaces and self._can_push(item.repository)
        )
        return ResolvedNotification(item, pull, source_head, mutation_allowed, self._latest_comment_body(item.latest_comment_url))
    def head_is_green(self, item: ResolvedNotification) -> bool:
        payload = self._get(f"/repos/{item.pull.repository}/commits/{item.pull.head_sha}/check-runs")
        runs = payload.get("check_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list) or not runs:
            return False
        return all(
            isinstance(run, dict)
            and run.get("status") == "completed"
            and run.get("conclusion") in {"success", "neutral", "skipped"}
            for run in runs
        )

    def mark_read(self, notification_id: str) -> None:
        if not _THREAD.fullmatch(notification_id):
            raise GitHubError("invalid notification id")
        self._call(["gh", "api", "--method", "PATCH", f"/notifications/threads/{notification_id}"])

    def verify_completion(self, item: ResolvedNotification, result: WorkerResult) -> bool:
        if result.head_sha is None or result.comment_kind is None or result.comment_id is None:
            return False
        remote = self._pull(item.pull.repository, item.pull.number)
        if remote.head_sha != result.head_sha:
            return False
        endpoint = self._comment_endpoint(item.pull.repository, item.pull.number, result.comment_kind, result.comment_id)
        if endpoint is None:
            return False
        comment = self._get(endpoint)
        user = comment.get("user") if isinstance(comment, dict) else None
        if not isinstance(comment, dict) or str(comment.get("id")) != str(result.comment_id) or self.config is None:
            return False
        if not isinstance(user, dict) or user.get("login") != self.config.github_login:
            return False
        if result.comment_kind == "review_summary":
            return True
        key = "issue_url" if result.comment_kind == "issue" else "pull_request_url"
        expected = f"/repos/{item.pull.repository}/{'issues' if result.comment_kind == 'issue' else 'pulls'}/{item.pull.number}"
        return self._api_url(comment.get(key)) == expected

    def _notification(self, raw: Any) -> Notification | None:
        if not isinstance(raw, dict) or raw.get("unread") is False:
            return None
        subject = raw.get("subject")
        repository = raw.get("repository")
        if not isinstance(subject, dict) or not isinstance(repository, dict):
            return None
        full_name = self._text(repository.get("full_name"))
        subject_url = self._text(subject.get("url"))
        notification_id = self._text(raw.get("id"))
        updated_at = self._text(raw.get("updated_at"))
        subject_type = self._text(subject.get("type"))
        reason = self._text(raw.get("reason")) or "unknown"
        if not all((full_name, subject_url, notification_id, updated_at, subject_type)) or not _REPOSITORY.fullmatch(full_name):
            return None
        path = self._subject_path(subject_url)
        pull_match = _PULL_PATH.fullmatch(path or "")
        if pull_match and pull_match.group(1) == full_name:
            return Notification(
                notification_id, updated_at, full_name, int(pull_match.group(2)), subject_type, reason, "review", None,
                subject_url, self._text(subject.get("title")) or "", self._api_url(subject.get("latest_comment_url")),
            )
        if subject_type in {"CheckSuite", "CheckRun"}:
            return Notification(
                notification_id, updated_at, full_name, None, subject_type, reason, "check", None,
                subject_url, self._text(subject.get("title")) or "", self._api_url(subject.get("latest_comment_url")),
            )
        return None

    def _pull(self, repository: str, number: int) -> PullRequest:
        payload = self._get(f"/repos/{repository}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError("pull response was not an object")
        head = payload.get("head")
        user = payload.get("user")
        if not isinstance(head, dict) or not isinstance(user, dict):
            raise GitHubError("pull response was incomplete")
        sha = self._text(head.get("sha"))
        url = self._text(payload.get("html_url"))
        author = self._text(user.get("login"))
        state = self._text(payload.get("state"))
        if not all((sha, url, author, state)):
            raise GitHubError("pull response had invalid fields")
        return PullRequest(
            repository, number, state, bool(payload.get("merged_at")), sha, url, author,
            self._text(payload.get("title")) or "", self._text(payload.get("body")) or "",
        )

    def _can_push(self, repository: str) -> bool:
        payload = self._get(f"/repos/{repository}")
        permissions = payload.get("permissions") if isinstance(payload, dict) else None
        return isinstance(permissions, dict) and permissions.get("push") is True

    def _get_subject(self, value: str | None) -> dict[str, Any]:
        path = self._subject_path(value)
        if path is None:
            raise GitHubError("invalid subject URL")
        payload = self._get(path)
        if not isinstance(payload, dict):
            raise GitHubError("check response was not an object")
        return payload

    def _check_pull_number(self, payload: dict[str, Any]) -> int | None:
        pulls = payload.get("pull_requests")
        return next((pull["number"] for pull in pulls if isinstance(pull, dict) and isinstance(pull.get("number"), int) and pull["number"] > 0), None) if isinstance(pulls, list) else None

    def _latest_comment_body(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            payload = self._get(value)
        except GitHubError:
            return None
        return self._text(payload.get("body")) if isinstance(payload, dict) else None

    def _get(self, endpoint: str) -> Any:
        return self._json(self._call(["gh", "api", "--method", "GET", endpoint]))

    def _call(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(argv, check=True, capture_output=True, text=True, shell=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise GitHubError("gh command failed") from error

    @staticmethod
    def _json(result: subprocess.CompletedProcess[str]) -> Any:
        try:
            return json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise GitHubError("gh command returned invalid JSON") from error

    @staticmethod
    def _text(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _subject_path(value: str | None) -> str | None:
        if not value:
            return None
        if value.startswith("/repos/") and not any(character in value for character in "?#"):
            parts = value.split("/")[1:]
            if all(part and part not in {".", ".."} for part in parts):
                return value
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.query or parsed.fragment:
            return None
        return parsed.path

    @classmethod
    def _api_url(cls, value: Any) -> str | None:
        return cls._subject_path(value) if isinstance(value, str) else None

    @staticmethod
    def _comment_endpoint(repository: str, number: int, kind: str, comment_id: int) -> str | None:
        if not isinstance(comment_id, int) or comment_id < 1:
            return None
        endpoints = {
            "issue": f"/repos/{repository}/issues/comments/{comment_id}",
            "review": f"/repos/{repository}/pulls/comments/{comment_id}",
            "review_summary": f"/repos/{repository}/pulls/{number}/reviews/{comment_id}",
        }
        return endpoints.get(kind)
