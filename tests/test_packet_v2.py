"""Production-shaped packet-v2 acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent_ops.config import RepoPolicy
from agent_ops.audit.report import build_audit_report
from agent_ops.github.client import FakeGitHub
from agent_ops.github.discovery import fetch_pr_threads
from agent_ops.maintenance import review_fix as review_fix_module
from agent_ops.maintenance.ledger import Ledger
from agent_ops.maintenance.orchestrator import set_paused, sweep
from agent_ops.maintenance.worktree import base_is_ancestor, push_head_no_force
from agent_ops.runners.runner import (
    RunnerContractError,
    build_runner_environment,
    prove_github_capability_isolation,
    run_classifier,
)
from tests.conftest import make_config, sample_pr, wire_fake_for_pr, write_executable
from tests.test_sweep_e2e import init_head_repo, make_bare_remote


def _item(repository: str, number: int) -> Dict[str, Any]:
    return {
        "id": number,
        "number": number,
        "html_url": f"https://github.com/{repository}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/{repository}",
        "pull_request": {"url": f"https://api.github.com/repos/{repository}/pulls/{number}"},
    }


def _remote_ref(remote: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{ref}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _wire_real_remote(tmp_path: Path, fake: FakeGitHub, *, number: int = 1):
    head_repo, sha, ref = init_head_repo(tmp_path / f"git-{number}")
    remote = make_bare_remote(tmp_path / f"git-{number}", head_repo)
    pr = sample_pr(number=number, head_sha=sha, head_ref=ref)
    pr["reviewThreads"]["nodes"][0]["id"] = f"thread-{number}"
    pr["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["id"] = f"comment-{number}"
    pr["headRepository"]["url"] = str(remote)
    wire_fake_for_pr(fake, pr=pr)
    return pr, remote, sha, ref


def test_exactly_two_attested_sessions_with_same_session_build_continuation(tmp_path: Path):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    _, _, _, _ = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 0, outcome.message
    responses = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (cfg.state_dir / "requests").glob("*-response.json")
    }
    classifier = next(value for key, value in responses.items() if "classify" in key)
    builder = next(value for key, value in responses.items() if "build" in key)
    reviewer = next(value for key, value in responses.items() if "review" in key)
    session_ids = {
        classifier["runner_identity"]["session_id"],
        builder["runner_identity"]["session_id"],
        reviewer["runner_identity"]["session_id"],
    }
    assert len(session_ids) == 2
    assert classifier["runner_identity"]["fresh_session"] is True
    assert builder["runner_identity"]["fresh_session"] is False
    assert builder["runner_identity"]["session_id"] == classifier["runner_identity"]["session_id"]
    assert reviewer["runner_identity"]["fresh_session"] is True
    assert reviewer["runner_identity"]["session_id"] != classifier["runner_identity"]["session_id"]

    requests = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (cfg.state_dir / "requests").glob("*-request.json")
    }
    build_request = next(value for key, value in requests.items() if "build" in key)
    review_request = next(value for key, value in requests.items() if "review" in key)
    assert build_request["continuation_token"] == classifier["continuation_token"]
    assert build_request["expected_session_id"] == classifier["runner_identity"]["session_id"]
    assert review_request["candidate_sha"] == reviewer["reviewed_sha"] == reviewer["resulting_sha"]


def test_runner_identity_mismatch_fails_closed_before_git_or_github_mutation(tmp_path: Path):
    cfg = make_config(tmp_path)
    classifier = Path(cfg.classifier_command[1])
    classifier.write_text(
        classifier.read_text(encoding="utf-8").replace("'model': 'gpt-5.6-sol'", "'model': 'wrong-model'"),
        encoding="utf-8",
    )
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "runner_identity_route_mismatch" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha
    assert Ledger(cfg.state_dir / "ledger.sqlite3").circuit_open()


def test_runner_environment_structurally_removes_github_credentials_and_config(tmp_path: Path):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    source = {
        "PATH": os.environ.get("PATH", ""),
        "GH_TOKEN": "not-forwarded",
        "GITHUB_TOKEN": "not-forwarded",
        "HOME": "/tmp/operator-home",
    }
    environment = build_runner_environment(
        state_dir=cfg.state_dir,
        allowlist=["PATH"],
        source_environment=source,
    )

    assert "GH_TOKEN" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert environment["HOME"] != source["HOME"]
    assert environment["GH_CONFIG_DIR"].startswith(str(cfg.state_dir))
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert prove_github_capability_isolation(
        client=fake,
        operator_logins=cfg.operator_logins,
        gh_command=cfg.gh_command,
        runner_environment=environment,
    ) == "operator"


def test_classifier_rejects_non_exact_response_schema(tmp_path: Path):
    cfg = make_config(tmp_path)
    script = tmp_path / "bad-schema.py"
    write_executable(
        script,
        """#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
