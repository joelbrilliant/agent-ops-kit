"""Production-shaped notification worker lifecycle tests."""

from __future__ import annotations

import json
import subprocess
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from agent_ops.config import Config
from agent_ops.github.client import FakeGitHub, GitHubError
from agent_ops.maintenance.ledger import Ledger
from agent_ops.maintenance.notification_action import (
    NotificationActionRequest,
    run_notification_action,
)
from agent_ops.maintenance.notify_triage import triage_notifications
from agent_ops.runners.runner import RunnerContractError
from tests.conftest import make_config, sys_executable, write_executable
from tests.test_sweep_e2e import init_head_repo, make_bare_remote


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _worker(path: Path, *, outcome: str = "fixed", exit_code: int = 0) -> Path:
    mutation = ""
    if outcome == "fixed":
        mutation = textwrap.dedent(
            """
            target = wt / 'demo.txt'
            target.write_text(target.read_text().replace('broken=1', 'broken=0'))
            subprocess.run(['git', 'config', 'user.email', 'oscar@example.invalid'], cwd=wt, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Oscar'], cwd=wt, check=True)
            subprocess.run(['git', 'add', '--', 'demo.txt'], cwd=wt, check=True)
            subprocess.run(['git', 'commit', '-m', 'fix: notification failure'], cwd=wt, check=True)
            """
        )
    return write_executable(
        path,
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, subprocess, sys
            from pathlib import Path
            if {exit_code}:
                raise SystemExit({exit_code})
            args = sys.argv[1:]
            def get(flag):
                return Path(args[args.index(flag) + 1])
            req = json.loads(get('--request').read_text())
            response = get('--response')
            wt = get('--worktree')
            base = req['task']['base_sha']
            {textwrap.indent(mutation, '            ').lstrip()}
            resulting = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=wt, check=True,
                capture_output=True, text=True
            ).stdout.strip()
            changed = subprocess.run(
                ['git', 'diff', '--name-only', base + '..' + resulting], cwd=wt,
                check=True, capture_output=True, text=True
            ).stdout.splitlines()
            reply = ''
            if {outcome!r} in ('fixed', 'reply_only'):
                reply = 'handled at ' + resulting + '. checks: diff, unit'
            response.write_text(json.dumps({{
                'schema': 'NotificationWorkerResponseV1',
                'runner_identity': {{
                    'profile': 'oscar', 'provider': 'openai-codex',
                    'model': 'gpt-5.6-sol', 'reasoning_effort': 'xhigh',
                    'service_tier': 'fast', 'session_id': 'notify-session',
                    'fresh_session': True,
                }},
                'outcome': {outcome!r},
                'base_sha': base,
                'resulting_sha': resulting,
                'changed_paths': changed,
                'summary': 'Resolved the notification safely.',
                'proposed_fix': 'Repair the worker boundary.' if {outcome!r} == 'broken' else '',
                'reply_draft': reply,
                'voice_gate': {{
                    'schema': 'VoiceGateV1',
                    'shared_operator_contract_read': True,
                    'operator_profile_read': True,
                    'skill': 'joel-voice-writing',
                    'reference': 'references/voice.md',
                    'register': 'public-community-short-reply',
                    'passed': True,
                }},
            }}))
            """
        ),
    )


def _configured(
    tmp_path: Path,
    worker: Path,
    *,
    notifier: list[str] | None = None,
    max_attempts: int = 2,
) -> Config:
    cfg = make_config(tmp_path)
    policy = replace(
        cfg.notification_triage,
        enabled=True,
        action_worker_command=[
            sys_executable(),
            str(worker),
            "--request",
            "{request_path}",
            "--response",
            "{response_path}",
            "--worktree",
            "{worktree_path}",
        ],
        max_action_attempts=max_attempts,
        needs_joel_command=list(notifier or []),
    )
    return Config(**{**cfg.__dict__, "notification_triage": policy})


def _wire_pr(
    fake: FakeGitHub,
    *,
    remote: Path,
    sha: str,
    ref: str,
    number: int = 7,
) -> dict:
    payload = {
        "number": number,
        "state": "open",
        "merged": False,
        "draft": False,
        "html_url": f"https://github.com/operator/demo/pull/{number}",
        "user": {"login": "operator"},
        "head": {
            "sha": sha,
            "ref": ref,
            "repo": {
                "full_name": "operator/demo",
                "clone_url": str(remote),
                "html_url": "https://github.com/operator/demo",
            },
        },
        "base": {"repo": {"full_name": "operator/demo"}},
    }
    fake.rest_handlers[f"repos/operator/demo/pulls/{number}"] = payload
    return payload


def _action_request() -> NotificationActionRequest:
    return NotificationActionRequest(
        thread_id="notify-1",
        updated_at="2026-08-02T07:00:00Z",
        repository="operator/demo",
        pr_number=7,
        reason="check_failing_on_current_open_pr",
        subject_title="CI failed for feat/fix branch",
        notification_reason="ci_activity",
        related_url="https://github.com/operator/demo/pull/7",
    )


def test_notification_action_fixes_verifies_pushes_and_comments(tmp_path: Path) -> None:
    head, sha, ref = init_head_repo(tmp_path / "git")
    remote = make_bare_remote(tmp_path / "git", head)
    worker = _worker(tmp_path / "scripts" / "notify.py")
    cfg = _configured(tmp_path / "cfg", worker)
    fake = FakeGitHub()
    _wire_pr(fake, remote=remote, sha=sha, ref=ref)
    ledger = Ledger(cfg.state_dir / "ledger.sqlite3")

    outcome = run_notification_action(
        cfg, ledger, fake, _action_request(), attempt=1
    )

    assert outcome.outcome == "fixed"
    assert outcome.resulting_sha != sha
    assert _git(remote, "rev-parse", f"refs/heads/{ref}") == outcome.resulting_sha
    assert fake.pr_comments
    assert outcome.resulting_sha in fake.pr_comments[0]["body"]
    assert outcome.receipt_path is not None
    receipt = json.loads(outcome.receipt_path.read_text())
    assert receipt["outcome"] == "fixed"
    assert receipt["named_checks"] == ["unit"]


def test_notification_action_no_action_makes_no_mutation_or_comment(tmp_path: Path) -> None:
    head, sha, ref = init_head_repo(tmp_path / "git")
    remote = make_bare_remote(tmp_path / "git", head)
    worker = _worker(tmp_path / "scripts" / "notify.py", outcome="no_action")
    cfg = _configured(tmp_path / "cfg", worker)
    fake = FakeGitHub()
    _wire_pr(fake, remote=remote, sha=sha, ref=ref)
    ledger = Ledger(cfg.state_dir / "ledger.sqlite3")

    outcome = run_notification_action(
        cfg, ledger, fake, _action_request(), attempt=1
    )

    assert outcome.outcome == "no_action"
    assert _git(remote, "rev-parse", f"refs/heads/{ref}") == sha
    assert fake.pr_comments == []


def test_failure_after_push_opens_circuit_instead_of_retrying_mutation(
    tmp_path: Path,
) -> None:
    head, sha, ref = init_head_repo(tmp_path / "git")
    remote = make_bare_remote(tmp_path / "git", head)
    worker = _worker(tmp_path / "scripts" / "notify.py")
    cfg = _configured(tmp_path / "cfg", worker)
    fake = FakeGitHub()
    _wire_pr(fake, remote=remote, sha=sha, ref=ref)

    def fail_readback(_repository: str, _comment_id: int) -> dict:
        raise GitHubError("comment readback failed")

    fake.get_issue_comment = fail_readback  # type: ignore[method-assign]
    ledger = Ledger(cfg.state_dir / "ledger.sqlite3")

    with pytest.raises(RunnerContractError, match="comment readback failed"):
        run_notification_action(cfg, ledger, fake, _action_request(), attempt=1)

    assert ledger.circuit_open() is True
    assert "notification_failure_after_push" in (ledger.circuit_reason() or "")
    assert _git(remote, "rev-parse", f"refs/heads/{ref}") != sha


def _ci_notification() -> dict:
    return {
        "id": "ci-action-1",
        "reason": "ci_activity",
        "unread": True,
        "updated_at": "2026-08-02T07:00:00Z",
        "subject": {
            "title": "CI failed for feat/fix branch",
            "type": "CheckSuite",
            "url": "",
            "latest_comment_url": "",
        },
        "repository": {"full_name": "operator/demo"},
    }


def _wire_ci(fake: FakeGitHub, payload: dict) -> None:
    fake.notifications = [_ci_notification()]
    query = "is:pr is:open repo:operator/demo head:feat/fix"
    fake.search_pages[query] = [[{
        "number": 7,
        "html_url": payload["html_url"],
        "user": {"login": "operator"},
    }]]
    fake.required_check_rows["operator/demo#7"] = [
        {"bucket": "fail", "name": "unit", "state": "FAILURE"}
    ]


def test_triage_notifies_done_only_after_verified_worker_completion(tmp_path: Path) -> None:
    head, sha, ref = init_head_repo(tmp_path / "git")
    remote = make_bare_remote(tmp_path / "git", head)
    worker = _worker(tmp_path / "scripts" / "notify.py")
    sink = tmp_path / "paper-trail.txt"
    notifier = [
        sys_executable(),
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
        str(sink),
        "{message}",
    ]
    cfg = _configured(tmp_path / "cfg", worker, notifier=notifier)
    fake = FakeGitHub()
    payload = _wire_pr(fake, remote=remote, sha=sha, ref=ref)
    _wire_ci(fake, payload)

    outcome = triage_notifications(cfg, client=fake)

    assert outcome.exit_code == 0
    assert outcome.notified_joel == 1
    assert "Oscar handled operator/demo#7" in sink.read_text()
    assert outcome.items[0].decision.decision == "ACTION_FIX"
    assert "ci-action-1" in fake.marked_read
    row = Ledger(cfg.state_dir / "ledger.sqlite3").get_notification("ci-action-1")
    assert row is not None and row["status"] == "processed"


def test_worker_failure_retries_silently_then_reports_broken_with_fix(tmp_path: Path) -> None:
    head, sha, ref = init_head_repo(tmp_path / "git")
    remote = make_bare_remote(tmp_path / "git", head)
    worker = _worker(tmp_path / "scripts" / "notify.py", exit_code=1)
    sink = tmp_path / "paper-trail.txt"
    notifier = [
        sys_executable(),
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
        str(sink),
        "{message}",
    ]
    cfg = _configured(tmp_path / "cfg", worker, notifier=notifier, max_attempts=2)
    fake = FakeGitHub()
    payload = _wire_pr(fake, remote=remote, sha=sha, ref=ref)
    _wire_ci(fake, payload)

    first = triage_notifications(cfg, client=fake)
    assert first.exit_code != 0
    assert not sink.exists()
    assert fake.marked_read == []
    first_row = Ledger(cfg.state_dir / "ledger.sqlite3").get_notification("ci-action-1")
    assert first_row is not None and first_row["status"] == "pending_action"

    second = triage_notifications(cfg, client=fake)
    assert second.exit_code != 0
    text = sink.read_text()
    assert "something is badly broken" in text
    assert "Proposed fix:" in text
    assert "ci-action-1" in fake.marked_read
    second_row = Ledger(cfg.state_dir / "ledger.sqlite3").get_notification("ci-action-1")
    assert second_row is not None and second_row["status"] == "processed"
