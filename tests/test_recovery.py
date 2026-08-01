"""Unit coverage for the strict failed-check recovery boundary."""

import pytest

from agent_ops.audit.receipts import write_receipt
from agent_ops.audit.report import build_audit_report, render_audit_report
from agent_ops.contracts import ActionReceiptV1, CheckResultV1
from tests.conftest import make_config

from agent_ops.qa.recovery import (
    final_check_ids_match,
    initial_failure_is_repairable,
    recovery_markers,
)


def _check(check_id: str, status: str, summary: str) -> CheckResultV1:
    return CheckResultV1(check_id, "a" * 40, status, summary)


def test_recovery_rejects_empty_malformed_unknown_and_duplicate_initial_results():
    repairable = lambda checks: initial_failure_is_repairable(
        checks, expected_subject_ref="a" * 40
    )
    assert not repairable([])
    assert not repairable([_check("unit", "HOLD", "empty verification argv")])
    assert not repairable([_check("unit", "HOLD", "exit=0")])
    assert not repairable([_check("unit", "HOLD", "exit=-1")])
    assert not repairable([_check("unit", "SKIP", "exit=1")])
    assert not repairable([_check("unit", "HOLD", "exit=1"), _check("unit", "PASS", "exit=0")])
    wrong_subject = _check("unit", "HOLD", "exit=1")
    wrong_subject.subject_ref = "b" * 40
    assert not repairable([wrong_subject])


def test_recovery_rejects_unsafe_reserved_and_colliding_check_ids():
    repairable = lambda checks: initial_failure_is_repairable(
        checks, expected_subject_ref="a" * 40
    )
    assert not repairable([_check("unit check", "HOLD", "exit=1")])
    assert not repairable([_check("recovery.unit", "HOLD", "exit=1")])
    assert not repairable([_check("Recovery.unit", "HOLD", "exit=1")])
    assert not repairable([_check("u" * 120, "HOLD", "exit=1")])

    initial = [
        _check("unit", "HOLD", "exit=1"),
        _check("recovery.unit", "PASS", "exit=0"),
    ]
    final = [
        _check("unit", "PASS", "exit=0"),
        _check("recovery.unit", "PASS", "exit=0"),
    ]
    for check in final:
        check.subject_ref = "b" * 40
    with pytest.raises(ValueError, match="recovery_evidence_invalid"):
        recovery_markers(
            initial,
            final,
            initial_candidate_sha="a" * 40,
            final_reviewer_sha="b" * 40,
        )


def test_recovery_markers_require_exact_final_ids_and_public_safe_evidence():
    initial = [_check("unit", "HOLD", "exit=2"), _check("package", "PASS", "exit=0")]
    final = [_check("unit", "PASS", "exit=0"), _check("package", "PASS", "exit=0")]
    for check in final:
        check.subject_ref = "b" * 40
    assert final_check_ids_match(initial, final)
    markers = recovery_markers(
        initial,
        final,
        initial_candidate_sha="a" * 40,
        final_reviewer_sha="b" * 40,
    )
    assert [marker.to_dict() for marker in markers] == [{
        "schema": "CheckResultV1",
        "check_id": "recovery.unit",
        "subject_ref": "b" * 40,
        "status": "PASS",
        "summary": "recovered_after_review",
        "evidence_refs": ["a" * 40, "exit=2", "exit=0"],
    }]
    assert not final_check_ids_match(initial, list(reversed(final)))


def test_recovery_receipt_is_public_safe_and_audit_valid(tmp_path):
    cfg = make_config(tmp_path)
    receipt = ActionReceiptV1(
        signal_digest="a" * 64,
        repository="operator/demo",
        pr_number=1,
        thread_node_id="thread-1",
        base_sha="b" * 40,
        resulting_sha="c" * 40,
        named_checks=["unit", "recovery.unit"],
        reply_node_id="reply-1",
        outcome="completed",
    )
    path = write_receipt(cfg.state_dir, receipt, cfg.private_markers)

    report = build_audit_report(cfg)
    rendered = render_audit_report(report, "json")

    assert report.verdict == "PASS"
    assert report.items[0]["named_checks"] == ["recovery.unit", "unit"]
    assert path.exists()
    for forbidden in ("exit=2", "recovered_after_review", "stdout", "command", "/tmp", "private-marker"):
        assert forbidden not in rendered