response = Path(args[args.index('--response') + 1])
response.write_text(json.dumps({
    'schema': 'ClassifierResponseV1',
    'decision': {'schema': 'DecisionV1', 'verdict': 'ROUTINE', 'reason': 'x', 'requested_allowed_paths': ['demo.txt'], 'proposed_verification_ids': ['unit']},
    'continuation_token': 'continuation-token',
    'runner_identity': {'profile': 'oscar', 'provider': 'openai-codex', 'model': 'gpt-5.6-sol', 'reasoning_effort': 'xhigh', 'service_tier': 'fast', 'session_id': 'one', 'fresh_session': True},
    'unexpected': True,
}))
""",
    )
    environment = build_runner_environment(state_dir=cfg.state_dir, allowlist=["PATH"])

    with pytest.raises(RunnerContractError, match="invalid_keys"):
        run_classifier(
            [str(script), "--request", "{request_path}", "--response", "{response_path}"],
            request_payload={"schema": "ClassifierRequestV1"},
            state_dir=cfg.state_dir,
            run_id="schema",
            required_identity=cfg.required_runner_identity,
            runner_environment=environment,
            timeout=30,
        )


def test_builder_resulting_sha_mismatch_fails_closed_before_push(tmp_path: Path):
    cfg = make_config(tmp_path)
    builder = Path(cfg.builder_command[1])
    builder.write_text(
        builder.read_text(encoding="utf-8").replace(
            "'resulting_sha': resulting,",
            "'resulting_sha': 'f' * 40,",
        ),
        encoding="utf-8",
    )
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "builder_resulting_sha_mismatch" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_reviewer_rejects_extra_response_keys_before_push(tmp_path: Path):
    cfg = make_config(tmp_path)
    reviewer = Path(cfg.reviewer_command[1])
    reviewer.write_text(
        reviewer.read_text(encoding="utf-8").replace(
            "'schema': 'ReviewerResponseV1',",
            "'schema': 'ReviewerResponseV1', 'unexpected': True,",
        ),
        encoding="utf-8",
    )
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "ReviewerResponseV1_invalid_keys" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_review_thread_and_comment_connections_are_fully_paginated(tmp_path: Path):
    fake = FakeGitHub()
    base = sample_pr()
    threads = [
        {"id": "thread-a", "isResolved": False, "isOutdated": False, "path": "demo.txt", "line": 1},
        {"id": "thread-b", "isResolved": False, "isOutdated": False, "path": "pkg/ok.py", "line": 1},
    ]
    calls: List[str] = []

    def gql(query: str, variables: Dict[str, Any]):
        if "PrThreadsPage" in query:
            cursor = variables.get("threadCursor")
            calls.append(f"threads:{cursor}")
            page = 0 if cursor is None else 1
            pr = {key: value for key, value in base.items() if key != "reviewThreads"}
            pr["reviewThreads"] = {
                "nodes": [threads[page]],
                "pageInfo": {"hasNextPage": page == 0, "endCursor": "thread-next" if page == 0 else None},
            }
            return {"data": {"repository": {"pullRequest": pr}}}
        if "ReviewThreadCommentsPage" in query:
            cursor = variables.get("commentCursor")
            tid = variables["id"]
            calls.append(f"comments:{tid}:{cursor}")
            page = 0 if cursor is None else 1
            comment = {
                "id": f"{tid}-comment-{page}",
                "author": {"login": "reviewer"},
                "body": f"page {page}",
                "createdAt": f"2026-08-01T00:00:0{page}Z",
                "viewerDidAuthor": False,
            }
            return {
                "data": {
                    "node": {
                        "comments": {
                            "nodes": [comment],
                            "pageInfo": {"hasNextPage": page == 0, "endCursor": "comment-next" if page == 0 else None},
                        }
                    }
                }
            }
        return None

    fake.graphql_handlers.append(gql)
    result = fetch_pr_threads(fake, "operator/demo", 1)

    fetched = result["reviewThreads"]["nodes"]
    assert [thread["id"] for thread in fetched] == ["thread-a", "thread-b"]
    assert all(len(thread["comments"]["nodes"]) == 2 for thread in fetched)
    assert calls == [
        "threads:None",
        "comments:thread-a:None",
        "comments:thread-a:comment-next",
        "threads:thread-next",
        "comments:thread-b:None",
        "comments:thread-b:comment-next",
    ]


def test_one_sweep_processes_every_eligible_job_serially(tmp_path: Path):
    cfg = make_config(
        tmp_path,
        repository_policies={
            "operator/demo": RepoPolicy("operator/demo", ["demo.txt"], {}),
            "operator/second": RepoPolicy("operator/second", ["demo.txt"], {}),
        },
    )
    fake = FakeGitHub()
    pr1, _, _, _ = _wire_real_remote(tmp_path / "one", fake, number=1)

    head2, sha2, ref2 = init_head_repo(tmp_path / "two" / "git-2")
    remote2 = make_bare_remote(tmp_path / "two" / "git-2", head2)
    pr2 = sample_pr(number=2, head_sha=sha2, head_ref=ref2, base_repo="operator/second", head_repo="operator/second")
    pr2["reviewThreads"]["nodes"][0]["id"] = "thread-2"
    pr2["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["id"] = "comment-2"
    pr2["headRepository"]["url"] = str(remote2)
    wire_fake_for_pr(fake, base_repo="operator/second", pr=pr2)
    items = [_item("operator/demo", 1), _item("operator/second", 2)]
    fake.search_pages["is:pr is:open author:operator"] = [items]
    fake.search_pages["is:pr is:open user:operator"] = [items]

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 0, outcome.message
    assert outcome.jobs_completed == 2
    assert outcome.jobs_held == 0
    assert len(outcome.receipt_paths) == 2
    assert {reply["thread"] for reply in fake.replies} == {"thread-1", "thread-2"}
    assert pr1["reviewThreads"]["nodes"][0]["comments"]["nodes"][-1]["id"].startswith("reply-")


def test_pause_and_open_circuit_both_inspect_without_mutating(tmp_path: Path):
    for mode in ("pause", "circuit"):
        cfg = make_config(tmp_path / mode)
        fake = FakeGitHub()
        _, remote, original_sha, ref = _wire_real_remote(tmp_path / mode, fake)
        if mode == "pause":
            set_paused(cfg, True)
        else:
            Ledger(cfg.state_dir / "ledger.sqlite3").open_circuit("operator_hold")

        outcome = sweep(cfg, client=fake)

        assert outcome.inspected_prs == 1
        assert outcome.signals_found == 1
        assert not fake.replies
        assert _remote_ref(remote, ref) == original_sha
        assert not list((cfg.state_dir / "requests").glob("*.json"))


def test_required_checks_are_revalidated_immediately_before_push(tmp_path: Path):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)
    calls = {"count": 0}

    def checks():
        calls["count"] += 1
        if calls["count"] == 1:
            return [{"bucket": "pass", "name": "required", "state": "SUCCESS"}]
        return [{"bucket": "fail", "name": "required", "state": "FAILURE"}]

    fake.required_check_rows["operator/demo#1"] = checks
    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert calls["count"] == 2
    assert "required_checks_stale_before_push" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_no_required_checks_is_valid_when_local_verification_passes(tmp_path: Path):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    _wire_real_remote(tmp_path, fake)
    calls = {"count": 0}

    def checks():
        calls["count"] += 1
        return []

    fake.required_check_rows["operator/demo#1"] = checks

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 0, outcome.message
    assert outcome.jobs_completed == 1
    assert calls["count"] == 2
    assert fake.replies


def test_unrepaired_final_check_opens_circuit_without_github_mutation(tmp_path: Path):
    cfg = make_config(
        tmp_path,
        default_verification_commands={
            "unit": [sys.executable, "-c", "raise SystemExit(1)"]
        },
    )
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)
    fake.required_check_rows["operator/demo#1"] = []

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "final_verification_failed" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha
    assert Ledger(cfg.state_dir / "ledger.sqlite3").circuit_open()


def test_final_failure_receipt_uses_final_reviewer_check_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    counter = tmp_path / "verification-count.txt"
    cfg = make_config(
        tmp_path,
        default_verification_commands={
            "unit": [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"p=Path({str(counter)!r}); "
                    "n=int(p.read_text()) + 1 if p.exists() else 1; "
                    "p.write_text(str(n)); raise SystemExit(n)"
                ),
            ]
        },
    )
    captured = []
    original_receipt = review_fix_module._receipt

    def capture_receipt(*args, **kwargs):
        captured.append(
            [
                (check.check_id, check.status, check.summary)
                for check in kwargs["checks"]
            ]
        )
        return original_receipt(*args, **kwargs)

    monkeypatch.setattr(review_fix_module, "_receipt", capture_receipt)
    fake = FakeGitHub()
    _pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "final_verification_failed" in outcome.message
    assert captured[-1] == [("unit", "HOLD", "exit=2")]
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_recovery_requires_a_committed_reviewer_change(tmp_path: Path):
    counter = tmp_path / "verification-count.txt"
    cfg = make_config(
        tmp_path,
        default_verification_commands={
            "unit": [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"p=Path({str(counter)!r}); "
                    "n=int(p.read_text()) + 1 if p.exists() else 1; "
                    "p.write_text(str(n)); raise SystemExit(1 if n == 1 else 0)"
                ),
            ]
        },
    )
    fake = FakeGitHub()
    _pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "reviewer_recovery_missing_committed_change" in outcome.message
    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "held"
    assert receipt["named_checks"] == ["unit"]
    assert counter.read_text(encoding="utf-8") == "2"
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_recovery_reviewer_cannot_rewrite_the_exact_candidate(tmp_path: Path):
    cfg = make_config(
        tmp_path,
        default_verification_commands={
            "unit": [
                sys.executable,
                "-c",
                "from pathlib import Path; raise SystemExit(0 if '# reviewer' in Path('demo.txt').read_text() else 1)",
            ]
        },
    )
    reviewer = Path(cfg.reviewer_command[1])
    reviewer.write_text(
        """#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
