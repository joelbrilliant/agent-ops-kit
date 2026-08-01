"""Ledger claim, concurrency, circuit breaker, reclaim."""

from __future__ import annotations

import time
from pathlib import Path

from agent_ops.maintenance.ledger import Ledger, make_claim_key


def test_claim_key_stable():
    k1 = make_claim_key("A/B", 1, "t", "c", "sha")
    k2 = make_claim_key("a/b", 1, "t", "c", "sha")
    assert k1 == k2


def test_atomic_duplicate_claim(tmp_path: Path):
    led = Ledger(tmp_path / "l.sqlite3")
    kwargs = dict(
        repository="o/r",
        pr_number=1,
        thread_node_id="t1",
        latest_comment_node_id="c1",
        observed_head_sha="deadbeef",
        signal_digest="d" * 64,
        reclaim_after_seconds=3600,
    )
    r1, j1, _ = led.try_claim(**kwargs)
    assert r1 == "claimed" and j1
    r2, _, detail = led.try_claim(**kwargs)
    assert r2 == "duplicate"
    assert detail in ("claimed", "running", "completed", "held")


def test_single_active_job(tmp_path: Path):
    led = Ledger(tmp_path / "l.sqlite3")
    r1, j1, _ = led.try_claim(
        repository="o/r",
        pr_number=1,
        thread_node_id="t1",
        latest_comment_node_id="c1",
        observed_head_sha="sha1",
        signal_digest="a" * 64,
        reclaim_after_seconds=3600,
    )
    assert r1 == "claimed"
    r2, _, detail = led.try_claim(
        repository="o/r",
        pr_number=2,
        thread_node_id="t2",
        latest_comment_node_id="c2",
        observed_head_sha="sha2",
        signal_digest="b" * 64,
        reclaim_after_seconds=3600,
    )
    assert r2 == "busy"
    assert detail == j1


def test_circuit_breaker_blocks_claims(tmp_path: Path):
    led = Ledger(tmp_path / "l.sqlite3")
    led.open_circuit("path_escape")
    assert led.circuit_open()
    r, _, reason = led.try_claim(
        repository="o/r",
        pr_number=1,
        thread_node_id="t",
        latest_comment_node_id="c",
        observed_head_sha="s",
        signal_digest="c" * 64,
        reclaim_after_seconds=3600,
    )
    assert r == "circuit_open"
    assert reason == "path_escape"
    led.clear_circuit()
    assert not led.circuit_open()
    r2, j2, _ = led.try_claim(
        repository="o/r",
        pr_number=1,
        thread_node_id="t",
        latest_comment_node_id="c",
        observed_head_sha="s",
        signal_digest="c" * 64,
        reclaim_after_seconds=3600,
    )
    assert r2 == "claimed" and j2


def test_reclaim_interrupted_job(tmp_path: Path):
    led = Ledger(tmp_path / "l.sqlite3")
    r1, j1, _ = led.try_claim(
        repository="o/r",
        pr_number=1,
        thread_node_id="t",
        latest_comment_node_id="c",
        observed_head_sha="s",
        signal_digest="d" * 64,
        reclaim_after_seconds=3600,
    )
    assert r1 == "claimed"
    # Age the heartbeat
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "l.sqlite3"))
    old = time.time() - 10_000
    conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE job_id = ?", (old, j1))
    conn.commit()
    conn.close()
    n = led.reclaim_stale_jobs(reclaim_after_seconds=60)
    assert n == 1
    assert led.active_job() is None
    # Reclaim same claim key allowed when interrupted
    r2, j2, detail = led.try_claim(
        repository="o/r",
        pr_number=1,
        thread_node_id="t",
        latest_comment_node_id="c",
        observed_head_sha="s",
        signal_digest="d" * 64,
        reclaim_after_seconds=60,
    )
    assert r2 == "claimed"
    assert detail == "reclaimed"
    assert j2 != j1
