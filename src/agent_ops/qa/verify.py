"""Verification command execution (named argv arrays)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Mapping, Optional, Sequence

from agent_ops.contracts import CheckResultV1
from agent_ops.process import run_argv


def run_named_verifications(
    commands: Mapping[str, Sequence[str]],
    *,
    cwd: Path,
    subject_ref: str,
    timeout: int = 1800,
    env: Optional[Mapping[str, str]] = None,
) -> List[CheckResultV1]:
    results: List[CheckResultV1] = []
    for name, argv in commands.items():
        if not argv:
            results.append(
                CheckResultV1(
                    check_id=name,
                    subject_ref=subject_ref,
                    status="HOLD",
                    summary="empty verification argv",
                )
            )
            continue
        proc = run_argv(
            list(argv), cwd=cwd, env=env, timeout=timeout, check=False
        )
        status = "PASS" if proc.ok else "HOLD"
        summary = f"exit={proc.returncode}"
        results.append(
            CheckResultV1(
                check_id=name,
                subject_ref=subject_ref,
                status=status,
                summary=summary,
                evidence_refs=[f"stdout_len={len(proc.stdout)}", f"stderr_len={len(proc.stderr)}"],
            )
        )
    return results


def all_passed(checks: Sequence[CheckResultV1]) -> bool:
    return bool(checks) and all(c.status == "PASS" for c in checks)
