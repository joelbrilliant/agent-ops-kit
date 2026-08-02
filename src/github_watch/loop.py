"""The linear GitHub Watch poll, decide, run, verify, mark and notify flow."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from .config import Config
from .github import GitHub, ResolvedNotification, StaleNotification
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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.github = github
        self.worker = worker
        self.state = state
        self.buzz_send = buzz_send or self._send_buzz
        self.now = now or (lambda: datetime.now(UTC))

    def run(self) -> RunResult:
        if not self.state.lock.acquire():
            return RunResult(locked=True)
        try:
            if self.state.is_paused():
                return RunResult(paused=True)
            self._drain_pending()
            processed = 0
            # GitHub normally returns newest first, but make that contract
            # explicit before coalescing. One live PR inspection handles all
            # notification rows already present for that PR in this poll.
            notifications = sorted(
                self.github.list_notifications(self.config.batch_limit),
                key=lambda item: item.updated_at,
                reverse=True,
            )
            handled_pulls: dict[tuple[str, int], StateRow] = {}
            for notification in notifications:
                if self._aged_out(notification.updated_at):
                    processed += 1
                    self.state.record_aged_out(notification)
                    self._drain_pending()
                    continue
                try:
                    item = self.github.resolve(notification)
                except Exception:
                    item = None
                if item is None:
                    if self.state.get(notification.notification_id, notification.updated_at) is None:
                        processed += 1
                        message = (
                            f"Oscar is blocked on {notification.repository} notification {notification.notification_id}: "
                            "pull request could not be resolved safely. Proposed fix: inspect the unread GitHub "
                            "notification and retry after its pull request is identifiable."
                        )
                        self.state.record_unresolved(notification, message)
                        self._drain_pending()
                    continue
                pull_key = (item.pull.repository.lower(), item.pull.number)
                superseding = self.state.superseding_for(item)
                if superseding is not None:
                    processed += 1
                    self.state.record_coalesced(item, superseding)
                    self._drain_pending()
                    handled_pulls.setdefault(pull_key, superseding)
                    continue
                terminal = self.state.terminal_for(item)
                if terminal:
                    handled_pulls.setdefault(pull_key, terminal)
                    continue
                prior = handled_pulls.get(pull_key)
                if prior is not None:
                    processed += 1
                    self.state.record_coalesced(item, prior)
                    self._drain_pending()
                    continue
                processed += 1
                if self._obsolete(item):
                    self._record_no_action(item)
                    self._remember(item, handled_pulls)
                    continue
                self.state.begin(item)
                try:
                    result = self.worker.run(item)
                except Exception:
                    result = WorkerResult.blocked("Oscar could not run", "inspect the pull request and retry the bounded session")
                self._record_worker_result(item, result)
                self._remember(item, handled_pulls)
            return RunResult(processed=processed)
        finally:
            self.state.lock.release()

    def _aged_out(self, updated_at: str) -> bool:
        try:
            timestamp = datetime.fromisoformat(updated_at)
        except (TypeError, ValueError):
            return False
        if timestamp.tzinfo is None:
            return False
        now = self.now()
        if now.tzinfo is None:
            return False
        cutoff = now.astimezone(UTC) - timedelta(
            hours=self.config.max_notification_age_hours,
        )
        return timestamp < cutoff

    def _obsolete(self, item: ResolvedNotification) -> bool:
        if item.pull.state == "closed" or item.pull.merged:
            return True
        try:
            return bool(
                item.notification.kind == "check" and item.source_head_sha
                and item.source_head_sha != item.pull.head_sha and self.github.head_is_green(item)
            )
        except Exception:
            return False

    def _record_no_action(self, item: ResolvedNotification) -> None:
        self.state.record(item, "no_action", item.pull.head_sha)
        self._drain_pending()

    def _remember(
        self,
        item: ResolvedNotification,
        handled_pulls: dict[tuple[str, int], StateRow],
    ) -> None:
        row = self.state.get(
            item.notification.notification_id,
            item.notification.updated_at,
        )
        if row and row.outcome in {"no_action", "completed", "blocked"}:
            handled_pulls[(item.pull.repository.lower(), item.pull.number)] = row

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
                    self.github.mark_read(row.notification_id, row.source_updated_at)
                except StaleNotification:
                    self.state.set_superseded(row)
                except Exception as error:
                    self.state.set_error(row, f"mark read failed: {type(error).__name__}")
                    continue
                else:
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
