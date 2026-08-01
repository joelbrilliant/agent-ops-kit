"""SQLite ledger: atomic claims, single active job, circuit breaker."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  claim_key TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  thread_node_id TEXT NOT NULL,
  latest_comment_node_id TEXT NOT NULL,
  observed_head_sha TEXT NOT NULL,
  signal_digest TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  job_id TEXT,
  hold_reason TEXT,
  resulting_sha TEXT,
  reply_node_id TEXT,
  outcome TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  claim_key TEXT NOT NULL,
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  heartbeat_at REAL NOT NULL,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_job ON jobs(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS notifications (
  thread_id TEXT PRIMARY KEY,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  repository TEXT,
  pr_number INTEGER,
  related_url TEXT,
  joel_summary TEXT,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_recorded ON notifications(recorded_at);
"""


_SAFE_RECLAIM_PHASES = {
    "claimed",
    "classifying",
    "preparing",
    "building",
    "verifying",
    "reviewing",
    "final_verifying",
    "pre_push",
}


def make_claim_key(
    repository: str,
    pr_number: int,
    thread_node_id: str,
    latest_comment_node_id: str,
    observed_head_sha: str,
) -> str:
    return "|".join(
        [
            repository.lower(),
            str(pr_number),
            thread_node_id,
            latest_comment_node_id,
            observed_head_sha,
        ]
    )


@dataclass
class ClaimRow:
    claim_key: str
    repository: str
    pr_number: int
    thread_node_id: str
    latest_comment_node_id: str
    observed_head_sha: str
    signal_digest: str
    status: str
    created_at: float
    updated_at: float
    job_id: Optional[str]
    hold_reason: Optional[str]
    resulting_sha: Optional[str]
    reply_node_id: Optional[str]
    outcome: Optional[str]


