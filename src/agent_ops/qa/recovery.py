"""Fail-closed helpers for one reviewer repair after a check failure."""

from __future__ import annotations

import re
from typing import List, Sequence

from agent_ops.contracts import CheckResultV1


_POSITIVE_EXIT = re.compile(r"^exit=[1-9][0-9]*$")


def initial_failure_is_repairable(
    checks: Sequence[CheckResultV1], *, expected_subject_ref: str
) -> bool:
    """Accept only normal positive-exit failures from the trusted verifier."""
    if not checks:
        return False
    check_ids = []
    held = False
    for check in checks:
        if (
            not isinstance(check.check_id, str)
            or not check.check_id
            or check.subject_ref != expected_subject_ref
            or not isinstance(check.status, str)
            or not isinstance(check.summary, str)
        ):
            return False
        check_ids.append(check.check_id)
        if check.status == "PASS":
            if check.summary != "exit=0":
                return False
        elif check.status == "HOLD":
            if not _POSITIVE_EXIT.fullmatch(check.summary):
                return False
            held = True
        else:
            return False
    return held and len(set(check_ids)) == len(check_ids)


def final_check_ids_match(
    initial_checks: Sequence[CheckResultV1], final_checks: Sequence[CheckResultV1]
) -> bool:
    """Require the rerun to cover precisely the original named checks."""
    return [check.check_id for check in final_checks] == [
        check.check_id for check in initial_checks
    ]


def recovery_markers(
    initial_checks: Sequence[CheckResultV1],
    final_checks: Sequence[CheckResultV1],
    *,
    initial_candidate_sha: str,
    final_reviewer_sha: str,
) -> List[CheckResultV1]:
    """Create deterministic receipt-only markers for checks repaired by review."""
    if (
        not initial_failure_is_repairable(
            initial_checks, expected_subject_ref=initial_candidate_sha
        )
        or not final_check_ids_match(initial_checks, final_checks)
        or any(
            check.subject_ref != final_reviewer_sha
            or check.status != "PASS"
            or check.summary != "exit=0"
            for check in final_checks
        )
    ):
        raise ValueError("recovery_evidence_invalid")
    return [
        CheckResultV1(
            check_id="recovery." + initial.check_id,
            subject_ref=final_reviewer_sha,
            status="PASS",
            summary="recovered_after_review",
            evidence_refs=[initial_candidate_sha, initial.summary, final.summary],
        )
        for initial, final in zip(initial_checks, final_checks)
        if initial.status == "HOLD"
    ]
