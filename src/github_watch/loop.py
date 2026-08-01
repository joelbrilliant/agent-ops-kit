"""The linear GitHub Watch poll, decide, run, verify, mark and notify flow."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .github import GitHub, ResolvedNotification
from .state import StateRow, StateStore
from .worker import OscarWorker, WorkerResult


@dataclass(frozen=True)
class RunResult:
    processed: int = 0
    locked: bool = False
    paused: bool = False


class WatchLoop:
    """Runs one serial poll while retaining only effects that still need retrying."""

    def __init__(
        self,
        config: Config,
        github: GitHub,
        worker: OscarWorker,
        state: StateStore,
        buzz_send: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.github = github
        self.worker = worker
        self.state = state
        self.buzz_send = buzz_send or self._send_buzz

    def run(self) -> RunResult:
        if not self.state.lock.acquire():
            return RunResult(locked=True)
        try:
            if self.state.is_paused():
                return RunResult(paused=True)
            self._drain_pending()
            processed = 0
            for notification in self.github.list_notifications(self.config.batch_limit):
                item = self.github.resolve(notification)
                if item is None or self.state.terminal_for(item):
                    continue
                processed += 1
                if self._obsolete(item):
                    self._record_no_action(item)
                    continue
                if not item.mutation_allowed and item.notification.latest_comment_url and not item.latest_comment_body:
                    self._record_blocked(
                        item,
                        "latest GitHub comment context could not be fetched",
                        "retry after the latest GitHub comment is available",
                    )
                    continue
                self.state.begin(item)
                try:
                    result = self.worker.run(item)
                except Exception:
                    result = WorkerResult.blocked("Oscar could not run", "inspect the pull request and retry the bounded session")
                self._record_worker_result(item, result)
            return RunResult(processed=processed)
        finally:
            self.state.lock.release()

    def _obsolete(self, item: ResolvedNotification) -> bool:
        if item.pull.state == "closed" or item.pull.merged:
            return True
        return bool(
            item.notification.kind == "check"
            and item.source_head_sha
            and item.source_head_sha != item.pull.head_sha
            and self.github.head_is_green(item)
        )

    def _record_no_action(self, item: ResolvedNotification) -> None:
        self.state.record(item, "no_action", item.pull.head_sha)
        self._drain_pending()

    def _record_worker_result(self, item: ResolvedNotification, result: WorkerResult) -> None:
        if result.outcome == "no_action":
            self.state.record(item, "no_action", item.pull.head_sha)
        elif result.outcome == "completed":
            if not item.mutation_allowed:
                self._record_blocked(
                    item,
                    "Oscar returned completed for read-only triage",
                    "review the pull request and take the required action manually",
                )
                return
            try:
                verified = self.github.verify_completion(item, result)
            except Exception:
                verified = False
            if verified:
                message = self._completed_message(item, result)
                self.state.record(item, "completed", result.head_sha or item.pull.head_sha, message)
            else:
                self._record_blocked(item, "completion verification failed", "inspect the remote head and GitHub reply before retrying")
                return
        else:
            self._record_blocked(
                item,
                _safe_text(result.blocker, "Oscar needs a manual check"),
                _safe_text(result.proposed_fix, "inspect the pull request and retry the bounded session"),
            )
            return
        self._drain_pending()

    def _record_blocked(self, item: ResolvedNotification, blocker: str, proposal: str) -> None:
        self.state.record(item, "blocked", item.pull.head_sha, self._blocked_message(item, blocker, proposal))
        self._drain_pending()

    def _drain_pending(self) -> None:
        for pending in self.state.pending():
            row = self.state.get(pending.notification_id, pending.source_updated_at) or pending
            if row.outcome in {"no_action", "completed"} and not row.read_completed:
                try:
                    self.github.mark_read(row.notification_id)
                except Exception as error:
                    self.state.set_error(row, f"mark read failed: {type(error).__name__}")
                    continue
                self.state.set_read(row)
                row = self.state.get(row.notification_id, row.source_updated_at) or row
            if row.outcome in {"completed", "blocked"} and not row.buzz_completed and row.message:
                try:
                    self.buzz_send(row.message)
                except Exception as error:
                    self.state.set_error(row, f"Buzz delivery failed: {type(error).__name__}")
                    continue
                self.state.set_buzz(row)

    @staticmethod
    def _completed_message(item: ResolvedNotification, result: WorkerResult) -> str:
        summary = _safe_text(result.summary, "routine pull request work completed")
        pull = item.pull
        return f"Oscar completed {pull.repository}#{pull.number}: {summary}. {pull.url}"

    @staticmethod
    def _blocked_message(item: ResolvedNotification, blocker: str, proposal: str) -> str:
        pull = item.pull
        return f"Oscar is blocked on {pull.repository}#{pull.number}: {blocker}. Proposed fix: {proposal}. {pull.url}"

    def _send_buzz(self, message: str) -> None:
        subprocess.run(
            [self.config.buzz_executable, message, self.config.buzz_channel],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )


def _safe_text(value: str | None, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    compact = " ".join(value.split())
    lowered = compact.lower()
    forbidden = ("/", "\\", "http", "token", "password", "secret", "bearer", "traceback", "exception", "stack")
    if not compact or len(compact) > 180 or any(word in lowered for word in forbidden):
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9 ,.():!?'&+-]", "", compact)
    return cleaned or fallback
