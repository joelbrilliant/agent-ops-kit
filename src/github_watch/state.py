"""One-table SQLite state and one OS lock for GitHub Watch."""

from __future__ import annotations

import fcntl
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .github import Notification, ResolvedNotification


@dataclass(frozen=True)
class StateRow:
    notification_id: str
    source_updated_at: str
    repository: str
    pull_number: int
    head_sha: str
    outcome: str | None
    read_completed: bool
    buzz_completed: bool
    message: str | None
    last_error: str | None
    updated_at: str


class RunLock:
    """An advisory lock that the operating system releases on a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class StateStore:
    """Stores every notification update in a single SQLite table."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(directory / "github-watch.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.lock = RunLock(directory / "github-watch.lock")
        self._create_table()

    def get(self, notification_id: str, updated_at: str) -> StateRow | None:
        row = self.connection.execute(
            "SELECT * FROM notifications WHERE notification_id = ? AND source_updated_at = ?",
            (notification_id, updated_at),
        ).fetchone()
        return self._row(row) if row else None

    def terminal_for(self, item: ResolvedNotification) -> StateRow | None:
        row = self.get(item.notification.notification_id, item.notification.updated_at)
        if row and row.outcome in {"no_action", "completed", "blocked"} and row.head_sha == item.pull.head_sha:
            return row
        return None

    def begin(self, item: ResolvedNotification) -> None:
        self.connection.execute(
            """
            INSERT INTO notifications
            (notification_id, source_updated_at, repository, pull_number, head_sha, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(notification_id, source_updated_at) DO UPDATE SET
              repository = excluded.repository,
              pull_number = excluded.pull_number,
              head_sha = excluded.head_sha,
              outcome = NULL,
              read_completed = 0,
              buzz_completed = 0,
              message = NULL,
              last_error = NULL,
              updated_at = excluded.updated_at
            WHERE notifications.head_sha != excluded.head_sha
            """,
            self._values(item) + (self._now(),),
        )
        self.connection.commit()

    def record(self, item: ResolvedNotification, outcome: str, head_sha: str, message: str | None = None) -> None:
        self.connection.execute(
            """
            INSERT INTO notifications
            (notification_id, source_updated_at, repository, pull_number, head_sha, outcome, message, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notification_id, source_updated_at) DO UPDATE SET
              repository = excluded.repository,
              pull_number = excluded.pull_number,
              head_sha = excluded.head_sha,
              outcome = excluded.outcome,
              read_completed = CASE WHEN notifications.head_sha != excluded.head_sha THEN 0 ELSE notifications.read_completed END,
              buzz_completed = CASE WHEN notifications.head_sha != excluded.head_sha THEN 0 ELSE notifications.buzz_completed END,
              message = excluded.message,
              last_error = NULL,
              updated_at = excluded.updated_at
            """,
            self._identity(item) + (head_sha, outcome, message, self._now()),
        )
        self.connection.commit()

    def pending(self) -> list[StateRow]:
        rows = self.connection.execute(
            """
            SELECT * FROM notifications
            WHERE (outcome IN ('no_action', 'completed') AND read_completed = 0)
               OR (outcome IN ('completed', 'blocked') AND buzz_completed = 0)
            ORDER BY updated_at
            """
        ).fetchall()
        return [self._row(row) for row in rows]

    def set_read(self, row: StateRow) -> None:
        self._set(row, "read_completed = 1, last_error = NULL")

    def set_buzz(self, row: StateRow) -> None:
        self._set(row, "buzz_completed = 1, last_error = NULL")

    def set_error(self, row: StateRow, error: str) -> None:
        self._set(row, "last_error = ?", (error[:240],))

    def rows(self) -> list[StateRow]:
        return [self._row(row) for row in self.connection.execute("SELECT * FROM notifications ORDER BY updated_at").fetchall()]

    def is_paused(self) -> bool:
        return (self.directory / "paused").exists()

    def pause(self) -> None:
        (self.directory / "paused").write_text("paused\n", encoding="utf-8")

    def resume(self) -> None:
        (self.directory / "paused").unlink(missing_ok=True)

    def _create_table(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
              notification_id TEXT NOT NULL,
              source_updated_at TEXT NOT NULL,
              repository TEXT NOT NULL,
              pull_number INTEGER NOT NULL,
              head_sha TEXT NOT NULL,
              outcome TEXT,
              read_completed INTEGER NOT NULL DEFAULT 0,
              buzz_completed INTEGER NOT NULL DEFAULT 0,
              message TEXT,
              last_error TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (notification_id, source_updated_at)
            )
            """
        )
        self.connection.commit()

    def _set(self, row: StateRow, assignment: str, values: tuple[object, ...] = ()) -> None:
        self.connection.execute(
            f"UPDATE notifications SET {assignment}, updated_at = ? WHERE notification_id = ? AND source_updated_at = ?",
            values + (self._now(), row.notification_id, row.source_updated_at),
        )
        self.connection.commit()

    @staticmethod
    def _values(item: ResolvedNotification) -> tuple[str, str, str, int, str]:
        return StateStore._identity(item) + (item.pull.head_sha,)

    @staticmethod
    def _identity(item: ResolvedNotification) -> tuple[str, str, str, int]:
        pull = item.pull
        notification = item.notification
        return notification.notification_id, notification.updated_at, pull.repository, pull.number

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _row(row: sqlite3.Row) -> StateRow:
        return StateRow(
            notification_id=row["notification_id"],
            source_updated_at=row["source_updated_at"],
            repository=row["repository"],
            pull_number=row["pull_number"],
            head_sha=row["head_sha"],
            outcome=row["outcome"],
            read_completed=bool(row["read_completed"]),
            buzz_completed=bool(row["buzz_completed"]),
            message=row["message"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )
