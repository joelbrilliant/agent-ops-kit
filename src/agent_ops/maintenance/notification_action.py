"""Bounded Oscar execution for actionable PR notifications.

The orchestrator owns GitHub credentials, worktrees, verification, pushes,
comments and durable claims. Oscar receives an isolated worktree and must return
one strict terminal response. No detached worker or mutable job JSON is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from agent_ops.audit.redaction import assert_no_private_material, redact_text
from agent_ops.config import Config
from agent_ops.contracts import TaskSpecV1
from agent_ops.github.client import GitHubClient, GitHubError
from agent_ops.maintenance.ledger import Ledger, make_claim_key
from agent_ops.maintenance.review_fix import (
    _validate_local_candidate,
    _validate_reply_draft,
)
from agent_ops.maintenance.worktree import (
    base_is_ancestor,
    create_worktree,
    current_head,
    diff_text,
    push_head_no_force,
    remote_ref_sha,
    remove_worktree,
    worktree_is_clean,
)
from agent_ops.process import RunnerError
from agent_ops.qa.verify import all_passed, run_named_verifications
from agent_ops.runners.runner import (
    RunnerContractError,
    build_runner_environment,
    prove_github_capability_isolation,
    run_notification_worker,
    run_reviewer,
    write_owner_only_json,
)


@dataclass(frozen=True)
class NotificationActionRequest:
    thread_id: str
    updated_at: str
    repository: str
    pr_number: int
    reason: str
    subject_title: str
    notification_reason: str
    related_url: str
    latest_comment_url: str = ""


@dataclass(frozen=True)
class NotificationActionOutcome:
    outcome: str
    summary: str
    proposed_fix: str = ""
    resulting_sha: str = ""
    comment_id: int = 0
    receipt_path: Optional[Path] = None


def _load_pr(client: GitHubClient, repository: str, pr_number: int) -> Dict[str, Any]:
    payload = client.rest_get(f"repos/{repository}/pulls/{int(pr_number)}")
    if not isinstance(payload, dict):
        raise GitHubError("pull request payload invalid")
    return payload


def _latest_comment_body(client: GitHubClient, url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path.lstrip("/") if parsed.scheme else url.lstrip("/")
    if not path.startswith("repos/"):
        return ""
    try:
        payload = client.rest_get(path)
    except GitHubError:
        return ""
    return str(payload.get("body") or "")


def _verification_commands(config: Config, repository: str) -> Dict[str, List[str]]:
    commands = dict(config.default_verification_commands)
    policy = config.policy_for(repository)
    if policy:
        commands.update(policy.verification_commands)
    if not commands:
        raise RunnerContractError("verification_evidence_missing")
    return commands


def _allowed_paths(config: Config, repository: str) -> List[str]:
    policy = config.policy_for(repository)
    paths = list(policy.permitted_paths) if policy else []
    if not paths:
        raise RunnerContractError("notification_policy_paths_missing")
    return paths


def _head_snapshot(
    config: Config,
    client: GitHubClient,
    request: NotificationActionRequest,
) -> Dict[str, str]:
    pr = _load_pr(client, request.repository, request.pr_number)
    if str(pr.get("state") or "").lower() != "open" or bool(pr.get("merged")):
        return {"terminal": "no_action", "summary": "PR is already closed or merged."}
    author = str((pr.get("user") or {}).get("login") or "").lower()
    if config.operator_logins and author not in {
        login.lower() for login in config.operator_logins
    }:
        raise RunnerContractError("notification_pr_not_operator_authored")
    head = pr.get("head") or {}
    head_repo = head.get("repo") or {}
    repository = str(head_repo.get("full_name") or request.repository)
    clone_url = str(head_repo.get("clone_url") or "")
    if not clone_url:
        html_url = str(head_repo.get("html_url") or f"https://github.com/{repository}")
        clone_url = html_url.rstrip("/") + ".git"
    sha = str(head.get("sha") or "")
    ref = str(head.get("ref") or "")
    if not sha or not ref or not repository or not clone_url:
        raise RunnerContractError("notification_pr_head_incomplete")
    return {
        "sha": sha,
        "ref": ref,
        "head_repository": repository,
        "clone_url": clone_url,
        "url": str(pr.get("html_url") or request.related_url),
    }


def _post_and_verify_comment(
    client: GitHubClient,
    *,
    repository: str,
    pr_number: int,
    body: str,
) -> int:
    comment_id = client.create_pr_comment(repository, pr_number, body)
    readback = client.get_issue_comment(repository, comment_id)
    if int(readback.get("id") or 0) != comment_id or str(readback.get("body") or "") != body:
        raise RunnerContractError("notification_reply_readback_mismatch")
    return comment_id


def _write_receipt(
    config: Config,
    request: NotificationActionRequest,
    *,
    outcome: str,
    base_sha: str,
    resulting_sha: str,
    check_ids: Sequence[str],
    comment_id: int,
    summary: str,
) -> Path:
    safe_id = "".join(character for character in request.thread_id if character.isalnum())[:40]
    path = config.state_dir / "receipts" / f"notify-{safe_id or 'unknown'}-{base_sha[:12]}.json"
    write_owner_only_json(
        path,
        {
            "schema": "NotificationActionReceiptV1",
            "thread_id": request.thread_id,
            "updated_at": request.updated_at,
            "repository": request.repository,
            "pr_number": request.pr_number,
            "reason": request.reason,
            "outcome": outcome,
            "base_sha": base_sha,
            "resulting_sha": resulting_sha,
            "named_checks": list(check_ids),
            "comment_id": int(comment_id),
            "summary": summary,
        },
    )
    return path


def run_notification_action(
    config: Config,
    ledger: Ledger,
    client: GitHubClient,
    request: NotificationActionRequest,
    *,
    attempt: int,
) -> NotificationActionOutcome:
    """Run one serial PR notification job to a verified terminal state."""
    command = list(config.notification_triage.action_worker_command)
    if not command:
        raise RunnerContractError("notification_action_worker_not_configured")
    snapshot = _head_snapshot(config, client, request)
    if snapshot.get("terminal") == "no_action":
        return NotificationActionOutcome(
            outcome="no_action",
            summary=snapshot.get("summary", "No action remains."),
        )
    base_sha = snapshot["sha"]
    claim_key = make_claim_key(
        request.repository,
        request.pr_number,
        f"notification:{request.thread_id}",
        f"{request.updated_at}:attempt:{int(attempt)}",
        base_sha,
    )
    claim_result, job_id, detail = ledger.try_claim(
        repository=request.repository,
        pr_number=request.pr_number,
        thread_node_id=f"notification:{request.thread_id}",
        latest_comment_node_id=f"{request.updated_at}:attempt:{int(attempt)}",
        observed_head_sha=base_sha,
        signal_digest=claim_key,
        reclaim_after_seconds=config.reclaim_after_seconds,
    )
    if claim_result == "busy":
        raise RunnerContractError("notification_action_busy")
    if claim_result == "circuit_open":
        raise RunnerContractError(f"circuit_open:{detail or 'unknown'}")
    if claim_result != "claimed" or not job_id:
        raise RunnerContractError(f"notification_claim_{claim_result}")

    worktree: Optional[Path] = None
    resulting_sha = base_sha
    pushed = False
    try:
        ledger.mark_running(job_id, claim_key)
        runner_environment = build_runner_environment(
            state_dir=config.state_dir,
            allowlist=config.runner_environment_allowlist,
        )
        prove_github_capability_isolation(
            client=client,
            operator_logins=config.operator_logins,
            gh_command=config.gh_command,
            runner_environment=runner_environment,
        )
        allowed_paths = _allowed_paths(config, request.repository)
        verification_commands = _verification_commands(config, request.repository)
        observed_remote = remote_ref_sha(
            config.git_command, snapshot["clone_url"], snapshot["ref"]
        )
        if observed_remote != base_sha:
            raise RunnerContractError("notification_head_stale_before_build")
        worktree = create_worktree(
            git_cmd=config.git_command,
            workspace_root=config.workspace_root,
            repository=snapshot["head_repository"],
            clone_url=snapshot["clone_url"],
            head_sha=base_sha,
            pr_number=request.pr_number,
            branch_name=snapshot["ref"],
        )
        if current_head(config.git_command, worktree) != base_sha:
            raise RunnerContractError("notification_worktree_wrong_head")
        try:
            checks = client.required_checks(request.repository, request.pr_number)
            if not checks:
                all_checks = getattr(client, "all_checks", None)
                checks = all_checks(request.repository, request.pr_number) if callable(all_checks) else []
        except GitHubError:
            checks = []
        task = TaskSpecV1(
            goal=(
                "Resolve this GitHub notification end-to-end. Make the smallest safe code fix, "
                "or return reply_only, no_action or broken with evidence."
            ),
            base_sha=base_sha,
            permitted_paths=allowed_paths,
            permitted_actions=["edit", "test", "commit", "draft_reply"],
            acceptance_criteria=[
                "resolve the notification rather than forwarding routine triage to Joel",
                "keep all changes inside repository policy paths",
                "leave a clean worktree and one evidence-backed terminal response",
            ],
            non_goals=["merge", "force_push", "deploy", "permission_changes"],
            approval_class="standing_github_notification_work",
            repository=request.repository,
            pr_number=request.pr_number,
        )
        worker_request = {
            "schema": "NotificationWorkerRequestV1",
            "task": task.to_dict(),
            "notification": {
                "thread_id": request.thread_id,
                "updated_at": request.updated_at,
                "reason": request.reason,
                "notification_reason": request.notification_reason,
                "subject_title": request.subject_title,
                "related_url": request.related_url,
                "untrusted_latest_comment": _latest_comment_body(
                    client, request.latest_comment_url
                ),
            },
            "pull_request": {
                "repository": request.repository,
                "number": request.pr_number,
                "url": snapshot["url"],
                "head_sha": base_sha,
                "head_ref": snapshot["ref"],
                "checks": checks,
            },
        }
        ledger.mark_phase(job_id, "notification_worker")
        worker = run_notification_worker(
            command,
            request_payload=worker_request,
            base_sha=base_sha,
            state_dir=config.state_dir,
            worktree_path=worktree,
            run_id=job_id,
            required_identity=config.required_runner_identity,
            runner_environment=runner_environment,
            timeout=config.runner_timeout_seconds,
        )
        resulting_sha = current_head(config.git_command, worktree)
        if worker.resulting_sha != resulting_sha:
            raise RunnerContractError("notification_worker_resulting_sha_mismatch")
        safe_summary = redact_text(worker.summary, config.private_markers)
        safe_proposed_fix = redact_text(worker.proposed_fix, config.private_markers)

        if worker.outcome in {"no_action", "reply_only", "broken"}:
            if resulting_sha != base_sha or not worktree_is_clean(config.git_command, worktree):
                raise RunnerContractError("notification_non_fix_mutated_worktree")
            comment_id = 0
            if worker.outcome == "reply_only":
                privacy = assert_no_private_material(worker.reply_draft, config.private_markers)
                if privacy:
                    raise RunnerContractError(
                        "notification_reply_privacy_failure:" + ",".join(privacy)
                    )
                comment_id = _post_and_verify_comment(
                    client,
                    repository=request.repository,
                    pr_number=request.pr_number,
                    body=worker.reply_draft,
                )
            ledger.complete_job(
                job_id,
                claim_key,
                outcome="held" if worker.outcome == "broken" else "completed",
                resulting_sha=base_sha,
                reply_node_id=str(comment_id) if comment_id else None,
                hold_reason=safe_summary if worker.outcome == "broken" else None,
            )
            receipt = _write_receipt(
                config,
                request,
                outcome=worker.outcome,
                base_sha=base_sha,
                resulting_sha=base_sha,
                check_ids=[],
                comment_id=comment_id,
                summary=safe_summary,
            )
            return NotificationActionOutcome(
                outcome=worker.outcome,
                summary=safe_summary,
                proposed_fix=safe_proposed_fix,
                resulting_sha=base_sha,
                comment_id=comment_id,
                receipt_path=receipt,
            )

        candidate_sha, _ = _validate_local_candidate(
            config,
            worktree,
            base_sha,
            allowed_paths,
            worker.changed_paths,
        )
        if candidate_sha != worker.resulting_sha:
            raise RunnerContractError("notification_worker_candidate_sha_mismatch")
        ledger.mark_phase(job_id, "notification_verifying")
        verification = run_named_verifications(
            verification_commands,
            cwd=worktree,
            subject_ref=candidate_sha,
            timeout=config.runner_timeout_seconds,
        )
        if not all_passed(verification):
            raise RunnerContractError("notification_worker_verification_failed")
        ledger.mark_phase(job_id, "notification_reviewing")
        reviewer = run_reviewer(
            config.reviewer_command,
            task=task,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            diff_text=diff_text(config.git_command, worktree, base_sha),
            verification=[check.to_dict() for check in verification],
            state_dir=config.state_dir,
            worktree_path=worktree,
            run_id=job_id,
            classifier_identity=worker.identity,
            required_identity=config.required_runner_identity,
            runner_environment=runner_environment,
            timeout=config.runner_timeout_seconds,
        )
        resulting_sha, _ = _validate_local_candidate(
            config, worktree, base_sha, allowed_paths
        )
        if reviewer.resulting_sha != resulting_sha or reviewer.verdict != "PASS":
            raise RunnerContractError("notification_reviewer_hold")
        ledger.mark_phase(job_id, "notification_final_verifying")
        verification = run_named_verifications(
            verification_commands,
            cwd=worktree,
            subject_ref=resulting_sha,
            timeout=config.runner_timeout_seconds,
        )
        if not all_passed(verification):
            raise RunnerContractError("notification_final_verification_failed")
        check_ids = [check.check_id for check in verification]
        _validate_reply_draft(
            reviewer.reply_draft,
            resulting_sha,
            check_ids,
            config.private_markers,
        )
        if not base_is_ancestor(config.git_command, worktree, base_sha):
            raise RunnerContractError("notification_history_integrity_failed")
        live = _head_snapshot(config, client, request)
        if live.get("sha") != base_sha or live.get("ref") != snapshot["ref"]:
            raise RunnerContractError("notification_pr_stale_before_push")
        if remote_ref_sha(
            config.git_command, snapshot["clone_url"], snapshot["ref"]
        ) != base_sha:
            raise RunnerContractError("notification_remote_stale_before_push")
        ledger.mark_phase(job_id, "notification_pushing")
        push = push_head_no_force(
            git_cmd=config.git_command,
            worktree=worktree,
            remote_url=snapshot["clone_url"],
            head_ref=snapshot["ref"],
        )
        if not push.ok:
            raise RunnerContractError("notification_push_failed")
        pushed = True
        if remote_ref_sha(
            config.git_command, snapshot["clone_url"], snapshot["ref"]
        ) != resulting_sha:
            raise RunnerContractError("notification_push_attestation_failed")
        ledger.mark_phase(job_id, "notification_replying")
        comment_id = _post_and_verify_comment(
            client,
            repository=request.repository,
            pr_number=request.pr_number,
            body=reviewer.reply_draft,
        )
        ledger.complete_job(
            job_id,
            claim_key,
            outcome="completed",
            resulting_sha=resulting_sha,
            reply_node_id=str(comment_id),
        )
        receipt = _write_receipt(
            config,
            request,
            outcome="fixed",
            base_sha=base_sha,
            resulting_sha=resulting_sha,
            check_ids=check_ids,
            comment_id=comment_id,
            summary=safe_summary,
        )
        return NotificationActionOutcome(
            outcome="fixed",
            summary=safe_summary,
            resulting_sha=resulting_sha,
            comment_id=comment_id,
            receipt_path=receipt,
        )
    except (RunnerError, GitHubError, OSError, ValueError) as exc:
        reason = redact_text(str(exc) or exc.__class__.__name__, config.private_markers)
        if pushed:
            ledger.fail_job_open_circuit(
                job_id,
                claim_key,
                "notification_failure_after_push:" + reason,
            )
        else:
            ledger.complete_job(
                job_id,
                claim_key,
                outcome="held",
                resulting_sha=resulting_sha if resulting_sha != base_sha else None,
                hold_reason=reason,
            )
        raise RunnerContractError(reason) from exc
    finally:
        if worktree is not None:
            try:
                remove_worktree(
                    config.git_command,
                    config.workspace_root,
                    snapshot["head_repository"],
                    worktree,
                )
            except (RunnerError, OSError):
                ledger.open_circuit("notification_worktree_cleanup_failed")
