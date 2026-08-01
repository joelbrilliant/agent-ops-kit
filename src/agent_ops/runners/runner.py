"""External runner contract: classifier, builder, reviewer via argv arrays."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agent_ops.config import RunnerIdentityPolicy, expand_runner_argv
from agent_ops.contracts import DecisionV1, TaskSpecV1
from agent_ops.github.client import GitHubClient
from agent_ops.process import ProcResult, RunnerError, run_argv


PLACEHOLDERS = ("{request_path}", "{response_path}", "{worktree_path}")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RunnerContractError(RunnerError):
    """A runner failed execution, schema or identity validation."""


@dataclass(frozen=True)
class RunnerIdentity:
    profile: str
    provider: str
    model: str
    reasoning_effort: str
    service_tier: str
    session_id: str
    fresh_session: bool


@dataclass(frozen=True)
class ClassificationResult:
    decision: DecisionV1
    continuation_token: str
    identity: RunnerIdentity


@dataclass(frozen=True)
class BuilderResult:
    identity: RunnerIdentity
    base_sha: str
    resulting_sha: str
    changed_paths: List[str]


@dataclass(frozen=True)
class ReviewerResult:
    identity: RunnerIdentity
    reviewed_sha: str
    resulting_sha: str
    verdict: str
    findings: List[Dict[str, Any]]
    fixes: List[str]
    reply_draft: str
    voice_gate: Dict[str, Any]


def write_owner_only_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def build_runner_environment(
    *,
    state_dir: Path,
    allowlist: Sequence[str],
    source_environment: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    source = source_environment or os.environ
    environment = {
        name: str(source[name])
        for name in allowlist
        if name in source and str(source[name])
    }
    environment.setdefault("PATH", os.defpath)
    home = state_dir / "runner-home"
    gh_config = home / "gh"
    xdg_config = home / "xdg"
    for directory in (home, gh_config, xdg_config):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    environment.update(
        {
            "HOME": str(home),
            "GH_CONFIG_DIR": str(gh_config),
            "XDG_CONFIG_HOME": str(xdg_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_ASKPASS": shutil.which("false") or "false",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "AGENT_OPS_CAPABILITY_ISOLATED": "1",
        }
    )
    return environment


def prove_github_capability_isolation(
    *,
    client: GitHubClient,
    operator_logins: Sequence[str],
    gh_command: str,
    runner_environment: Mapping[str, str],
) -> str:
    viewer = client.viewer_login().lower()
    if viewer not in {login.lower() for login in operator_logins}:
        raise RunnerContractError("orchestrator_identity_mismatch")
    isolated = run_argv(
        [gh_command, "auth", "status"],
        env=runner_environment,
        timeout=30,
        check=False,
    )
    if isolated.ok:
        raise RunnerContractError("runner_github_auth_available")
    return viewer


def _require_exact_keys(data: Dict[str, Any], expected: Sequence[str], label: str) -> None:
    expected_set = set(expected)
    if set(data) != expected_set:
        raise RunnerContractError(f"{label}_invalid_keys")


def _read_response(path: Path, schema: str, expected_keys: Sequence[str]) -> Dict[str, Any]:
    if not path.is_file():
        raise RunnerContractError(f"{schema}_missing_response")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerContractError(f"{schema}_invalid_json") from exc
    if not isinstance(data, dict):
        raise RunnerContractError(f"{schema}_invalid_shape")
    _require_exact_keys(data, expected_keys, schema)
    if data.get("schema") != schema:
        raise RunnerContractError(f"{schema}_schema_mismatch")
    return data


def _identity_from_dict(data: Any) -> RunnerIdentity:
    if not isinstance(data, dict):
        raise RunnerContractError("runner_identity_invalid_shape")
    _require_exact_keys(
        data,
        (
            "profile",
            "provider",
            "model",
            "reasoning_effort",
            "service_tier",
            "session_id",
            "fresh_session",
        ),
        "runner_identity",
    )
    if not isinstance(data.get("fresh_session"), bool):
        raise RunnerContractError("runner_identity_invalid_freshness")
    values = {
        key: str(data.get(key) or "").strip()
        for key in (
            "profile",
            "provider",
            "model",
            "reasoning_effort",
            "service_tier",
            "session_id",
        )
    }
    if not all(values.values()):
        raise RunnerContractError("runner_identity_missing_value")
    return RunnerIdentity(fresh_session=data["fresh_session"], **values)


def validate_runner_identity(
    identity: RunnerIdentity,
    policy: RunnerIdentityPolicy,
    *,
    fresh_session: bool,
    expected_session_id: Optional[str] = None,
    forbidden_session_id: Optional[str] = None,
) -> None:
    actual_route = (
        identity.profile,
        identity.provider,
        identity.model,
        identity.reasoning_effort,
        identity.service_tier,
    )
    required_route = (
        policy.profile,
        policy.provider,
        policy.model,
        policy.reasoning_effort,
        policy.service_tier,
    )
    if actual_route != required_route:
        raise RunnerContractError("runner_identity_route_mismatch")
    if identity.fresh_session is not fresh_session:
        raise RunnerContractError("runner_identity_freshness_mismatch")
    if expected_session_id and identity.session_id != expected_session_id:
        raise RunnerContractError("runner_identity_session_mismatch")
    if forbidden_session_id and identity.session_id == forbidden_session_id:
        raise RunnerContractError("reviewer_session_not_distinct")


def run_runner(
    command_template: Sequence[str],
    *,
    request_path: Path,
    response_path: Path,
    worktree_path: Optional[Path] = None,
    timeout: int = 3600,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ProcResult:
    argv = expand_runner_argv(
        command_template,
        request_path=request_path,
        response_path=response_path,
        worktree_path=worktree_path,
    )
    for part in argv:
        if any(control in part for control in ("\x00", "\n", "\r")):
            raise RunnerContractError("runner_argv_control_character")
        if any(placeholder in part for placeholder in PLACEHOLDERS):
            raise RunnerContractError("runner_argv_unexpanded_placeholder")
    return run_argv(argv, cwd=cwd, env=env, timeout=timeout, check=False)


def run_classifier(
    command_template: Sequence[str],
    *,
    request_payload: Dict[str, Any],
    state_dir: Path,
    run_id: str,
    required_identity: RunnerIdentityPolicy,
    runner_environment: Mapping[str, str],
    timeout: int = 3600,
) -> ClassificationResult:
    req = state_dir / "requests" / f"{run_id}-classify-request.json"
    resp = state_dir / "requests" / f"{run_id}-classify-response.json"
    if resp.exists():
        resp.unlink()
    write_owner_only_json(req, request_payload)
    result = run_runner(
        command_template,
        request_path=req,
        response_path=resp,
        timeout=timeout,
        env=runner_environment,
    )
    if not result.ok:
        raise RunnerContractError(f"classifier_failed:{result.returncode}")
    data = _read_response(
        resp,
        "ClassifierResponseV1",
        ("schema", "decision", "continuation_token", "runner_identity"),
    )
    decision_data = data.get("decision")
    if not isinstance(decision_data, dict):
        raise RunnerContractError("classifier_decision_invalid_shape")
    _require_exact_keys(
        decision_data,
        (
            "schema",
            "verdict",
            "reason",
            "requested_allowed_paths",
            "proposed_verification_ids",
        ),
        "DecisionV1",
    )
    if decision_data.get("schema") != "DecisionV1":
        raise RunnerContractError("classifier_decision_schema_mismatch")
    verdict = decision_data.get("verdict")
    reason = decision_data.get("reason")
    paths = decision_data.get("requested_allowed_paths")
    checks = decision_data.get("proposed_verification_ids")
    if verdict not in ("ROUTINE", "HOLD") or not isinstance(reason, str):
        raise RunnerContractError("classifier_decision_invalid_value")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise RunnerContractError("classifier_paths_invalid")
    if not isinstance(checks, list) or not all(isinstance(check, str) for check in checks):
        raise RunnerContractError("classifier_checks_invalid")
    token = data.get("continuation_token")
    if not isinstance(token, str) or len(token.strip()) < 8:
        raise RunnerContractError("classifier_continuation_token_missing")
    identity = _identity_from_dict(data.get("runner_identity"))
    validate_runner_identity(identity, required_identity, fresh_session=True)
    return ClassificationResult(
        decision=DecisionV1(
            verdict=verdict,
            reason=reason,
            requested_allowed_paths=list(paths),
            proposed_verification_ids=list(checks),
        ),
        continuation_token=token,
        identity=identity,
    )


def run_builder(
    command_template: Sequence[str],
    *,
    task: TaskSpecV1,
    signal_public: Dict[str, Any],
    state_dir: Path,
    worktree_path: Path,
    run_id: str,
    continuation_token: str,
    classifier_identity: RunnerIdentity,
    required_identity: RunnerIdentityPolicy,
    runner_environment: Mapping[str, str],
    timeout: int = 3600,
) -> BuilderResult:
    req = state_dir / "requests" / f"{run_id}-build-request.json"
    resp = state_dir / "requests" / f"{run_id}-build-response.json"
    if resp.exists():
        resp.unlink()
    runner_signal = dict(signal_public)
    untrusted_body = runner_signal.pop("_raw_body_for_runner_only", None)
    write_owner_only_json(
        req,
        {
            "schema": "BuilderRequestV1",
            "task": task.to_dict(),
            "signal": runner_signal,
            "untrusted_review_body": untrusted_body,
            "continuation_token": continuation_token,
            "expected_session_id": classifier_identity.session_id,
        },
    )
    result = run_runner(
        command_template,
        request_path=req,
        response_path=resp,
        worktree_path=worktree_path,
        timeout=timeout,
        cwd=worktree_path,
        env=runner_environment,
    )
    if not result.ok:
        raise RunnerContractError(f"builder_failed:{result.returncode}")
    data = _read_response(
        resp,
        "BuilderResponseV1",
        (
            "schema",
            "runner_identity",
            "base_sha",
            "resulting_sha",
            "changed_paths",
            "continuation_token_digest",
        ),
    )
    identity = _identity_from_dict(data.get("runner_identity"))
    validate_runner_identity(
        identity,
        required_identity,
        fresh_session=False,
        expected_session_id=classifier_identity.session_id,
    )
    if data.get("continuation_token_digest") != _digest(continuation_token):
        raise RunnerContractError("builder_continuation_token_mismatch")
    base_sha = str(data.get("base_sha") or "")
    resulting_sha = str(data.get("resulting_sha") or "")
    changed_paths = data.get("changed_paths")
    if base_sha != task.base_sha or not _SHA_RE.fullmatch(resulting_sha):
        raise RunnerContractError("builder_sha_binding_mismatch")
    if not isinstance(changed_paths, list) or not all(
        isinstance(path, str) for path in changed_paths
    ):
        raise RunnerContractError("builder_changed_paths_invalid")
    return BuilderResult(
        identity=identity,
        base_sha=base_sha,
        resulting_sha=resulting_sha,
        changed_paths=list(changed_paths),
    )


def run_reviewer(
    command_template: Sequence[str],
    *,
    task: TaskSpecV1,
    base_sha: str,
    candidate_sha: str,
    diff_text: str,
    verification: List[Dict[str, Any]],
    state_dir: Path,
    worktree_path: Path,
    run_id: str,
    classifier_identity: RunnerIdentity,
    required_identity: RunnerIdentityPolicy,
    runner_environment: Mapping[str, str],
    timeout: int = 3600,
) -> ReviewerResult:
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
            "candidate_sha": candidate_sha,
            "diff": diff_text,
            "verification": verification,
            "required_reply_register": "public-community-short-reply",
        },
    )
    result = run_runner(
        command_template,
        request_path=req,
        response_path=resp,
        worktree_path=worktree_path,
        timeout=timeout,
        cwd=worktree_path,
        env=runner_environment,
    )
    if not result.ok:
        raise RunnerContractError(f"reviewer_failed:{result.returncode}")
    data = _read_response(
        resp,
        "ReviewerResponseV1",
        (
            "schema",
            "runner_identity",
            "reviewed_sha",
            "resulting_sha",
            "verdict",
            "findings",
            "fixes",
            "reply_draft",
            "voice_gate",
        ),
    )
    identity = _identity_from_dict(data.get("runner_identity"))
    validate_runner_identity(
        identity,
        required_identity,
        fresh_session=True,
        forbidden_session_id=classifier_identity.session_id,
    )
    reviewed_sha = str(data.get("reviewed_sha") or "")
    resulting_sha = str(data.get("resulting_sha") or "")
    verdict = str(data.get("verdict") or "")
    findings = data.get("findings")
    fixes = data.get("fixes")
    reply_draft = data.get("reply_draft")
    voice_gate = data.get("voice_gate")
    if reviewed_sha != candidate_sha or not _SHA_RE.fullmatch(resulting_sha):
        raise RunnerContractError("reviewer_sha_binding_mismatch")
    if verdict not in ("PASS", "HOLD"):
        raise RunnerContractError("reviewer_verdict_invalid")
    if not isinstance(findings, list) or not all(
        isinstance(finding, dict) for finding in findings
    ):
        raise RunnerContractError("reviewer_findings_invalid")
    if not isinstance(fixes, list) or not all(isinstance(fix, str) for fix in fixes):
        raise RunnerContractError("reviewer_fixes_invalid")
    if verdict == "PASS" and findings:
        raise RunnerContractError("reviewer_pass_with_unresolved_findings")
    if not isinstance(reply_draft, str) or not reply_draft.strip():
        raise RunnerContractError("reviewer_reply_draft_missing")
    if not isinstance(voice_gate, dict):
        raise RunnerContractError("reviewer_voice_gate_invalid")
    _require_exact_keys(
        voice_gate,
        (
            "schema",
            "shared_operator_contract_read",
            "operator_profile_read",
            "skill",
            "reference",
            "register",
            "passed",
        ),
        "VoiceGateV1",
    )
    if voice_gate != {
        "schema": "VoiceGateV1",
        "shared_operator_contract_read": True,
        "operator_profile_read": True,
        "skill": "joel-voice-writing",
        "reference": "references/voice.md",
        "register": "public-community-short-reply",
        "passed": True,
    }:
        raise RunnerContractError("reviewer_voice_gate_failed")
    return ReviewerResult(
        identity=identity,
        reviewed_sha=reviewed_sha,
        resulting_sha=resulting_sha,
        verdict=verdict,
        findings=list(findings),
        fixes=list(fixes),
        reply_draft=reply_draft,
        voice_gate=dict(voice_gate),
    )


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
