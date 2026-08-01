"""Issue-to-draft-PR sweep with fail-closed mutation gates."""

from __future__ import annotations

import codecs
import os
import re
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
    commit_messages,
    create_worktree,
    current_head,
    diff_text,
    local_config_fingerprint,
    push_head_no_force,
    remote_ref_sha,
    remove_worktree,
    validate_path_bounds,
    worktree_is_clean,
)
from agent_ops.paths import is_path_allowed, is_safe_repo_path, normalize_repo_path
from agent_ops.process import RunnerError
from agent_ops.qa.recovery import (
    final_check_ids_match,
    initial_failure_is_repairable,
    recovery_markers,
)
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
    # Packet AC-7: repository + issue + conversation digest + observed default-branch tip SHA
    return make_claim_key(
        signal.repository,
        signal.issue_number,
        "issue",
        signal.conversation_digest,
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


def _assert_repository_authority(
    client: GitHubClient,
    repository: str,
    operator_logins: Sequence[str],
) -> None:
    data = client.rest_get(f"/repos/{repository}")
    if not isinstance(data, dict):
        raise RunnerContractError("repository_authority_payload_invalid")
    if str(data.get("full_name") or "").lower() != repository.lower():
        raise RunnerContractError("repository_identity_mismatch")
    if data.get("private") is not False:
        raise RunnerContractError("repository_not_public")
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        raise RunnerContractError("repository_permissions_missing")
    if not any(permissions.get(name) is True for name in ("push", "maintain", "admin")):
        raise RunnerContractError("repository_push_permission_missing")
    owner = str((data.get("owner") or {}).get("login") or "").lower()
    if not owner or owner not in {login.lower() for login in operator_logins}:
        raise RunnerContractError("repository_not_operator_owned")


def _assert_issue_last_safe_point(
    config: Config,
    issue_cfg: IssueAutomationConfig,
    client: GitHubClient,
    signal: IssueSignalV1,
    *,
    branch: str,
    stale_reason: str,
    decision: DecisionV1,
    allowed_paths: Sequence[str],
    verification_commands: Dict[str, List[str]],
) -> IssueSignalV1:
    """Re-prove AC-4 gates immediately before worktree create and before mutation."""
    if config.exact_policy_for(signal.repository) is None:
        raise RunnerContractError("missing_exact_repository_policy")
    expected_base = issue_cfg.enabled_repositories.get(signal.repository)
    if expected_base is None:
        raise RunnerContractError("repository_not_enabled_for_issue_automation")
    if expected_base != signal.base_ref:
        raise RunnerContractError("issue_base_ref_policy_mismatch")
    if _policy_paths(config, signal.repository) != list(allowed_paths):
        raise RunnerContractError("issue_path_policy_changed")
    if _select_verifications(config, signal.repository, decision) != verification_commands:
        raise RunnerContractError("issue_verification_policy_changed")

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
        and not all(label in live.labels for label in issue_cfg.require_labels)
    ):
        raise RunnerContractError("label_removed_before_push")
    if meta is None or meta.get("is_private") is not False:
        raise RunnerContractError("repository_not_public")
    _assert_repository_authority(client, signal.repository, config.operator_logins)
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


def _effective_private_markers(config: Config) -> List[str]:
    markers = list(config.private_markers)
    markers.extend((str(config.workspace_root), str(config.state_dir), str(Path.home())))
    return list(dict.fromkeys(marker for marker in markers if marker))


def _untrusted_text_markers(signal: IssueSignalV1) -> List[str]:
    values = [signal._raw_title, signal._raw_body]
    values.extend(
        str(comment.get("body") or "")
        for comment in signal._raw_comments
        if isinstance(comment, dict)
    )
    markers: List[str] = []
    for value in values:
        stripped = value.strip()
        if len(stripped) >= 24:
            markers.append(stripped)
        markers.extend(
            line.strip() for line in value.splitlines() if len(line.strip()) >= 24
        )
    return list(dict.fromkeys(markers))


def _assert_public_text_safe(
    text: str,
    signal: IssueSignalV1,
    private_markers: Sequence[str],
) -> None:
    markers = list(private_markers) + _untrusted_text_markers(signal)
    if assert_no_private_material(text, markers):
        raise RunnerContractError("public_material_privacy_failure")