class Ledger:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                with self._connect() as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(SCHEMA)
                    columns = {
                        str(row["name"])
                        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
                    }
                    if "phase" not in columns:
                        conn.execute(
                            "ALTER TABLE jobs ADD COLUMN phase TEXT NOT NULL DEFAULT 'claimed'"
                        )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def circuit_open(self) -> bool:
        return self.get_meta("circuit_breaker", "closed") == "open"

    def open_circuit(self, reason: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("circuit_breaker", "open"),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("circuit_reason", reason),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("circuit_opened_at", str(time.time())),
            )

    def clear_circuit(self) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("circuit_breaker", "closed"),
            )
            conn.execute("DELETE FROM meta WHERE key IN ('circuit_reason', 'circuit_opened_at')")

    def circuit_reason(self) -> Optional[str]:
        return self.get_meta("circuit_reason")

    def _row_to_claim(self, row: sqlite3.Row) -> ClaimRow:
        return ClaimRow(
            claim_key=row["claim_key"],
            repository=row["repository"],
            pr_number=int(row["pr_number"]),
            thread_node_id=row["thread_node_id"],
            latest_comment_node_id=row["latest_comment_node_id"],
            observed_head_sha=row["observed_head_sha"],
            signal_digest=row["signal_digest"],
            status=row["status"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            job_id=row["job_id"],
            hold_reason=row["hold_reason"],
            resulting_sha=row["resulting_sha"],
            reply_node_id=row["reply_node_id"],
            outcome=row["outcome"],
        )

    def get_claim(self, claim_key: str) -> Optional[ClaimRow]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM claims WHERE claim_key = ?", (claim_key,)).fetchone()
            return self._row_to_claim(row) if row else None

    def active_job(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'active' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def reclaim_stale_jobs(self, reclaim_after_seconds: int) -> int:
        """Reclaim only pre-mutation jobs. Mutation-ambiguous jobs open the circuit."""
        now = time.time()
        cutoff = now - reclaim_after_seconds
        reclaimed = 0
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT job_id, claim_key, heartbeat_at, phase FROM jobs WHERE status = 'active'"
            ).fetchall()
            for row in rows:
                if float(row["heartbeat_at"]) > cutoff:
                    continue
                phase = str(row["phase"] or "claimed")
                if phase not in _SAFE_RECLAIM_PHASES:
                    reason = f"interrupted_after_mutation_boundary:{phase}"
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', phase = 'failed', updated_at = ?, detail = ? WHERE job_id = ?",
                        (now, reason, row["job_id"]),
                    )
                    conn.execute(
                        "UPDATE claims SET status = 'failed', updated_at = ?, hold_reason = ?, outcome = 'circuit' WHERE claim_key = ?",
                        (now, reason, row["claim_key"]),
                    )
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES('circuit_breaker', 'open') "
                        "ON CONFLICT(key) DO UPDATE SET value='open'"
                    )
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES('circuit_reason', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (reason,),
                    )
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES('circuit_opened_at', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(now),),
                    )
                    continue
                conn.execute(
                    "UPDATE jobs SET status = 'interrupted', phase = 'interrupted', updated_at = ?, detail = ? WHERE job_id = ?",
                    (now, "reclaimed_after_timeout", row["job_id"]),
                )
                conn.execute(
                    "UPDATE claims SET status = 'interrupted', updated_at = ?, hold_reason = ? WHERE claim_key = ?",
                    (now, "job_interrupted_reclaimable", row["claim_key"]),
                )
                reclaimed += 1
        return reclaimed

    def try_claim(
        self,
        *,
        repository: str,
        pr_number: int,
        thread_node_id: str,
        latest_comment_node_id: str,
        observed_head_sha: str,
        signal_digest: str,
        reclaim_after_seconds: int,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """
        Atomically claim work.

        Returns (result, job_id, detail) where result is:
          - claimed
          - duplicate
          - busy (another active job)
          - circuit_open
        """
        self.reclaim_stale_jobs(reclaim_after_seconds)

        claim_key = make_claim_key(
            repository, pr_number, thread_node_id, latest_comment_node_id, observed_head_sha
        )
        now = time.time()
        job_id = str(uuid.uuid4())

        with self._tx() as conn:
            circuit = conn.execute(
                "SELECT value FROM meta WHERE key = 'circuit_breaker'"
            ).fetchone()
            if circuit and circuit["value"] == "open":
                reason = conn.execute(
                    "SELECT value FROM meta WHERE key = 'circuit_reason'"
                ).fetchone()
                return "circuit_open", None, str(reason["value"]) if reason else None
            # Same claim key first - duplicate beats busy so retries are idempotent
            existing = conn.execute(
                "SELECT * FROM claims WHERE claim_key = ?", (claim_key,)
            ).fetchone()
            if existing:
                status = existing["status"]
                if status in ("completed", "claimed", "running", "held", "failed"):
                    return "duplicate", existing["job_id"], status
                # interrupted may be reclaimed into a new job if no active work
                if status == "interrupted":
                    active = conn.execute(
                        "SELECT job_id FROM jobs WHERE status = 'active' LIMIT 1"
                    ).fetchone()
                    if active:
                        return "busy", None, str(active["job_id"])
                    conn.execute(
                        "UPDATE claims SET status = 'claimed', updated_at = ?, job_id = ?, hold_reason = NULL WHERE claim_key = ?",
                        (now, job_id, claim_key),
                    )
                    conn.execute(
                        "INSERT INTO jobs(job_id, claim_key, status, phase, created_at, updated_at, heartbeat_at, detail) VALUES (?,?,?,?,?,?,?,?)",
                        (job_id, claim_key, "active", "claimed", now, now, now, "reclaimed"),
                    )
                    return "claimed", job_id, "reclaimed"

            # Only one active job globally for new claims
            active = conn.execute(
                "SELECT job_id FROM jobs WHERE status = 'active' LIMIT 1"
            ).fetchone()
            if active:
                return "busy", None, str(active["job_id"])

            conn.execute(
                """
                INSERT INTO claims(
                  claim_key, repository, pr_number, thread_node_id, latest_comment_node_id,
                  observed_head_sha, signal_digest, status, created_at, updated_at, job_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    claim_key,
                    repository,
                    pr_number,
                    thread_node_id,
                    latest_comment_node_id,
                    observed_head_sha,
                    signal_digest,
                    "claimed",
                    now,
                    now,
                    job_id,
                ),
            )
            conn.execute(
                "INSERT INTO jobs(job_id, claim_key, status, phase, created_at, updated_at, heartbeat_at, detail) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, claim_key, "active", "claimed", now, now, now, "claimed"),
            )
            return "claimed", job_id, None

    def heartbeat(self, job_id: str) -> None:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ? AND status = 'active'",
                (now, now, job_id),
            )

    def mark_phase(self, job_id: str, phase: str) -> None:
        if not phase or any(character.isspace() for character in phase):
            raise ValueError("invalid job phase")
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET phase = ?, heartbeat_at = ?, updated_at = ?, detail = ? "
                "WHERE job_id = ? AND status = 'active'",
                (phase, now, now, phase, job_id),
            )

    def mark_running(self, job_id: str, claim_key: str) -> None:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'active', phase = 'classifying', updated_at = ?, heartbeat_at = ?, detail = ? WHERE job_id = ?",
                (now, now, "classifying", job_id),
            )
            conn.execute(
                "UPDATE claims SET status = 'running', updated_at = ? WHERE claim_key = ?",
                (now, claim_key),
            )

    def complete_job(
        self,
        job_id: str,
        claim_key: str,
        *,
        outcome: str,
        resulting_sha: Optional[str] = None,
        reply_node_id: Optional[str] = None,
        hold_reason: Optional[str] = None,
    ) -> None:
        now = time.time()
        status = "completed" if outcome == "completed" else "held"
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, phase = ?, updated_at = ?, detail = ? WHERE job_id = ?",
                (outcome, outcome, now, outcome, job_id),
            )
            conn.execute(
                """
                UPDATE claims SET status = ?, updated_at = ?, resulting_sha = ?,
                  reply_node_id = ?, outcome = ?, hold_reason = ?
                WHERE claim_key = ?
                """,
                (status, now, resulting_sha, reply_node_id, outcome, hold_reason, claim_key),
            )

    def fail_job_open_circuit(
        self, job_id: Optional[str], claim_key: Optional[str], reason: str
    ) -> None:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("circuit_breaker", "open"),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("circuit_reason", reason),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("circuit_opened_at", str(now)),
            )
            if job_id:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', phase = 'failed', updated_at = ?, detail = ? WHERE job_id = ?",
                    (now, reason, job_id),
                )
            if claim_key:
                conn.execute(
                    "UPDATE claims SET status = 'failed', updated_at = ?, hold_reason = ?, outcome = ? WHERE claim_key = ?",
                    (now, reason, "circuit", claim_key),
                )

    def list_recent_claims(self, limit: int = 20) -> List[ClaimRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM claims ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_claim(r) for r in rows]

    def is_processed(self, claim_key: str) -> bool:
        row = self.get_claim(claim_key)
        if not row:
            return False
        return row.status in ("completed", "held", "claimed", "running", "failed")


    def is_notification_processed(self, notification_id: str) -> bool:
        return self.get_notification(notification_id) is not None

    def get_notification(self, thread_id: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notifications WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "thread_id": row["thread_id"],
                "decision": row["decision"],
                "reason": row["reason"],
                "updated_at": row["updated_at"],
                "related_repository": row["repository"] or "",
                "related_pr_number": int(row["pr_number"] or 0),
                "related_url": row["related_url"] or "",
                "joel_summary": row["joel_summary"] or "",
                "status": row["status"],
                "schema": "NotifyTriageDecisionV1",
                "mark_read": False,
            }

    def record_notification(
        self,
        *,
        thread_id: str,
        decision: str,
        reason: str,
        updated_at: str,
        repository: str = "",
        pr_number: int = 0,
        related_url: str = "",
        joel_summary: str = "",
        status: str = "processed",
    ) -> None:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO notifications(
                  thread_id, decision, reason, updated_at, repository, pr_number,
                  related_url, joel_summary, status, created_at, recorded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  decision=excluded.decision,
                  reason=excluded.reason,
                  updated_at=excluded.updated_at,
                  repository=excluded.repository,
                  pr_number=excluded.pr_number,
                  related_url=excluded.related_url,
                  joel_summary=excluded.joel_summary,
                  status=excluded.status,
                  recorded_at=excluded.recorded_at
                """,
                (
                    thread_id,
                    decision,
                    reason,
                    updated_at or "",
                    repository or None,
                    int(pr_number or 0) or None,
                    related_url or None,
                    joel_summary or None,
                    status,
                    now,
                    now,
                ),
            )