def get(flag): return Path(args[args.index(flag) + 1])
request = json.loads(get('--request').read_text())
worktree = get('--worktree')
subprocess.run(['git', 'reset', '--hard', request['base_sha']], cwd=worktree, check=True)
(worktree / 'demo.txt').write_text((worktree / 'demo.txt').read_text() + '# reviewer\\n')
subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=worktree, check=True)
subprocess.run(['git', 'config', 'user.name', 'Test Oscar'], cwd=worktree, check=True)
subprocess.run(['git', 'add', 'demo.txt'], cwd=worktree, check=True)
subprocess.run(['git', 'commit', '-m', 'fix: rewritten repair'], cwd=worktree, check=True)
head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
get('--response').write_text(json.dumps({
    'schema': 'ReviewerResponseV1',
    'runner_identity': {'profile': 'oscar', 'provider': 'openai-codex', 'model': 'gpt-5.6-sol', 'reasoning_effort': 'xhigh', 'service_tier': 'fast', 'session_id': 'rewriting-review-session', 'fresh_session': True},
    'reviewed_sha': request['candidate_sha'], 'resulting_sha': head, 'verdict': 'PASS', 'findings': [], 'fixes': ['rewritten repair'],
    'reply_draft': 'fixed at ' + head + '. checks: unit',
    'voice_gate': {'schema': 'VoiceGateV1', 'shared_operator_contract_read': True, 'operator_profile_read': True, 'skill': 'joel-voice-writing', 'reference': 'references/voice.md', 'register': 'public-community-short-reply', 'passed': True},
}))
""",
        encoding="utf-8",
    )
    fake = FakeGitHub()
    _pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "reviewer_recovery_rewrote_candidate" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_reserved_recovery_check_id_holds_before_reviewer_and_mutation(tmp_path: Path):
    cfg = make_config(
        tmp_path,
        default_verification_commands={
            "unit": [sys.executable, "-c", "raise SystemExit(1)"],
            "recovery.unit": [sys.executable, "-c", "pass"],
        },
    )
    fake = FakeGitHub()
    _pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "builder_verification_failed" in outcome.message
    assert not list((cfg.state_dir / "requests").glob("*review-request.json"))
    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["named_checks"] == ["unit", "recovery.unit"]
    report = build_audit_report(cfg)
    assert len(report.items) == 1
    assert report.items[0]["named_checks"] == ["recovery.unit", "unit"]
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def _run_pr_failed_check_recovery(tmp_path: Path):
    cfg = make_config(
        tmp_path,
        default_verification_commands={
            "unit": [
                sys.executable,
                "-c",
                "from pathlib import Path; raise SystemExit(0 if '# reviewer' in Path('demo.txt').read_text() else 1)",
            ]
        },
    )
    reviewer = Path(cfg.reviewer_command[1])
    reviewer.write_text(
        """#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