def _assert_public_candidate_safe(
    config: Config,
    signal: IssueSignalV1,
    worktree: Path,
    base_sha: str,
    changed: Sequence[str],
) -> None:
    markers = _effective_private_markers(config) + _untrusted_text_markers(signal)
    public_text = "\n".join(
        (
            diff_text(config.git_command, worktree, base_sha),
            commit_messages(config.git_command, worktree, base_sha),
            "\n".join(changed),
        )
    )
    if assert_no_private_material(public_text, markers):
        raise RunnerContractError("public_candidate_privacy_failure")

    carry_limit = max([512, *(len(marker) for marker in markers)])
    for relative in changed:
        target = worktree / relative
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink():
            _assert_public_text_safe(os.readlink(target), signal, markers)
            continue
        if not target.is_file():
            raise RunnerContractError("changed_path_not_regular_file")
        decoder = codecs.getincrementaldecoder("utf-8")("ignore")
        carry = ""
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    text = carry + decoder.decode(b"", final=True)
                    if text and assert_no_private_material(text, markers):
                        raise RunnerContractError("public_candidate_privacy_failure")
                    break
                text = carry + decoder.decode(chunk)
                if assert_no_private_material(text, markers):
                    raise RunnerContractError("public_candidate_privacy_failure")
                carry = text[-carry_limit:]


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


_CLOSING_ISSUE_REFERENCE_RE = re.compile(
    r"(?i)\b(?:close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?)\s+"
    r"(?P<reference>"
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+"
    r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+"
    r"|GH-\d+"
    r"|#\d+"
    r")\b"
)


def _prepare_pr_body(body: str, issue_number: int) -> str:
    expected_reference = f"#{issue_number}"
    references = [
        match.group("reference")
        for match in _CLOSING_ISSUE_REFERENCE_RE.finditer(body)
    ]
    if any(reference.lower() != expected_reference.lower() for reference in references):
        raise RunnerContractError("pr_body_expands_issue_close_authority")
    if expected_reference not in references:
        return body.rstrip() + f"\n\nCloses {expected_reference}\n"
    return body


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
        hold_reason=redact_text(hold_reason or "", _effective_private_markers(config)) or None,
        redaction_record=default_redaction_record(),
    )
    return write_issue_receipt(config.state_dir, receipt, _effective_private_markers(config))


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
    safe_reason = redact_text(reason, _effective_private_markers(config))
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
    """Deterministic pre-worktree hold for explicit unsafe exception markers."""
    lower = _issue_text_blob(signal).lower()
    markers = {
        "ignore previous": "prompt_injection",
        "prompt injection": "prompt_injection",
        "exfiltrat": "security_sensitive_work",
        "credential": "credential_change",
        "api key": "credential_change",
        "force push": "destructive_work",
        "drop table": "destructive_work",
        "delete the database": "destructive_work",
        "delete history": "destructive_work",
        "product direction": "product_direction",
        "product roadmap": "product_direction",
        "rewrite the architecture": "architecture_change",
        "conflicting requirements": "ambiguous_acceptance",
        "acceptance criteria conflict": "ambiguous_acceptance",
        "another repository": "cross_repository_change",
        "cross-repo": "cross_repository_change",
        "ci workflow": "workflow_change",
        ".github/workflows": "workflow_change",
        "repository permissions": "permission_change",
        "repository settings": "settings_change",
        "branch protection": "settings_change",
        "vendor bundle": "generated_or_vendor_change",
        "generated fixture": "generated_or_vendor_change",
        "schema migration": "database_migration",
        "database migration": "database_migration",
        "destructive data loss": "database_migration",
        "production deploy": "production_effects",
        "change permissions": "permission_change",
    }
    for marker, reason in markers.items():
        if marker in lower:
            return reason
    return None


