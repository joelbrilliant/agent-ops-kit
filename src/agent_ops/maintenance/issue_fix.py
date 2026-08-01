"""Issue-to-draft-PR sweep with fail-closed mutation gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_ops.audit.receipts import default_redaction_record, write_issue_receipt
from agent_ops.audit.redaction import assert_no_private_material, redact_text
from agent_ops.config import Config, IssueAutomationConfig, ensure_state_dirs
from agent_ops.contracts import (
    CheckResultV1,
    DecisionV1,
    IssueDraftReceiptV1,
    IssueSignalV1,
    TaskSpecV1,
    issue_signal_digest,
)
from agent_ops.github.client import GhClient, GitHubClient, GitHubError
from agent_ops.github.draft_pr import (
    create_draft_pull_request,
    read_pull_request,
    verify_draft_pr_readback,
)
from agent_ops.github.issue_reply import post_and_verify_issue_comment
from agent_ops.github.issues import (
    IssueDiscoveryResult,
    IssueSkip,
    discover_issue_signals,
    fetch_issue_snapshot,
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
from agent_ops.paths import is_path_allowed, is_safe_repo_path, normalize_repo_path
from agent_ops.process import RunnerError
from agent_ops.qa.verify import all_passed, run_named_verifications
from agent_ops.runners.runner import (
    RunnerContractError,
    build_runner_environment,
    prove_github_capability_isolation,
    run_builder,
    run_classifier,
    run_issue_reviewer,
)


@dataclass
class IssueInspectOutcome:
    inspected_issues: int
    signals: List[IssueSignalV1]
    skips: List[IssueSkip]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "inspected_issues": self.inspected_issues,
            "signals": [signal.to_public_dict() for signal in self.signals],
            "skips": [skip.__dict__ for skip in self.skips],
        }


@dataclass
class IssueSweepOutcome:
    exit_code: int
    message: str
    inspected_issues: int = 0
    signals_found: int = 0
    receipt_path: Optional[Path] = None
    receipt_paths: List[Path] = field(default_factory=list)
    jobs_completed: int = 0
    jobs_held: int = 0


@dataclass
class IssueJobOutcome:
    completed: bool
    circuit_open: bool
    message: str
    receipt_path: Path
    resulting_sha: Optional[str] = None


def _issue_claim_key(signal: IssueSignalV1) -> str:
    # Packet AC-7: repository + issue + observed updated_at + observed default-branch tip SHA
    return make_claim_key(
        signal.repository,
        signal.issue_number,
        "issue",
        signal.observed_updated_at,
        signal.observed_base_sha,
    )


def _require_issue_cfg(config: Config) -> IssueAutomationConfig:
    if config.issue_automation is None:
        raise RunnerContractError("issue_automation_not_configured")
    if not config.issue_automation.enabled:
        raise RunnerContractError("issue_automation_disabled")
    return config.issue_automation


def inspect_issue_work(
    config: Config, client: Optional[GitHubClient] = None
) -> IssueInspectOutcome:
    ensure_state_dirs(config)
    if config.issue_automation is None:
        return IssueInspectOutcome(inspected_issues=0, signals=[], skips=[])
    github = client or GhClient(config.gh_command)
    discovered = discover_issue_signals(github, config)
    ledger = Ledger(config.state_dir / "ledger.sqlite3")
    skips = list(discovered.skips)
    pending: List[IssueSignalV1] = []
    for signal in discovered.signals:
        if ledger.is_processed(_issue_claim_key(signal)):
            skips.append(
                IssueSkip(
                    signal.repository,
                    signal.issue_number,
                    "already_processed",
                    signal.issue_node_id,
                )
            )
        else:
            pending.append(signal)
    return IssueInspectOutcome(
        inspected_issues=discovered.inspected_issues,
        signals=pending,
        skips=skips,
    )


def _branch_name(issue_cfg: IssueAutomationConfig, signal: IssueSignalV1) -> str:
    short = issue_signal_digest(signal)[:8]
    return f"{issue_cfg.branch_prefix}-{signal.issue_number}-{short}"


def _owned_namespaces(config: Config) -> set[str]:
    owned = {login.lower() for login in config.operator_logins}
    owned.update(namespace.lower() for namespace in config.owned_namespaces)
    return owned


def _can_push_repository(
    client: GitHubClient,
    repository: str,
    operator_logins: Sequence[str],
    owned_namespaces: Sequence[str] = (),
) -> bool:
    data = client.rest_get(f"/repos/{repository}")
    permissions = data.get("permissions") or {}
    push = bool(permissions.get("push") or permissions.get("maintain") or permissions.get("admin"))
    owner = str((data.get("owner") or {}).get("login") or "").lower()
    full_name = str(data.get("full_name") or repository).lower()
    allowed = {login.lower() for login in operator_logins}
    allowed.update(namespace.lower() for namespace in owned_namespaces)
    owner_ok = (not owner) or owner in allowed
    ns_ok = full_name.split("/", 1)[0] in allowed
    return push and (owner_ok or ns_ok)


def _repo_is_public(client: GitHubClient, repository: str) -> bool:
    data = client.rest_get(f"/repos/{repository}")
    return not bool(data.get("private"))


def _assert_issue_last_safe_point(
    config: Config,
    issue_cfg: IssueAutomationConfig,
    client: GitHubClient,
    signal: IssueSignalV1,
    *,
    branch: str,
    stale_reason: str,
) -> IssueSignalV1:
    """Re-prove AC-4 gates immediately before worktree create and before mutation."""
    if config.exact_policy_for(signal.repository) is None:
        raise RunnerContractError("missing_exact_repository_policy")
    expected_base = issue_cfg.enabled_repositories.get(signal.repository)
    if expected_base is None:
        raise RunnerContractError("repository_not_enabled_for_issue_automation")
    if expected_base != signal.base_ref:
        raise RunnerContractError("issue_base_ref_policy_mismatch")

    live, skip, meta = fetch_issue_snapshot(
        client,
        repository=signal.repository,
        issue_number=signal.issue_number,
        base_ref=signal.base_ref,
        require_labels=issue_cfg.require_labels,
        ignore_labels=issue_cfg.ignore_labels,
    )
    if skip is not None or live is None:
        raise RunnerContractError(stale_reason)
    if (
        live.conversation_digest != signal.conversation_digest
        or live.observed_base_sha != signal.observed_base_sha
        or live.observed_updated_at != signal.observed_updated_at
        or live.latest_comment_node_id != signal.latest_comment_node_id
        or live.issue_node_id != signal.issue_node_id
    ):
        raise RunnerContractError(stale_reason)
    if (
        issue_cfg.require_labels
        and not any(label in live.labels for label in issue_cfg.require_labels)
    ):
        raise RunnerContractError("label_removed_before_push")
    if meta and meta.get("is_private") is True:
        raise RunnerContractError("repository_not_public")
    if not _repo_is_public(client, signal.repository):
        raise RunnerContractError("repository_not_public")
    if not _can_push_repository(
        client,
        signal.repository,
        config.operator_logins,
        owned_namespaces=sorted(_owned_namespaces(config)),
    ):
        raise RunnerContractError("repository_push_permission_missing")
    existing = remote_ref_sha(config.git_command, signal.clone_url, branch)
    if existing is not None:
        raise RunnerContractError("target_branch_exists")
    return live


def _validate_bounds(
    issue_cfg: IssueAutomationConfig,
    *,
    allowed_paths: Sequence[str],
    changed: Sequence[str],
    diff: str,
) -> Optional[str]:
    if len(allowed_paths) > issue_cfg.max_paths_per_issue:
        return "max_paths_per_issue_exceeded"
    if len(changed) > issue_cfg.max_changed_files:
        return "max_changed_files_exceeded"
    diff_lines = diff.count("\n") + (1 if diff and not diff.endswith("\n") else 0)
    if diff and diff_lines > issue_cfg.max_diff_lines:
        return "max_diff_lines_exceeded"
    return None


def _select_verifications(
    config: Config, repository: str, decision: DecisionV1
) -> Dict[str, List[str]]:
    available: Dict[str, List[str]] = dict(config.default_verification_commands)
    policy = config.exact_policy_for(repository) or config.policy_for(repository)
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


def _policy_paths(config: Config, repository: str) -> List[str]:
    policy = config.exact_policy_for(repository)
    if policy is None or not policy.permitted_paths:
        raise RunnerContractError("missing_exact_repository_policy")
    paths = [normalize_repo_path(p) for p in policy.permitted_paths]
    if any(not is_safe_repo_path(p) for p in paths):
        raise RunnerContractError("policy_path_unsafe")
    return paths


def _issue_allowed_paths(
    config: Config, repository: str, decision: DecisionV1
) -> List[str]:
    """Issue jobs use configured policy paths only; classifier cannot widen scope."""
    policy_paths = _policy_paths(config, repository)
    requested = [normalize_repo_path(path) for path in decision.requested_allowed_paths]
    if any(not is_safe_repo_path(path) for path in requested):
        raise RunnerContractError("classifier_path_unsafe")
    for path in requested:
        if not is_path_allowed(path, policy_paths):
            raise RunnerContractError("classifier_scope_expansion")
    return list(policy_paths)

def _validate_local_candidate(
    config: Config,
    worktree: Path,
    base_sha: str,
    allowed_paths: Sequence[str],
    declared_paths: Optional[Sequence[str]] = None,
    issue_cfg: Optional[IssueAutomationConfig] = None,
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
    if issue_cfg is not None:
        bound_reason = _validate_bounds(
            issue_cfg,
            allowed_paths=allowed_paths,
            changed=changed,
            diff=diff_text(config.git_command, worktree, base_sha),
        )
        if bound_reason:
            raise RunnerContractError(bound_reason)
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


def _compose_issue_reply(
    issue_cfg: IssueAutomationConfig,
    *,
    reply_draft: str,
    draft_pr_url: str,
    resulting_sha: str,
    named_checks: Sequence[str],
) -> str:
    template = issue_cfg.issue_reply_template
    checks = ", ".join(named_checks)
    try:
        rendered = template.format(
            pr_url=draft_pr_url,
            draft_pr_url=draft_pr_url,
            resulting_sha=resulting_sha,
            named_checks=checks,
            reply_draft=reply_draft,
        )
    except (KeyError, ValueError):
        rendered = reply_draft
    if "{draft_pr_url}" in rendered:
        rendered = rendered.replace("{draft_pr_url}", draft_pr_url)
    if draft_pr_url and draft_pr_url not in rendered:
        rendered = rendered.rstrip() + f"\n\nDraft PR: {draft_pr_url}\n"
    if resulting_sha and resulting_sha not in rendered:
        rendered = rendered.rstrip() + f"\nSHA: {resulting_sha}\n"
    return rendered


def _compose_pr_title(issue_cfg: IssueAutomationConfig, signal: IssueSignalV1, fallback: str) -> str:
    try:
        title = issue_cfg.draft_pr_title_template.format(
            issue_number=signal.issue_number,
            title_digest=signal.title_digest[:12],
            repository=signal.repository,
        )
    except (KeyError, ValueError):
        title = fallback
    title = (title or fallback).strip() or fallback
    if any(ch in title for ch in ("\n", "\r", "\x00")):
        return fallback
    return title


def _receipt(
    config: Config,
    *,
    signal: IssueSignalV1,
    outcome: str,
    checks: Sequence[CheckResultV1],
    resulting_sha: Optional[str],
    hold_reason: Optional[str],
    branch_name: str = "",
    draft_pr_number: Optional[int] = None,
    draft_pr_url: Optional[str] = None,
    issue_reply_node_id: Optional[str] = None,
) -> Path:
    receipt = IssueDraftReceiptV1(
        signal_digest=issue_signal_digest(signal),
        repository=signal.repository,
        issue_number=signal.issue_number,
        base_sha=signal.observed_base_sha,
        resulting_sha=resulting_sha or "",
        branch_name=branch_name,
        draft_pr_number=draft_pr_number,
        draft_pr_url=draft_pr_url,
        issue_reply_node_id=issue_reply_node_id,
        named_checks=[check.check_id for check in checks],
        outcome=outcome,
        hold_reason=redact_text(hold_reason or "", config.private_markers) or None,
        redaction_record=default_redaction_record(),
    )
    return write_issue_receipt(config.state_dir, receipt, config.private_markers)


def _hold_job(
    config: Config,
    ledger: Ledger,
    *,
    signal: IssueSignalV1,
    job_id: str,
    claim_key: str,
    reason: str,
    checks: Sequence[CheckResultV1],
    resulting_sha: Optional[str],
    circuit: bool,
    branch_name: str = "",
) -> IssueJobOutcome:
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
        branch_name=branch_name,
    )
    if not circuit:
        ledger.complete_job(
            job_id,
            claim_key,
            outcome="held",
            resulting_sha=resulting_sha,
            hold_reason=safe_reason,
        )
    return IssueJobOutcome(
        completed=False,
        circuit_open=circuit,
        message=safe_reason,
        receipt_path=receipt_path,
        resulting_sha=resulting_sha,
    )


def _issue_text_blob(signal: IssueSignalV1) -> str:
    parts = [signal._raw_title, signal._raw_body]
    for comment in signal._raw_comments:
        if isinstance(comment, dict):
            parts.append(str(comment.get("body") or ""))
    return "\n".join(parts)


def _hold_markers_in_issue(signal: IssueSignalV1) -> Optional[str]:
    """Deterministic pre-worktree injection hold for obvious unsafe issue text."""
    lower = _issue_text_blob(signal).lower()
    markers = [
        "ignore previous",
        "exfiltrat",
        "credential",
        "force push",
        "drop table",
        "product direction",
        "api key",
        "prompt injection",
        "production deploy",
        "change permissions",
        "delete the database",
    ]
    for marker in markers:
        if marker in lower:
            return f"hold_marker:{marker}"
    return None


def run_claimed_issue_job(
    config: Config,
    ledger: Ledger,
    client: GitHubClient,
    signal: IssueSignalV1,
    job_id: str,
    claim_key: str,
    runner_environment: Dict[str, str],
) -> IssueJobOutcome:
    issue_cfg = _require_issue_cfg(config)
    checks: List[CheckResultV1] = []
    worktree: Optional[Path] = None
    resulting_sha: Optional[str] = None
    branch = _branch_name(issue_cfg, signal)
    mutation_started = False
    ledger.mark_running(job_id, claim_key)
    try:
        pre_hold = _hold_markers_in_issue(signal)
        if pre_hold:
            return _hold_job(
                config,
                ledger,
                signal=signal,
                job_id=job_id,
                claim_key=claim_key,
                reason=pre_hold,
                checks=checks,
                resulting_sha=None,
                circuit=False,
                branch_name=branch,
            )

        classifier = run_classifier(
            issue_cfg.classifier_command,
            request_payload={
                "schema": "IssueClassifierRequestV1",
                "signal": signal.to_public_dict(),
                "untrusted_issue_title": signal._raw_title,
                "untrusted_issue_body": signal._raw_body,
                "untrusted_issue_comments": list(signal._raw_comments),
                "hold_categories": [
                    "product_direction",
                    "conflicting_requirements",
                    "security",
                    "credentials",
                    "permissions",
                    "destructive_work",
                    "production_effects",
                    "unclear_acceptance",
                    "prompt_injection",
                ],
            },
            state_dir=config.state_dir,
            run_id=job_id,
            required_identity=issue_cfg.build_runner_identity,
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
                branch_name=branch,
            )

        allowed_paths = _issue_allowed_paths(config, signal.repository, decision)
        verification_commands = _select_verifications(config, signal.repository, decision)

        # Re-fetch and re-prove AC-4 gates before worktree creation.
        _assert_issue_last_safe_point(
            config,
            issue_cfg,
            client,
            signal,
            branch=branch,
            stale_reason="issue_snapshot_stale_before_build",
        )

        ledger.mark_phase(job_id, "preparing")
        worktree = create_worktree(
            git_cmd=config.git_command,
            workspace_root=config.workspace_root,
            repository=signal.repository,
            clone_url=signal.clone_url,
            head_sha=signal.observed_base_sha,
            pr_number=signal.issue_number,
            branch_name=branch,
        )
        if current_head(config.git_command, worktree) != signal.observed_base_sha:
            raise RunnerContractError("worktree_not_at_observed_sha")

        task = TaskSpecV1(
            goal="Apply one bounded routine issue fix and commit it locally",
            base_sha=signal.observed_base_sha,
            permitted_paths=allowed_paths,
            permitted_actions=["edit", "test", "commit"],
            acceptance_criteria=[
                "address the labelled issue within configured path policy",
                "keep all changes inside permitted paths",
                "pass every named verification",
            ],
            non_goals=[
                "scope expansion",
                "credential or permission changes",
                "history rewriting",
                "merge or deploy",
            ],
            approval_class="standing_routine_issue_fix",
            repository=signal.repository,
            issue_number=signal.issue_number,
        )

        ledger.mark_phase(job_id, "building")
        builder = run_builder(
            issue_cfg.builder_command,
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
            required_identity=issue_cfg.build_runner_identity,
            runner_environment=runner_environment,
            timeout=config.runner_timeout_seconds,
        )
        candidate_sha, _ = _validate_local_candidate(
            config,
            worktree,
            signal.observed_base_sha,
            allowed_paths,
            builder.changed_paths,
            issue_cfg=issue_cfg,
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
        reviewer = run_issue_reviewer(
            issue_cfg.reviewer_command,
            task=task,
            base_sha=signal.observed_base_sha,
            candidate_sha=candidate_sha,
            diff_text=diff_text(config.git_command, worktree, signal.observed_base_sha),
            verification=[check.to_dict() for check in checks],
            state_dir=config.state_dir,
            worktree_path=worktree,
            run_id=job_id,
            classifier_identity=classifier.identity,
            required_identity=issue_cfg.review_runner_identity,
            runner_environment=runner_environment,
            timeout=config.runner_timeout_seconds,
        )
        resulting_sha, _ = _validate_local_candidate(
            config,
            worktree,
            signal.observed_base_sha,
            allowed_paths,
            issue_cfg=issue_cfg,
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
        pr_title_findings = assert_no_private_material(
            reviewer.pr_title, config.private_markers
        )
        pr_body_findings = assert_no_private_material(
            reviewer.pr_body, config.private_markers
        )
        if pr_title_findings or pr_body_findings:
            raise RunnerContractError("pr_copy_privacy_failure")

        ledger.mark_phase(job_id, "pre_push")
        # Last safe point: re-prove policy, ownership, public visibility, pushability,
        # open/label/digest/base SHA state, and absent target branch before mutation.
        _assert_issue_last_safe_point(
            config,
            issue_cfg,
            client,
            signal,
            branch=branch,
            stale_reason="issue_stale_before_push",
        )

        ledger.mark_phase(job_id, "pushing")
        mutation_started = True
        push_result = push_head_no_force(
            git_cmd=config.git_command,
            worktree=worktree,
            remote_url=signal.clone_url,
            head_ref=branch,
        )
        if not push_result.ok:
            raise RunnerContractError("push_failed")
        remote_sha = remote_ref_sha(config.git_command, signal.clone_url, branch)
        if remote_sha != resulting_sha:
            raise RunnerContractError("remote_sha_mismatch_after_push")

        ledger.mark_phase(job_id, "creating_pr")
        created = create_draft_pull_request(
            client,
            repository=signal.repository,
            base_ref=signal.base_ref,
            head_ref=branch,
            title=_compose_pr_title(issue_cfg, signal, reviewer.pr_title),
            body=(
                reviewer.pr_body
                if f"Closes #{signal.issue_number}" in reviewer.pr_body
                or f"closes #{signal.issue_number}" in reviewer.pr_body.lower()
                else reviewer.pr_body.rstrip() + f"\n\nCloses #{signal.issue_number}\n"
            ),
        )
        create_mismatch = verify_draft_pr_readback(
            created,
            expected_base_ref=signal.base_ref,
            expected_head_ref=branch,
            expected_head_oid=resulting_sha,
        )
        if create_mismatch:
            raise RunnerContractError(create_mismatch)
        readback = read_pull_request(
            client, repository=signal.repository, number=created.number
        )
        mismatch = verify_draft_pr_readback(
            readback,
            expected_base_ref=signal.base_ref,
            expected_head_ref=branch,
            expected_head_oid=resulting_sha,
        )
        if mismatch:
            raise RunnerContractError(mismatch)

        final_reply = _compose_issue_reply(
            issue_cfg,
            reply_draft=reviewer.reply_draft,
            draft_pr_url=readback.url,
            resulting_sha=resulting_sha,
            named_checks=check_ids,
        )
        _validate_reply_draft(
            final_reply,
            resulting_sha,
            check_ids,
            config.private_markers,
        )
        if readback.url not in final_reply:
            raise RunnerContractError("reply_missing_draft_pr_url")

        ledger.mark_phase(job_id, "replying")
        comment = post_and_verify_issue_comment(
            client,
            issue_node_id=signal.issue_node_id,
            body=final_reply,
        )

        receipt_path = _receipt(
            config,
            signal=signal,
            outcome="completed",
            checks=checks,
            resulting_sha=resulting_sha,
            hold_reason=None,
            branch_name=branch,
            draft_pr_number=readback.number,
            draft_pr_url=readback.url,
            issue_reply_node_id=comment.node_id,
        )
        ledger.complete_job(
            job_id,
            claim_key,
            outcome="completed",
            resulting_sha=resulting_sha,
            reply_node_id=comment.node_id,
        )
        return IssueJobOutcome(
            completed=True,
            circuit_open=False,
            message="completed",
            receipt_path=receipt_path,
            resulting_sha=resulting_sha,
        )
    except (RunnerContractError, RunnerError, GitHubError, OSError, ValueError) as exc:
        reason = redact_text(str(exc) or exc.__class__.__name__, config.private_markers)
        # After mutation starts, any failure is ambiguous: open circuit.
        circuit = mutation_started or reason.startswith(
            (
                "push_",
                "remote_sha",
                "pr_",
                "createPullRequest",
                "issue comment",
                "addComment",
            )
        )
        # Safety / identity / path / verification always open circuit.
        safety_prefixes = (
            "runner_identity",
            "disallowed_changes",
            "history_integrity",
            "runner_github_auth",
            "orchestrator_identity",
            "builder_verification",
            "final_verification",
            "runner_left_dirty",
            "classifier_scope",
            "reply_draft_privacy",
            "pr_copy_privacy",
            "mutation",
        )
        if any(reason.startswith(p) or p in reason for p in safety_prefixes):
            circuit = True
        if mutation_started:
            circuit = True
        return _hold_job(
            config,
            ledger,
            signal=signal,
            job_id=job_id,
            claim_key=claim_key,
            reason=reason,
            checks=checks,
            resulting_sha=resulting_sha,
            circuit=circuit,
            branch_name=branch,
        )
    finally:
        if worktree is not None:
            try:
                remove_worktree(
                    config.git_command,
                    config.workspace_root,
                    signal.repository,
                    worktree,
                )
            except Exception:
                pass


def issue_sweep(
    config: Config,
    client: Optional[GitHubClient] = None,
) -> IssueSweepOutcome:
    from agent_ops.exit_codes import HELD, OK
    from agent_ops.maintenance.orchestrator import is_paused

    ensure_state_dirs(config)
    if config.issue_automation is None or not config.issue_automation.enabled:
        return IssueSweepOutcome(exit_code=OK, message="issue_automation_disabled")

    github = client or GhClient(config.gh_command)
    ledger = Ledger(config.state_dir / "ledger.sqlite3")

    if is_paused(config):
        return IssueSweepOutcome(exit_code=OK, message="paused_inspect_only")
    if ledger.circuit_open():
        return IssueSweepOutcome(
            exit_code=HELD,
            message="circuit_open:" + (ledger.circuit_reason() or "unknown"),
        )
    if ledger.active_job() is not None:
        return IssueSweepOutcome(exit_code=OK, message="busy_inspect_only")

    discovered = discover_issue_signals(github, config)
    pending: List[IssueSignalV1] = []
    for signal in discovered.signals:
        key = _issue_claim_key(signal)
        if ledger.is_processed(key):
            continue
        pending.append(signal)

    if not pending:
        return IssueSweepOutcome(
            exit_code=OK,
            message="no_actionable_signal",
            inspected_issues=discovered.inspected_issues,
            signals_found=0,
        )

    # One serial job per sweep.
    signal = pending[0]
    claim_key = _issue_claim_key(signal)
    claim_result, job_id, detail = ledger.try_claim(
        repository=signal.repository,
        pr_number=signal.issue_number,
        thread_node_id="issue",
        latest_comment_node_id=signal.observed_updated_at,
        observed_head_sha=signal.observed_base_sha,
        signal_digest=issue_signal_digest(signal),
        reclaim_after_seconds=config.reclaim_after_seconds,
    )
    if claim_result == "duplicate":
        return IssueSweepOutcome(
            exit_code=OK,
            message="no_actionable_signal",
            inspected_issues=discovered.inspected_issues,
            signals_found=0,
        )
    if claim_result == "busy":
        return IssueSweepOutcome(
            exit_code=OK,
            message="busy_inspect_only",
            inspected_issues=discovered.inspected_issues,
            signals_found=len(pending),
        )
    if claim_result == "circuit_open":
        return IssueSweepOutcome(
            exit_code=HELD,
            message=f"circuit_open:{detail or 'unknown'}",
            inspected_issues=discovered.inspected_issues,
            signals_found=len(pending),
        )
    if claim_result != "claimed" or not job_id:
        ledger.open_circuit("claim_state_invalid")
        return IssueSweepOutcome(
            exit_code=HELD,
            message="claim_state_invalid",
            inspected_issues=discovered.inspected_issues,
            signals_found=len(pending),
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
    except RunnerContractError as exc:
        ledger.fail_job_open_circuit(job_id, claim_key, str(exc))
        receipt = _receipt(
            config,
            signal=signal,
            outcome="held",
            checks=[],
            resulting_sha=None,
            hold_reason=str(exc),
        )
        return IssueSweepOutcome(
            exit_code=HELD,
            message=str(exc),
            inspected_issues=discovered.inspected_issues,
            signals_found=len(pending),
            receipt_path=receipt,
            receipt_paths=[receipt],
            jobs_held=1,
        )

    outcome = run_claimed_issue_job(
        config,
        ledger,
        github,
        signal,
        job_id,
        claim_key,
        runner_environment,
    )
    return IssueSweepOutcome(
        exit_code=OK if outcome.completed else HELD,
        message=outcome.message if outcome.completed else f"held:{outcome.message}",
        inspected_issues=discovered.inspected_issues,
        signals_found=len(pending),
        receipt_path=outcome.receipt_path,
        receipt_paths=[outcome.receipt_path],
        jobs_completed=1 if outcome.completed else 0,
        jobs_held=0 if outcome.completed else 1,
    )


# CLI-facing aliases used by agent_ops.cli
inspect_issues = inspect_issue_work
run_issue_sweep = issue_sweep
