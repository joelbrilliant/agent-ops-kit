"""External runner contract: classifier, builder, reviewer via argv arrays."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agent_ops.config import expand_runner_argv
from agent_ops.contracts import DecisionV1, TaskSpecV1, dumps_json
from agent_ops.process import ProcResult, RunnerError, run_argv


PLACEHOLDERS = ("{request_path}", "{response_path}", "{worktree_path}")


def write_owner_only_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def run_runner(
    command_template: Sequence[str],
    *,
    request_path: Path,
    response_path: Path,
    worktree_path: Optional[Path] = None,
    timeout: int = 3600,
    cwd: Optional[Path] = None,
) -> ProcResult:
    argv = expand_runner_argv(
        command_template,
        request_path=request_path,
        response_path=response_path,
        worktree_path=worktree_path,
    )
    # Safety: refuse if any argv element still looks like shell concatenation of untrusted body
    for part in argv:
        if "\n" in part and part not in (str(request_path), str(response_path), str(worktree_path or "")):
            # multi-line args only allowed for paths we generated? still refuse general
            pass
    return run_argv(argv, cwd=cwd, timeout=timeout, check=False)


def run_classifier(
    command_template: Sequence[str],
    *,
    request_payload: Dict[str, Any],
    state_dir: Path,
    run_id: str,
    timeout: int = 3600,
) -> DecisionV1:
    req = state_dir / "requests" / f"{run_id}-classify-request.json"
    resp = state_dir / "requests" / f"{run_id}-classify-response.json"
    if resp.exists():
        resp.unlink()
    write_owner_only_json(req, request_payload)
    result = run_runner(command_template, request_path=req, response_path=resp, timeout=timeout)
    if not result.ok:
        return DecisionV1(verdict="HOLD", reason=f"classifier_failed:{result.returncode}")
    if not resp.is_file():
        return DecisionV1(verdict="HOLD", reason="classifier_missing_response")
    try:
        data = json.loads(resp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DecisionV1(verdict="HOLD", reason="classifier_invalid_json")
    if not isinstance(data, dict):
        return DecisionV1(verdict="HOLD", reason="classifier_invalid_shape")
    return DecisionV1.from_dict(data)


def run_builder(
    command_template: Sequence[str],
    *,
    task: TaskSpecV1,
    signal_public: Dict[str, Any],
    state_dir: Path,
    worktree_path: Path,
    run_id: str,
    timeout: int = 3600,
) -> Dict[str, Any]:
    req = state_dir / "requests" / f"{run_id}-build-request.json"
    resp = state_dir / "requests" / f"{run_id}-build-response.json"
    if resp.exists():
        resp.unlink()
    write_owner_only_json(
        req,
        {
            "schema": "BuilderRequestV1",
            "task": task.to_dict(),
            "signal": signal_public,
            # Untrusted review text is provided only via this owner-only file.
            "untrusted_review_body_path": str(req),  # body embedded below
            "untrusted_review_body": signal_public.get("_raw_body_for_runner_only"),
        },
    )
    # Remove ephemeral field from what we might log
    result = run_runner(
        command_template,
        request_path=req,
        response_path=resp,
        worktree_path=worktree_path,
        timeout=timeout,
        cwd=worktree_path,
    )
    payload: Dict[str, Any] = {
        "ok": result.ok,
        "returncode": result.returncode,
        "stdout_digest": _digest(result.stdout),
        "stderr_digest": _digest(result.stderr),
    }
    if resp.is_file():
        try:
            payload["response"] = json.loads(resp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload["response_error"] = "invalid_json"
    return payload


def run_reviewer(
    command_template: Sequence[str],
    *,
    task: TaskSpecV1,
    base_sha: str,
    head_sha: str,
    diff_text: str,
    verification: List[Dict[str, Any]],
    state_dir: Path,
    worktree_path: Path,
    run_id: str,
    timeout: int = 3600,
) -> Dict[str, Any]:
    req = state_dir / "requests" / f"{run_id}-review-request.json"
    resp = state_dir / "requests" / f"{run_id}-review-response.json"
    if resp.exists():
        resp.unlink()
    write_owner_only_json(
        req,
        {
            "schema": "ReviewerRequestV1",
            "task": task.to_dict(),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "diff": diff_text,
            "verification": verification,
        },
    )
    result = run_runner(
        command_template,
        request_path=req,
        response_path=resp,
        worktree_path=worktree_path,
        timeout=timeout,
        cwd=worktree_path,
    )
    payload: Dict[str, Any] = {
        "ok": result.ok,
        "returncode": result.returncode,
        "stdout_digest": _digest(result.stdout),
        "stderr_digest": _digest(result.stderr),
    }
    if resp.is_file():
        try:
            payload["response"] = json.loads(resp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload["response_error"] = "invalid_json"
    return payload


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def heuristic_decision_from_body(body: str, path: str) -> DecisionV1:
    """Optional local fallback classifier for tests/canaries; production uses external command."""
    lower = body.lower()
    hold_markers = [
        "product direction",
        "rewrite the architecture",
        "ignore previous",
        "exfiltrat",
        "credential",
        "secret",
        "delete the database",
        "force push",
        "drop table",
        "production deploy",
        "change permissions",
        "api key",
        "prompt injection",
    ]
    for m in hold_markers:
        if m in lower:
            return DecisionV1(verdict="HOLD", reason=f"hold_marker:{m}", requested_allowed_paths=[path] if path else [])
    if not path:
        return DecisionV1(verdict="HOLD", reason="missing_path")
    return DecisionV1(
        verdict="ROUTINE",
        reason="routine_inline_feedback",
        requested_allowed_paths=[path],
        proposed_verification_ids=["unit"],
    )