def get(flag): return Path(args[args.index(flag) + 1])
request = json.loads(get('--request').read_text())
worktree = get('--worktree')
(worktree / 'demo.txt').write_text((worktree / 'demo.txt').read_text() + '# reviewer\\n')
subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=worktree, check=True)
subprocess.run(['git', 'config', 'user.name', 'Test Oscar'], cwd=worktree, check=True)
subprocess.run(['git', 'add', 'demo.txt'], cwd=worktree, check=True)
subprocess.run(['git', 'commit', '-m', 'fix: reviewer repair'], cwd=worktree, check=True)
head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip()
get('--response').write_text(json.dumps({
    'schema': 'ReviewerResponseV1',
    'runner_identity': {'profile': 'oscar', 'provider': 'openai-codex', 'model': 'gpt-5.6-sol', 'reasoning_effort': 'xhigh', 'service_tier': 'fast', 'session_id': 'recovery-review-session', 'fresh_session': True},
    'reviewed_sha': request['candidate_sha'], 'resulting_sha': head, 'verdict': 'PASS', 'findings': [], 'fixes': ['repair unit'],
    'reply_draft': 'fixed at ' + head + '. checks: unit',
    'voice_gate': {'schema': 'VoiceGateV1', 'shared_operator_contract_read': True, 'operator_profile_read': True, 'skill': 'joel-voice-writing', 'reference': 'references/voice.md', 'register': 'public-community-short-reply', 'passed': True},
}))
""",
        encoding="utf-8",
    )
    fake = FakeGitHub()
    _pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    return cfg, fake, outcome, remote, original_sha, ref


def test_pr_failed_check_reaches_fresh_reviewer_and_recovers_once(tmp_path: Path):
    cfg, fake, outcome, remote, original_sha, ref = _run_pr_failed_check_recovery(tmp_path)

    assert outcome.exit_code == 0, outcome.message
    assert _remote_ref(remote, ref) != original_sha
    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["named_checks"] == ["unit", "recovery.unit"]
    assert len(
        {
            payload["runner_identity"]["session_id"]
            for path in (cfg.state_dir / "requests").glob("*-response.json")
            for payload in [json.loads(path.read_text(encoding="utf-8"))]
        }
    ) == 2
    assert len(fake.replies) == 1
    report = build_audit_report(cfg)
    assert report.verdict == "PASS"
    assert report.items[0]["named_checks"] == ["recovery.unit", "unit"]


def test_recovery_still_uses_exactly_two_fresh_sessions(tmp_path: Path):
    cfg, _fake, outcome, _remote, _original_sha, _ref = _run_pr_failed_check_recovery(tmp_path)

    assert outcome.exit_code == 0, outcome.message
    responses = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (cfg.state_dir / "requests").glob("*-response.json")
    ]
    fresh = [response["runner_identity"] for response in responses if response["runner_identity"]["fresh_session"]]
    assert len(fresh) == 2
    assert len({identity["session_id"] for identity in fresh}) == 2


@pytest.mark.parametrize("exit_code", [0, 1])
def test_verification_mutation_never_reaches_reviewer(tmp_path: Path, exit_code: int):
    cfg = make_config(
        tmp_path,
        default_verification_commands={
            "unit": [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path('untracked-check-mutation').write_text('x'); raise SystemExit({exit_code})",
            ]
        },
    )
    fake = FakeGitHub()
    _pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "runner_left_dirty_worktree" in outcome.message
    assert Ledger(cfg.state_dir / "ledger.sqlite3").circuit_open()
    assert not list((cfg.state_dir / "requests").glob("*review-request.json"))
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


@pytest.mark.parametrize("mode", ["start", "timeout", "signal", "empty"])
def test_non_normal_verification_failures_never_reach_reviewer(
    tmp_path: Path, mode: str
):
    commands = {
        "start": [str(tmp_path / "missing-verifier")],
        "timeout": [sys.executable, "-c", "import time; time.sleep(2)"],
        "signal": [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        "empty": [],
    }
    cfg = make_config(
        tmp_path,
        default_verification_commands={"unit": commands[mode]},
        runner_timeout_seconds=1,
    )
    fake = FakeGitHub()
    _pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert Ledger(cfg.state_dir / "ledger.sqlite3").circuit_open()
    assert not list((cfg.state_dir / "requests").glob("*review-request.json"))
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


@pytest.mark.parametrize(
    ("rows", "expected_reason"),
    [
        (
            [{"bucket": "fail", "name": "required", "state": "FAILURE"}],
            "required_checks_not_green_before_build",
        ),
        (
            [{"bucket": "pass", "name": "", "state": "SUCCESS"}],
            "required_check_contract_incomplete",
        ),
    ],
)
def test_failing_or_malformed_required_checks_fail_closed(
    tmp_path: Path, rows: List[Dict[str, Any]], expected_reason: str
):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)
    fake.required_check_rows["operator/demo#1"] = rows

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert expected_reason in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_pr_head_sha_is_revalidated_immediately_before_push(tmp_path: Path):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)
    calls = {"count": 0}

    def moving_pr(query: str, variables: Dict[str, Any]):
        if "PrThreadsPage" not in query:
            return None
        calls["count"] += 1
        if calls["count"] < 3:
            return None
        changed = json.loads(json.dumps(pr))
        changed["headRefOid"] = "f" * 40
        return {"data": {"repository": {"pullRequest": changed}}}

    fake.graphql_handlers.insert(0, moving_pr)
    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert calls["count"] == 3
    assert "pr_snapshot_stale_before_push" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_push_permission_is_revalidated_immediately_before_push(tmp_path: Path):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)
    calls = {"count": 0}

    def permissions():
        calls["count"] += 1
        return {
            "permissions": {"push": calls["count"] == 1, "admin": False, "maintain": False},
            "full_name": "operator/demo",
        }

    fake.rest_handlers["repos/operator/demo"] = permissions
    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert calls["count"] == 2
    assert "push_permission_stale_before_push" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_thread_state_is_revalidated_immediately_before_push(tmp_path: Path):
    cfg = make_config(tmp_path)
    fake = FakeGitHub()
    pr, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    def stale_thread(query: str, variables: Dict[str, Any]):
        if "PullRequestReviewThread" in query and variables.get("id") == "thread-1":
            thread = dict(pr["reviewThreads"]["nodes"][0])
            thread["isResolved"] = True
            return {"data": {"node": thread}}
        return None

    fake.graphql_handlers.insert(0, stale_thread)
    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "thread_stale_before_push:thread_resolved" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha


def test_fork_fix_pushes_only_to_attested_head_repository(tmp_path: Path):
    cfg = make_config(tmp_path)
    head_repo, sha, ref = init_head_repo(tmp_path / "fork")
    fork_remote = make_bare_remote(tmp_path / "fork", head_repo)
    fake = FakeGitHub()
    pr = sample_pr(head_sha=sha, head_ref=ref, head_repo="operator/fork", is_fork=True)
    pr["headRepository"]["url"] = str(fork_remote)
    wire_fake_for_pr(fake, pr=pr)
    fake.rest_handlers["repos/operator/fork"] = {
        "permissions": {"push": True, "admin": False, "maintain": False},
        "full_name": "operator/fork",
        "owner": {"login": "operator"},
    }

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 0, outcome.message
    resulting = _remote_ref(fork_remote, ref)
    assert resulting != sha
    assert outcome.receipt_path is not None
    assert json.loads(outcome.receipt_path.read_text(encoding="utf-8"))["resulting_sha"] == resulting


def test_non_force_push_refuses_to_overwrite_concurrent_remote_history(tmp_path: Path):
    source, _, ref = init_head_repo(tmp_path / "source")
    remote = make_bare_remote(tmp_path / "source", source)
    local = tmp_path / "local"
    concurrent = tmp_path / "concurrent"
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(remote), str(concurrent)], check=True, capture_output=True, text=True)
    for worktree in (local, concurrent):
        _git(worktree, "config", "user.email", "test@example.invalid")
        _git(worktree, "config", "user.name", "Test")

    (local / "local.txt").write_text("local\n", encoding="utf-8")
    _git(local, "add", "local.txt")
    _git(local, "commit", "-m", "local")
    candidate = _git(local, "rev-parse", "HEAD")

    (concurrent / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(concurrent, "add", "remote.txt")
    _git(concurrent, "commit", "-m", "remote")
    _git(concurrent, "push", "origin", f"HEAD:refs/heads/{ref}")
    remote_advanced = _remote_ref(remote, ref)

    result = push_head_no_force(
        git_cmd="git",
        worktree=local,
        remote_url=str(remote),
        head_ref=ref,
    )

    assert result.ok is False
    detail = result.stderr.lower()
    assert "non-fast-forward" in detail or "fetch first" in detail or "rejected" in detail
    assert _remote_ref(remote, ref) == remote_advanced


def test_history_integrity_gate_rejects_rewritten_candidate(tmp_path: Path):
    source, base_sha, _ = init_head_repo(tmp_path / "history")
    _git(source, "switch", "--orphan", "rewritten")
    (source / "rewritten.txt").write_text("rewritten\n", encoding="utf-8")
    _git(source, "add", "rewritten.txt")
    _git(source, "commit", "-m", "rewritten")
    candidate = _git(source, "rev-parse", "HEAD")

    assert candidate != base_sha
    assert base_is_ancestor("git", source, base_sha) is False


def test_voice_gate_attestation_failure_blocks_push_and_reply(tmp_path: Path):
    cfg = make_config(tmp_path)
    reviewer = Path(cfg.reviewer_command[1])
    reviewer.write_text(
        reviewer.read_text(encoding="utf-8").replace("'passed': True", "'passed': False"),
        encoding="utf-8",
    )
    fake = FakeGitHub()
    _, remote, original_sha, ref = _wire_real_remote(tmp_path, fake)

    outcome = sweep(cfg, client=fake)

    assert outcome.exit_code == 1
    assert "reviewer_voice_gate_failed" in outcome.message
    assert not fake.replies
    assert _remote_ref(remote, ref) == original_sha
