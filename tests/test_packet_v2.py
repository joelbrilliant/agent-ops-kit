"""Production-shaped packet-v2 acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent_ops.config import RepoPolicy
from agent_ops.github.client import FakeGitHub
from agent_ops.github.discovery import fetch_pr_threads
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
