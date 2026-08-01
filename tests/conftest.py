from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from github_watch.config import Config
from github_watch.github import Notification, PullRequest, ResolvedNotification
from github_watch.worker import WorkerResult


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        github_login="joelbrilliant",
        allowed_namespaces=("acme",),
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        oscar_command="oscar",
        oscar_timeout_seconds=10,
        buzz_channel="00000000-0000-0000-0000-000000000000",
        buzz_executable="buzz",
        batch_limit=20,
    )


def note(
    notification_id: str = "thread-1",
    updated_at: str = "2026-08-02T00:00:00Z",
    kind: str = "review",
) -> Notification:
    return Notification(
        notification_id=notification_id,
        updated_at=updated_at,
        repository="acme/widget",
        pull_number=7,
        subject_type="PullRequest",
        reason="review_requested",
        kind=kind,
        source_head_sha=None,
    )


def resolved(
    item: Notification,
    state: str = "open",
    merged: bool = False,
    head_sha: str = "head-1",
    mutation_allowed: bool = True,
    latest_comment_body: str | None = None,
) -> ResolvedNotification:
    pull = PullRequest(
        repository="acme/widget",
        number=7,
        state=state,
        merged=merged,
        head_sha=head_sha,
        url="https://github.com/acme/widget/pull/7",
        author="joelbrilliant",
    )
    return ResolvedNotification(
        notification=item,
        pull=pull,
        mutation_allowed=mutation_allowed,
        latest_comment_body=latest_comment_body,
    )


class FakeGitHub:
    def __init__(self, notifications, resolutions, *, green: bool = False, verification: bool = True):
        self.notifications = list(notifications)
        self.resolutions = dict(resolutions)
        self.green = green
        self.verification = verification
        self.calls: list[tuple[str, str]] = []
        self.mark_failures = 0

    def list_notifications(self, limit: int):
        self.calls.append(("list", str(limit)))
        return self.notifications[:limit]

    def resolve(self, item: Notification):
        self.calls.append(("resolve", item.notification_id))
        return self.resolutions.get(item.notification_id)

    def head_is_green(self, item: ResolvedNotification) -> bool:
        self.calls.append(("green", item.notification.notification_id))
        return self.green

    def mark_read(self, notification_id: str) -> None:
        self.calls.append(("mark", notification_id))
        if self.mark_failures:
            self.mark_failures -= 1
            raise RuntimeError("mark read failed")

    def verify_completion(self, item: ResolvedNotification, result: WorkerResult) -> bool:
        self.calls.append(("verify", item.notification.notification_id))
        if self.verification and result.head_sha:
            previous = self.resolutions[item.notification.notification_id]
            self.resolutions[item.notification.notification_id] = ResolvedNotification(
                previous.notification,
                replace(previous.pull, head_sha=result.head_sha),
                previous.source_head_sha,
                previous.mutation_allowed,
                previous.latest_comment_body,
            )
        return self.verification


class FakeWorker:
    def __init__(self, *results: WorkerResult):
        self.results = list(results)
        self.calls: list[str] = []

    def run(self, item: ResolvedNotification) -> WorkerResult:
        self.calls.append(item.notification.notification_id)
        return self.results.pop(0)


class FakeBuzz:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.failures = 0

    def __call__(self, message: str) -> None:
        self.messages.append(message)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("buzz failed")