def run_claimed_issue_job(
    config: Config,
    ledger: Ledger,
    client: GitHubClient,
    signal: IssueSignalV1,
    job_id: str,
    claim_key: str,
    build_environment: Dict[str, str],
    review_environment: Dict[str, str],
) -> IssueJobOutcome:
    issue_cfg = _require_issue_cfg(config)
    checks: List[CheckResultV1] = []
    worktree: Optional[Path] = None
    resulting_sha: Optional[str] = None
    branch = _branch_name(issue_cfg, signal)
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
            runner_environment=build_environment,
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
                reason="classifier_hold",
                checks=checks,
                resulting_sha=None,
                circuit=False,
                branch_name=branch,
            )

        prove_github_capability_isolation(
            client=client,
            operator_logins=config.operator_logins,
            gh_command=config.gh_command,
            runner_environment=build_environment,
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
            decision=decision,
            allowed_paths=allowed_paths,
            verification_commands=verification_commands,
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
        trusted_git_config = local_config_fingerprint(config.git_command, worktree)

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
            runner_environment=build_environment,
            timeout=config.runner_timeout_seconds,
        )
        prove_github_capability_isolation(
            client=client,
            operator_logins=config.operator_logins,
            gh_command=config.gh_command,
            runner_environment=build_environment,
        )
        if local_config_fingerprint(config.git_command, worktree) != trusted_git_config:
            raise RunnerContractError("runner_modified_git_config")
        candidate_sha, candidate_paths = _validate_local_candidate(
            config,
            worktree,
            signal.observed_base_sha,
            allowed_paths,
            builder.changed_paths,
            issue_cfg=issue_cfg,
        )
        _assert_public_candidate_safe(
            config, signal, worktree, signal.observed_base_sha, candidate_paths
        )
        if builder.resulting_sha != candidate_sha:
            raise RunnerContractError("builder_resulting_sha_mismatch")

        ledger.mark_phase(job_id, "verifying")
        checks = run_named_verifications(
            verification_commands,
            cwd=worktree,
            subject_ref=candidate_sha,
            timeout=config.runner_timeout_seconds,
            env=build_environment,
        )
        initial_verification_checks = checks
        verified_candidate_sha, candidate_paths = _validate_local_candidate(
            config,
            worktree,
            signal.observed_base_sha,
            allowed_paths,
            builder.changed_paths,
            issue_cfg=issue_cfg,
        )
        if verified_candidate_sha != candidate_sha:
            raise RunnerContractError("verification_mutated_candidate")
        if local_config_fingerprint(config.git_command, worktree) != trusted_git_config:
            raise RunnerContractError("verification_modified_git_config")
        _assert_public_candidate_safe(
            config, signal, worktree, signal.observed_base_sha, candidate_paths
        )
        recovery_required = not all_passed(initial_verification_checks)
        if recovery_required and not initial_failure_is_repairable(
            initial_verification_checks, expected_subject_ref=candidate_sha
        ):
            raise RunnerContractError("builder_verification_failed")

        ledger.mark_phase(job_id, "reviewing")
        prove_github_capability_isolation(
            client=client,
            operator_logins=config.operator_logins,
            gh_command=config.gh_command,
            runner_environment=review_environment,
        )
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
            runner_environment=review_environment,
            timeout=config.runner_timeout_seconds,
        )
        prove_github_capability_isolation(
            client=client,
            operator_logins=config.operator_logins,
            gh_command=config.gh_command,
            runner_environment=review_environment,
        )
        if local_config_fingerprint(config.git_command, worktree) != trusted_git_config:
            raise RunnerContractError("reviewer_modified_git_config")
        resulting_sha, resulting_paths = _validate_local_candidate(
            config,
            worktree,
            signal.observed_base_sha,
            allowed_paths,
            issue_cfg=issue_cfg,
        )
        _assert_public_candidate_safe(
            config, signal, worktree, signal.observed_base_sha, resulting_paths
        )
        if reviewer.resulting_sha != resulting_sha:
            raise RunnerContractError("reviewer_resulting_sha_mismatch")
        if reviewer.verdict != "PASS":
            raise RunnerContractError("reviewer_hold")
        if recovery_required and not base_is_ancestor(
            config.git_command, worktree, candidate_sha
        ):
            raise RunnerContractError("reviewer_recovery_rewrote_candidate")

        ledger.mark_phase(job_id, "final_verifying")
        final_checks = run_named_verifications(
            verification_commands,
            cwd=worktree,
            subject_ref=resulting_sha,
            timeout=config.runner_timeout_seconds,
            env=review_environment,
        )
        checks = list(final_checks)
        if not final_check_ids_match(initial_verification_checks, final_checks):
            raise RunnerContractError("final_verification_check_ids_changed")
        if not all_passed(final_checks):
            raise RunnerContractError("final_verification_failed")
        final_verified_sha, resulting_paths = _validate_local_candidate(
            config,
            worktree,
            signal.observed_base_sha,
            allowed_paths,
            issue_cfg=issue_cfg,
        )
        if final_verified_sha != resulting_sha:
            raise RunnerContractError("final_verification_mutated_candidate")
        if local_config_fingerprint(config.git_command, worktree) != trusted_git_config:
            raise RunnerContractError("final_verification_modified_git_config")
        _assert_public_candidate_safe(
            config, signal, worktree, signal.observed_base_sha, resulting_paths
        )
        if recovery_required and (
            resulting_sha == candidate_sha
            or not changed_files(config.git_command, worktree, candidate_sha)
        ):
            raise RunnerContractError("reviewer_recovery_missing_committed_change")
        completed_receipt_checks = list(final_checks)
        if recovery_required:
            completed_receipt_checks.extend(
                recovery_markers(
                    initial_verification_checks,
                    final_checks,
                    initial_candidate_sha=candidate_sha,
                    final_reviewer_sha=resulting_sha,
                )
            )
        check_ids = [check.check_id for check in final_checks]
        _validate_reply_draft(
            reviewer.reply_draft,
            resulting_sha,
            check_ids,
            _effective_private_markers(config),
        )
        pr_title = _compose_pr_title(issue_cfg, signal, reviewer.pr_title)
        pr_body = _prepare_pr_body(reviewer.pr_body, signal.issue_number)
        _assert_public_text_safe(
            pr_title, signal, _effective_private_markers(config)
        )
        _assert_public_text_safe(
            pr_body, signal, _effective_private_markers(config)
        )

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
            decision=decision,
            allowed_paths=allowed_paths,
            verification_commands=verification_commands,
        )
        pre_push_sha, resulting_paths = _validate_local_candidate(
            config,
            worktree,
            signal.observed_base_sha,
            allowed_paths,
            issue_cfg=issue_cfg,
        )
        if pre_push_sha != resulting_sha:
            raise RunnerContractError("candidate_changed_before_push")
        if local_config_fingerprint(config.git_command, worktree) != trusted_git_config:
            raise RunnerContractError("git_config_changed_before_push")
        _assert_public_candidate_safe(
            config, signal, worktree, signal.observed_base_sha, resulting_paths
        )

        ledger.mark_phase(job_id, "pushing")
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
            title=pr_title,
            body=pr_body,
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
            _effective_private_markers(config),
        )
        _assert_public_text_safe(
            final_reply, signal, _effective_private_markers(config)
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
            checks=completed_receipt_checks,
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
        reason = redact_text(str(exc) or exc.__class__.__name__, _effective_private_markers(config))
        # Every exception after a ROUTINE classification requires operator diagnosis.
        # This includes stale state and all mutation-ambiguous failures.
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
    ledger.reclaim_stale_jobs(config.reclaim_after_seconds)
    if ledger.circuit_open():
        return IssueSweepOutcome(
            exit_code=HELD,
            message="circuit_open:" + (ledger.circuit_reason() or "unknown"),
        )
    if ledger.active_job() is not None:
        return IssueSweepOutcome(exit_code=OK, message="busy_inspect_only")

    try:
        discovered = discover_issue_signals(github, config)
    except (GitHubError, RunnerContractError, ValueError):
        ledger.open_circuit("issue_discovery_failed")
        return IssueSweepOutcome(exit_code=HELD, message="issue_discovery_failed")
    pending: List[IssueSignalV1] = []
    for signal in discovered.signals:
        if ledger.has_completed_issue(signal.repository, signal.issue_number):
            continue
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
        latest_comment_node_id=signal.conversation_digest,
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

    try:
        build_environment = build_runner_environment(
            state_dir=config.state_dir / "issue-build",
            allowlist=config.runner_environment_allowlist,
        )
        review_environment = build_runner_environment(
            state_dir=config.state_dir / "issue-review",
            allowlist=config.runner_environment_allowlist,
        )
        for environment in (build_environment, review_environment):
            prove_github_capability_isolation(
                client=github,
                operator_logins=config.operator_logins,
                gh_command=config.gh_command,
                runner_environment=environment,
            )
    except (RunnerContractError, RunnerError, GitHubError, OSError, ValueError) as exc:
        held = _hold_job(
            config,
            ledger,
            signal=signal,
            job_id=job_id,
            claim_key=claim_key,
            reason=str(exc) or exc.__class__.__name__,
            checks=[],
            resulting_sha=None,
            circuit=True,
        )
        return IssueSweepOutcome(
            exit_code=HELD,
            message=held.message,
            inspected_issues=discovered.inspected_issues,
            signals_found=len(pending),
            receipt_path=held.receipt_path,
            receipt_paths=[held.receipt_path],
            jobs_held=1,
        )

    outcome = run_claimed_issue_job(
        config,
        ledger,
        github,
        signal,
        job_id,
        claim_key,
        build_environment,
        review_environment,
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
