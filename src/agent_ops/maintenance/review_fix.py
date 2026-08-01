"""Packet-v2 reviewer-fix sweep with fail-closed mutation gates."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_ops.audit.receipts import write_receipt
from agent_ops.audit.redaction import assert_no_private_material, redact_text
from agent_ops.config import Config, ensure_state_dirs
from agent_ops.contracts import (
    ActionReceiptV1,
    CheckResultV1,
    DecisionV1,
    SignalV1,
    TaskSpecV1,
    signal_digest,
)
from agent_ops.github.client import GhClient, GitHubClient, GitHubError
from agent_ops.github.discovery import (
    DiscoverSkip,
    discover_actionable_signals,
    fetch_pr_threads,
)
from agent_ops.github.reply import (
    readback_thread,
    reply_on_thread,
    thread_still_actionable,
    verify_reply_present,
)
from agent_ops.maintenance.ledger import Ledger, make_claim_key
from agent_ops.maintenance.worktree import (
    base_is_ancestor,
    changed_files,
    create_worktree,
    current_head,
    diff_text,
    push_head_no_force,
    remote_ref_sha,
    remove_worktree,
    validate_path_bounds,
    worktree_is_clean,
)
from agent_ops.paths import (
    is_safe_repo_path,
    normalize_repo_path,
    is_path_allowed,
)
from agent_ops.process import RunnerError
from agent_ops.qa.verify import all_passed, run_named_verifications
from agent_ops.runners.runner import (
    RunnerContractError,
    build_runner_environment,
    prove_github_capability_isolation,
    run_builder,
    run_classifier,
    run_reviewer,
)


@dataclass
class InspectOutcome:
    inspected_prs: int
    signals: List[SignalV1]
    skips: List[DiscoverSkip]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "inspected_prs": self.inspected_prs,
            "signals": [signal.to_public_dict() for signal in self.signals],
            "skips": [skip.__dict__ for skip in self.skips],
        }


@dataclass
class SweepOutcome:
    exit_code: int
    message: str
    inspected_prs: int = 0
    signals_found: int = 0
    receipt_path: Optional[Path] = None
    receipt_paths: List[Path] = field(default_factory=list)
    jobs_completed: int = 0
    jobs_held: int = 0


@dataclass
class JobOutcome:
    completed: bool
    circuit_open: bool
    message: str
    receipt_path: Path
    resulting_sha: Optional[str] = None


def _claim_key(signal: SignalV1) -> str:
    return make_claim_key(
        signal.repository,
        signal.pr_number,
        signal.thread_node_id,
        signal.latest_comment_node_id,
        signal.observed_head_sha,
    )


def inspect_work(config: Config, client: Optional[GitHubClient] = None) -> InspectOutcome:
    ensure_state_dirs(config)
    github = client or GhClient(config.gh_command)
    discovered = discover_actionable_signals(
        github,
        operator_logins=config.operator_logins,
        owned_namespaces=config.owned_namespaces,
        trusted_reviewer_logins=config.trusted_reviewer_logins,
        trusted_reviewer_associations=config.trusted_reviewer_associations,
        excluded_repositories=config.excluded_repositories,
    )
    ledger = Ledger(config.state_dir / "ledger.sqlite3")
    skips = list(discovered.skips)
    pending: List[SignalV1] = []
    for signal in discovered.signals:
        if ledger.is_processed(_claim_key(signal)):
            skips.append(
                DiscoverSkip(
                    signal.repository,
                    signal.pr_number,
                    "already_processed",
                    signal.thread_node_id,
                )
            )
        else:
            pending.append(signal)
    return InspectOutcome(
        inspected_prs=len(discovered.inspected_prs), signals=pending, skips=skips
    )


def status(config: Config) -> Dict[str, Any]:
    ensure_state_dirs(config)
    ledger = Ledger(config.state_dir / "ledger.sqlite3")
    active = ledger.active_job()
    return {
        "circuit_open": ledger.circuit_open(),
        "circuit_reason": ledger.circuit_reason(),
        "paused": (config.state_dir / config.pause_file_name).exists(),
        "pause_file": str(config.state_dir / config.pause_file_name),
        "active_jobs": [active] if active else [],
        "claims": [claim.__dict__ for claim in ledger.list_recent_claims(limit=20)],
    }


def _pr_snapshot_matches(pr: Dict[str, Any], signal: SignalV1) -> bool:
    return (
        str(pr.get("headRefOid") or "") == signal.observed_head_sha
        and str(pr.get("headRefName") or "") == signal.head_ref
        and str((pr.get("headRepository") or {}).get("nameWithOwner") or "").lower()
        == signal.head_repository.lower()
        and str((pr.get("baseRepository") or {}).get("nameWithOwner") or "").lower()
        == signal.repository.lower()
        and bool(str(pr.get("baseRefName") or ""))
    )


def _can_push_head_repository(
    client: GitHubClient, head_repository: str, operator_logins: Sequence[str]
) -> bool:
    data = client.rest_get(f"/repos/{head_repository}")
    permissions = data.get("permissions") or {}
    push = bool(permissions.get("push") or permissions.get("maintain") or permissions.get("admin"))
    owner = str((data.get("owner") or {}).get("login") or "").lower()
    return push and (not owner or owner in {login.lower() for login in operator_logins})


def _required_check_snapshot(rows: Sequence[Dict[str, Any]]) -> Tuple[Tuple[str, str, str], ...]:
    snapshot: List[Tuple[str, str, str]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        bucket = str(row.get("bucket") or "").strip().lower()
        state = str(row.get("state") or "").strip().upper()
        if not name or not bucket or not state:
            raise RunnerContractError("required_check_contract_incomplete")
        snapshot.append((name, bucket, state))
    return tuple(sorted(snapshot))


def _required_checks_green(snapshot: Sequence[Tuple[str, str, str]]) -> bool:
    return all(
        bucket == "pass" and state in {"SUCCESS", "PASS", "COMPLETED"}
        for _, bucket, state in snapshot
    )


def _select_verifications(
    config: Config, repository: str, decision: DecisionV1
) -> Dict[str, List[str]]:
    available: Dict[str, List[str]] = dict(config.default_verification_commands)
    policy = config.policy_for(repository)
    if policy:
        available.update(policy.verification_commands)
    requested = list(decision.proposed_verification_ids)
    unknown = [check_id for check_id in requested if check_id not in available]
    if unknown:
        raise RunnerContractError("classifier_requested_unknown_verification")
    selected_ids: List[str] = []
    for check_id in list(available) + requested:
        if check_id not in selected_ids:
            selected_ids.append(check_id)
    if not selected_ids:
        raise RunnerContractError("verification_evidence_missing")
    return {check_id: available[check_id] for check_id in selected_ids}


def _allowed_paths(config: Config, signal: SignalV1, decision: DecisionV1) -> List[str]:
    signal_path = normalize_repo_path(signal.path)
    if not is_safe_repo_path(signal_path):
        raise RunnerContractError("review_path_unsafe")
    requested = [normalize_repo_path(path) for path in decision.requested_allowed_paths]
    if not requested or any(not is_safe_repo_path(path) for path in requested):
        raise RunnerContractError("classifier_path_unsafe")
    if any(path != signal_path for path in requested):
        raise RunnerContractError("classifier_scope_expansion")
    policy = config.policy_for(signal.repository)
    permitted = policy.permitted_paths if policy else []
    if not permitted or not is_path_allowed(signal_path, permitted):
        raise RunnerContractError("review_path_not_allowed_by_policy")
    return [signal_path]


def _validate_local_candidate(
    config: Config,
    worktree: Path,
    base_sha: str,
    allowed_paths: Sequence[str],
    declared_paths: Optional[Sequence[str]] = None,
) -> Tuple[str, List[str]]:
    head = current_head(config.git_command, worktree)
    if not worktree_is_clean(config.git_command, worktree):
        raise RunnerContractError("runner_left_dirty_worktree")
    if not base_is_ancestor(config.git_command, worktree, base_sha):
        raise RunnerContractError("history_integrity_failed")
    changed = changed_files(config.git_command, worktree, base_sha)
    if not changed:
        raise RunnerContractError("runner_produced_no_change")
    if declared_paths is not None and sorted(changed) != sorted(declared_paths):
        raise RunnerContractError("runner_changed_path_attestation_mismatch")
    paths_ok, disallowed = validate_path_bounds(
        changed,
        allowed=allowed_paths,
        protected=config.protected_path_patterns,
    )
    if not paths_ok:
        raise RunnerContractError("disallowed_changes:" + ",".join(sorted(disallowed)))
    return head, changed


def _validate_reply_draft(
    draft: str,
    resulting_sha: str,
    check_ids: Sequence[str],
    private_markers: Sequence[str],
) -> None:
    lowered = draft.lower()
    forbidden = (
        "openai-codex",
        "gpt-",
        "reasoning_effort",
        "service_tier",
        "session_id",
        "runner_identity",
        "worktree",
    )
    if resulting_sha not in draft:
        raise RunnerContractError("reply_draft_missing_resulting_sha")
    if any(check_id not in draft for check_id in check_ids):
        raise RunnerContractError("reply_draft_missing_named_check")
    if any(marker in lowered for marker in forbidden):
        raise RunnerContractError("reply_draft_contains_model_internals")
    findings = assert_no_private_material(draft, private_markers)
    if findings:
        raise RunnerContractError("reply_draft_privacy_failure:" + ",".join(findings))


def _receipt(
    config: Config,
    *,
    signal: SignalV1,
    outcome: str,
    checks: Sequence[CheckResultV1],
    resulting_sha: Optional[str],
    hold_reason: Optional[str],
    reply_node_id: Optional[str] = None,
) -> Path:
    receipt = ActionReceiptV1(
        signal_digest=signal_digest(signal),
        base_sha=signal.observed_head_sha,
        resulting_sha=resulting_sha or "",
        named_checks=[check.check_id for check in checks],
        reply_node_id=reply_node_id,
        outcome=outcome,
        repository=signal.repository,
        pr_number=signal.pr_number,
        thread_node_id=signal.thread_node_id,
        hold_reason=redact_text(hold_reason or "", config.private_markers) or None,
    )
    return write_receipt(config.state_dir, receipt, config.private_markers)


def _hold_job(
    config: Config,
    ledger: Ledger,
    *,
    signal: SignalV1,
    job_id: str,
    claim_key: str,
    reason: str,
    checks: Sequence[CheckResultV1],
    resulting_sha: Optional[str],
    circuit: bool,
) -> JobOutcome:
    safe_reason = redact_text(reason, config.private_markers)
    if circuit:
        ledger.fail_job_open_circuit(job_id, claim_key, safe_reason)
    receipt_path = _receipt(
        config,
        signal=signal,
        outcome="held",
        checks=checks,
        resulting_sha=resulting_sha,
        hold_reason=safe_reason,
    )
    if not circuit:
        ledger.complete_job(
            job_id,
            claim_key,
            outcome="held",
            resulting_sha=resulting_sha,
            hold_reason=safe_reason,
        )
    return JobOutcome(
        completed=False,
        circuit_open=circuit,
        message=safe_reason,
        receipt_path=receipt_path,
        resulting_sha=resulting_sha,
    )


def run_claimed_job(
    config: Config,
    ledger: Ledger,
    client: GitHubClient,
    signal: SignalV1,
    job_id: str,
    claim_key: str,
    runner_environment: Dict[str, str],
) -> JobOutcome:
    checks: List[CheckResultV1] = []
    worktree: Optional[Path] = None
    resulting_sha: Optional[str] = None
    ledger.mark_running(job_id, claim_key)
    try:
        classifier = run_classifier(
            config.classifier_command,
            request_payload={
                "schema": "ClassifierRequestV1",
                "signal": signal.to_public_dict(),
                "untrusted_review_body": signal._raw_body,
                "hold_categories": [
                    "product_direction",
                    "conflicting_feedback",
                    "security",
                    "credentials",
                    "permissions",
                    "destructive_work",
                    "production_effects",
                    "unclear_acceptance",
                ],
            },
            state_dir=config.state_dir,
            run_id=job_id,
            required_identity=config.required_runner_identity,
            runner_environment=runner_environment,
            timeout=config.runner_timeout_seconds,
        )
        decision = classifier.decision
        if decision.verdict != "ROUTINE":
            return _hold_job(
                config,
                ledger,
                signal=signal,
                job_id=job_id,
                claim_key=claim_key,
                reason=decision.reason or "classifier_hold",
                checks=checks,
                resulting_sha=None,
                circuit=False,
            )

        allowed_paths = _allowed_paths(config, signal, decision)
        verification_commands = _select_verifications(config, signal.repository, decision)

        live_pr = fetch_pr_threads(client, signal.repository, signal.pr_number)
        if not _pr_snapshot_matches(live_pr, signal):
            raise RunnerContractError("pr_snapshot_stale_before_build")
        if not _can_push_head_repository(client, signal.head_repository, config.operator_logins):
            raise RunnerContractError("head_repository_push_permission_missing")
        initial_checks = _required_check_snapshot(
            client.required_checks(signal.repository, signal.pr_number)
        )
        if not _required_checks_green(initial_checks):
            raise RunnerContractError("required_checks_not_green_before_build")
        observed_remote_sha = remote_ref_sha(
            config.git_command, signal.head_clone_url, signal.head_ref
        )
        if observed_remote_sha != signal.observed_head_sha:
            raise RunnerContractError("head_repository_ref_stale_before_build")

        ledger.mark_phase(job_id, "preparing")
        worktree = create_worktree(
            git_cmd=config.git_command,
            workspace_root=config.workspace_root,
            repository=signal.head_repository,
            clone_url=signal.head_clone_url,
            head_sha=signal.observed_head_sha,
            pr_number=signal.pr_number,
            branch_name=signal.head_ref,
        )
        if current_head(config.git_command, worktree) != signal.observed_head_sha:
            raise RunnerContractError("worktree_not_at_observed_sha")

        task = TaskSpecV1(
            goal="Apply one bounded routine review fix and commit it locally",
            base_sha=signal.observed_head_sha,
            permitted_paths=allowed_paths,
            permitted_actions=["edit", "test", "commit"],
            acceptance_criteria=[
                "address the exact trusted review comment",
                "keep all changes inside permitted paths",
                "pass every named verification",
            ],
            non_goals=[
                "scope expansion",
                "credential or permission changes",
                "history rewriting",
            ],
            approval_class="standing_routine_pr_feedback",
            repository=signal.repository,
            pr_number=signal.pr_number,
        )

        ledger.mark_phase(job_id, "building")
        builder = run_builder(
            config.builder_command,
            task=task,
            signal_public={
                **signal.to_public_dict(),
                "_raw_body_for_runner_only": signal._raw_body,
            },
            state_dir=config.state_dir,
            worktree_path=worktree,
            run_id=job_id,
            continuation_token=classifier.continuation_token,
            classifier_identity=classifier.identity,
            required_identity=config.required_runner_identity,
            runner_environment=runner_environment,
            timeout=config.runner_timeout_seconds,
        )
        candidate_sha, _ = _validate_local_candidate(
            config,
            worktree,
            signal.observed_head_sha,
            allowed_paths,
            builder.changed_paths,
        )
        if builder.resulting_sha != candidate_sha:
            raise RunnerContractError("builder_resulting_sha_mismatch")

        ledger.mark_phase(job_id, "verifying")
        checks = run_named_verifications(
            verification_commands,
            cwd=worktree,
            subject_ref=candidate_sha,
            timeout=config.runner_timeout_seconds,
        )
        if not all_passed(checks):
            raise RunnerContractError("builder_verification_failed")

        ledger.mark_phase(job_id, "reviewing")
        reviewer = run_reviewer(
            config.reviewer_command,
            task=task,
            base_sha=signal.observed_head_sha,
            candidate_sha=candidate_sha,
            diff_text=diff_text(config.git_command, worktree, signal.observed_head_sha),
            verification=[check.to_dict() for check in checks],
            state_dir=config.state_dir,
            worktree_path=worktree,
            run_id=job_id,
            classifier_identity=classifier.identity,
            required_identity=config.required_runner_identity,
            runner_environment=runner_environment,
            timeout=config.runner_timeout_seconds,
        )
        resulting_sha, _ = _validate_local_candidate(
            config,
            worktree,
            signal.observed_head_sha,
            allowed_paths,
        )
        if reviewer.resulting_sha != resulting_sha:
            raise RunnerContractError("reviewer_resulting_sha_mismatch")
        if reviewer.verdict != "PASS":
            raise RunnerContractError("reviewer_hold")

        ledger.mark_phase(job_id, "final_verifying")
        checks = run_named_verifications(
            verification_commands,
            cwd=worktree,
            subject_ref=resulting_sha,
            timeout=config.runner_timeout_seconds,
        )
        if not all_passed(checks):
            raise RunnerContractError("final_verification_failed")
        check_ids = [check.check_id for check in checks]
        _validate_reply_draft(
            reviewer.reply_draft,
            resulting_sha,
            check_ids,
            config.private_markers,
        )

        ledger.mark_phase(job_id, "pre_push")
        live_thread = readback_thread(client, signal.thread_node_id)
        thread_actionable, thread_reason = thread_still_actionable(
            live_thread,
            expected_latest_comment_id=signal.latest_comment_node_id,
        )
        if not thread_actionable:
            raise RunnerContractError("thread_stale_before_push:" + thread_reason)
        live_pr = fetch_pr_threads(client, signal.repository, signal.pr_number)
        if not _pr_snapshot_matches(live_pr, signal):
            raise RunnerContractError("pr_snapshot_stale_before_push")
        final_checks = _required_check_snapshot(
            client.required_checks(signal.repository, signal.pr_number)
        )
        if final_checks != initial_checks or not _required_checks_green(final_checks):
            raise RunnerContractError("required_checks_stale_before_push")
        if not _can_push_head_repository(client, signal.head_repository, config.operator_logins):
            raise RunnerContractError("push_permission_stale_before_push")
        remote_before_push = remote_ref_sha(
            config.git_command, signal.head_clone_url, signal.head_ref
        )
        if remote_before_push != signal.observed_head_sha:
            raise RunnerContractError("head_repository_ref_stale_before_push")
        if current_head(config.git_command, worktree) != resulting_sha:
            raise RunnerContractError("local_head_stale_before_push")
        if not worktree_is_clean(config.git_command, worktree):
            raise RunnerContractError("worktree_dirty_before_push")
        if not base_is_ancestor(config.git_command, worktree, signal.observed_head_sha):
            raise RunnerContractError("history_integrity_failed_before_push")
        preview = json.dumps(
            {
                "reply": reviewer.reply_draft,
                "repository": signal.repository,
                "sha": resulting_sha,
                "checks": check_ids,
            },
            sort_keys=True,
        )
        privacy_findings = assert_no_private_material(preview, config.private_markers)
        if privacy_findings:
            raise RunnerContractError(
                "public_mutation_privacy_failure:" + ",".join(privacy_findings)
            )

        ledger.mark_phase(job_id, "pushing")
        push = push_head_no_force(
            git_cmd=config.git_command,
            worktree=worktree,
            remote_url=signal.head_clone_url,
            head_ref=signal.head_ref,
        )
        if not push.ok:
            raise RunnerContractError("push_failed")
        ledger.mark_phase(job_id, "pushed")
        if remote_ref_sha(
            config.git_command, signal.head_clone_url, signal.head_ref
        ) != resulting_sha:
            raise RunnerContractError("pushed_ref_attestation_failed")

        ledger.mark_phase(job_id, "replying")
        reply_node_id = reply_on_thread(
            client,
            thread_node_id=signal.thread_node_id,
            body=reviewer.reply_draft,
        )
        if not reply_node_id:
            raise RunnerContractError("reply_missing_node_id")
        ledger.mark_phase(job_id, "replied")
        readback = readback_thread(client, signal.thread_node_id)
        if not verify_reply_present(readback, reply_node_id, reviewer.reply_draft):
            raise RunnerContractError("reply_readback_mismatch")

        receipt_path = _receipt(
            config,
            signal=signal,
            outcome="completed",
            checks=checks,
            resulting_sha=resulting_sha,
            hold_reason=None,
            reply_node_id=reply_node_id,
        )
        ledger.complete_job(
            job_id,
            claim_key,
            outcome="completed",
            resulting_sha=resulting_sha,
            reply_node_id=reply_node_id,
        )
        return JobOutcome(
            completed=True,
            circuit_open=False,
            message="completed",
            receipt_path=receipt_path,
            resulting_sha=resulting_sha,
        )
    except (RunnerError, GitHubError, OSError, ValueError) as exc:
        return _hold_job(
            config,
            ledger,
            signal=signal,
            job_id=job_id,
            claim_key=claim_key,
            reason=str(exc) or exc.__class__.__name__,
            checks=checks,
            resulting_sha=resulting_sha,
            circuit=True,
        )
    except Exception as exc:
        return _hold_job(
            config,
            ledger,
            signal=signal,
            job_id=job_id,
            claim_key=claim_key,
            reason=f"internal_failure:{exc.__class__.__name__}",
            checks=checks,
            resulting_sha=resulting_sha,
            circuit=True,
        )
    finally:
        if worktree is not None:
            try:
                remove_worktree(
                    config.git_command,
                    config.workspace_root,
                    signal.head_repository,
                    worktree,
                )
            except (RunnerError, OSError):
                ledger.open_circuit("worktree_cleanup_failed")


def sweep(
    config: Config,
    client: Optional[GitHubClient] = None,
    *,
    target_repository: str = "",
    target_pr_number: int = 0,
) -> SweepOutcome:
    ensure_state_dirs(config)
    github = client or GhClient(config.gh_command)
    ledger = Ledger(config.state_dir / "ledger.sqlite3")

    try:
        inspected = inspect_work(config, client=github)
    except (GitHubError, RunnerError, OSError, ValueError) as exc:
        reason = redact_text(str(exc) or exc.__class__.__name__, config.private_markers)
        ledger.open_circuit("inspection_failed:" + reason)
        return SweepOutcome(exit_code=1, message="inspection_failed:" + reason)

    if (config.state_dir / config.pause_file_name).exists():
        return SweepOutcome(
            exit_code=0,
            message="paused_inspect_only",
            inspected_prs=inspected.inspected_prs,
            signals_found=len(inspected.signals),
        )
    if ledger.circuit_open():
        return SweepOutcome(
            exit_code=1,
            message=f"circuit_open:{ledger.circuit_reason() or 'unknown'}",
            inspected_prs=inspected.inspected_prs,
            signals_found=len(inspected.signals),
        )
    ledger.reclaim_stale_jobs(config.reclaim_after_seconds)
    if ledger.circuit_open():
        return SweepOutcome(
            exit_code=1,
            message=f"circuit_open:{ledger.circuit_reason() or 'interrupted_job'}",
            inspected_prs=inspected.inspected_prs,
            signals_found=len(inspected.signals),
        )
    signals = list(inspected.signals)
    if target_repository and target_pr_number:
        signals = [
            signal
            for signal in signals
            if signal.repository.lower() == target_repository.lower()
            and signal.pr_number == int(target_pr_number)
        ]
    if not signals:
        return SweepOutcome(
            exit_code=0,
            message="no_actionable_signal",
            inspected_prs=inspected.inspected_prs,
            signals_found=0,
        )

    runner_environment = build_runner_environment(
        state_dir=config.state_dir,
        allowlist=config.runner_environment_allowlist,
    )
    try:
        prove_github_capability_isolation(
            client=github,
            operator_logins=config.operator_logins,
            gh_command=config.gh_command,
            runner_environment=runner_environment,
        )
    except (RunnerError, OSError) as exc:
        reason = redact_text(str(exc) or exc.__class__.__name__, config.private_markers)
        ledger.open_circuit(reason)
        return SweepOutcome(
            exit_code=1,
            message=reason,
            inspected_prs=inspected.inspected_prs,
            signals_found=len(signals),
        )

    queue = list(signals)
    attempted: set = set()
    receipts: List[Path] = []
    completed = 0
    held = 0
    last_message = ""

    while queue and not ledger.circuit_open():
        signal = queue.pop(0)
        claim_key = _claim_key(signal)
        if claim_key in attempted:
            continue
        attempted.add(claim_key)
        claim_result, job_id, detail = ledger.try_claim(
            repository=signal.repository,
            pr_number=signal.pr_number,
            thread_node_id=signal.thread_node_id,
            latest_comment_node_id=signal.latest_comment_node_id,
            observed_head_sha=signal.observed_head_sha,
            signal_digest=signal.body_digest,
            reclaim_after_seconds=config.reclaim_after_seconds,
        )
        if claim_result == "duplicate":
            continue
        if claim_result == "busy":
            return SweepOutcome(
                exit_code=0,
                message="busy_inspect_only",
                receipt_paths=receipts,
                jobs_completed=completed,
                jobs_held=held,
                inspected_prs=inspected.inspected_prs,
                signals_found=len(signals),
            )
        if claim_result == "circuit_open":
            last_message = f"circuit_open:{detail or 'unknown'}"
            break
        if claim_result != "claimed" or not job_id:
            ledger.open_circuit("claim_state_invalid")
            last_message = "claim_state_invalid"
            break

        result = run_claimed_job(
            config,
            ledger,
            github,
            signal,
            job_id,
            claim_key,
            runner_environment,
        )
        receipts.append(result.receipt_path)
        last_message = result.message
        if result.completed:
            completed += 1
            queue = [
                queued
                for queued in queue
                if not (
                    queued.repository.lower() == signal.repository.lower()
                    and queued.pr_number == signal.pr_number
                )
            ]
            try:
                refreshed = inspect_work(config, client=github)
                refreshed_signals = list(refreshed.signals)
                if target_repository and target_pr_number:
                    refreshed_signals = [
                        candidate
                        for candidate in refreshed_signals
                        if candidate.repository.lower() == target_repository.lower()
                        and candidate.pr_number == int(target_pr_number)
                    ]
                for candidate in refreshed_signals:
                    if _claim_key(candidate) not in attempted:
                        queue.append(candidate)
            except (GitHubError, RunnerError, OSError, ValueError) as exc:
                ledger.open_circuit("refresh_failed:" + redact_text(str(exc), config.private_markers))
                last_message = "refresh_failed"
        else:
            held += 1
        if result.circuit_open:
            break

    exit_code = 1 if held or ledger.circuit_open() else 0
    if ledger.circuit_open():
        message = f"circuit_open:{ledger.circuit_reason() or last_message or 'unknown'}"
    elif held:
        message = f"held:{held};completed:{completed}"
    else:
        message = f"completed:{completed}"
    return SweepOutcome(
        exit_code=exit_code,
        message=message,
        receipt_path=receipts[-1] if receipts else None,
        receipt_paths=receipts,
        jobs_completed=completed,
        jobs_held=held,
        inspected_prs=inspected.inspected_prs,
        signals_found=len(signals),
    )
