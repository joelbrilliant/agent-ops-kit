"""Named packet minimum tests for Slice 3 issue-to-draft-PR production path."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from agent_ops.audit.report import build_audit_report
from agent_ops.config import RunnerIdentityPolicy
from agent_ops.github.client import FakeGitHub
from agent_ops.maintenance import issue_fix as issue_fix_module
from agent_ops.maintenance.issue_fix import _branch_name, inspect_issue_work, issue_sweep
from agent_ops.runners.runner import write_owner_only_json
from tests.conftest import sys_executable, write_executable
from tests.test_issue_slice3 import (
    BUILD_ID,
    REVIEW_ID,
    _git,
    _sync_pr_head_oid,
    init_base_repo,
    make_bare_remote,
    make_issue_config,
    sample_issue,
    wire_fake_for_issue,
)


def _install_head_sync(fake: FakeGitHub, bare: Path) -> None:
    handlers = list(fake.graphql_handlers)

    def gql_with_head(query_text: str, variables: Dict[str, Any]):
        if "createPullRequest" in query_text:
            _sync_pr_head_oid(fake, bare)
        for handler in handlers:
            result = handler(query_text, variables)
            if result is not None:
                if "createPullRequest" in query_text:
                    _sync_pr_head_oid(fake, bare)
                    state = getattr(fake, "_issue_draft_state", {})
                    pr = (result.get("data") or {}).get("createPullRequest", {}).get(
                        "pullRequest"
                    )
                    if isinstance(pr, dict) and state.get("head_oid"):
                        pr["headRefOid"] = state["head_oid"]
                if (
                    "pullRequest" in query_text
                    and "isDraft" in query_text
                    and "createPullRequest" not in query_text
                ):
                    state = getattr(fake, "_issue_draft_state", {})
                    pr = ((result.get("data") or {}).get("repository") or {}).get(
                        "pullRequest"
                    )
                    if isinstance(pr, dict) and state.get("head_oid"):
                        pr["headRefOid"] = state["head_oid"]
                return result
        return None

    fake.graphql_handlers = [gql_with_head]


def _base_fixture(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    issue = sample_issue()
    wire_fake_for_issue(fake, issue=issue, base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    return cfg, fake, issue, bare, sha, head_repo


def _write_script(path: Path, body: str) -> Path:
    write_executable(path, textwrap.dedent(body))
    return path


def test_issue_job_uses_same_session_for_classify_and_build(tmp_path: Path):
    cfg, fake, _issue, _bare, _sha, _head = _base_fixture(tmp_path)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0, out.message
    build_reqs = list((cfg.state_dir / "requests").glob("*-build-request.json"))
    assert build_reqs
    payload = json.loads(build_reqs[0].read_text(encoding="utf-8"))
    assert payload["expected_session_id"] == "issue-classification-session"
    build_resps = list((cfg.state_dir / "requests").glob("*-build-response.json"))
    assert build_resps
    resp = json.loads(build_resps[0].read_text(encoding="utf-8"))
    assert resp["runner_identity"]["session_id"] == "issue-classification-session"
    assert resp["runner_identity"]["fresh_session"] is False


def test_issue_job_uses_distinct_fresh_reviewer_session(tmp_path: Path):
    cfg, fake, _issue, _bare, _sha, _head = _base_fixture(tmp_path)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0, out.message
    review_resps = list((cfg.state_dir / "requests").glob("*-issue-review-response.json"))
    assert review_resps
    resp = json.loads(review_resps[0].read_text(encoding="utf-8"))
    assert resp["runner_identity"]["session_id"] == "issue-review-session"
    assert resp["runner_identity"]["session_id"] != "issue-classification-session"
    assert resp["runner_identity"]["fresh_session"] is True


def test_issue_classifier_and_builder_require_build_identity(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    bad = _write_script(
        scripts / "bad_classify.py",
        """\
        #!/usr/bin/env python3
        import json, sys
        from pathlib import Path
        args = sys.argv[1:]
        resp = Path(args[args.index('--response')+1])
        resp.write_text(json.dumps({
            'schema': 'ClassifierResponseV1',
            'decision': {
                'schema': 'DecisionV1',
                'verdict': 'ROUTINE',
                'reason': 'ok',
                'requested_allowed_paths': [],
                'proposed_verification_ids': ['unit'],
            },
            'continuation_token': 'token-build-id-1',
            'runner_identity': {
                'profile': 'build',
                'provider': 'xai-oauth',
                'model': 'wrong-model',
                'reasoning_effort': 'low',
                'service_tier': 'standard',
                'session_id': 'issue-classification-session',
                'fresh_session': True,
            },
        }))
        """,
    )
    cfg = make_issue_config(
        tmp_path,
        # override via issue automation rebuild
    )
    assert cfg.issue_automation is not None
    cfg.issue_automation.classifier_command[:] = [
        sys_executable(),
        str(bad),
        "--request",
        "{request_path}",
        "--response",
        "{response_path}",
    ]
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_pulls
    assert not fake.created_issue_comments


def test_issue_reviewer_requires_review_identity(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    bad_review = _write_script(
        scripts / "bad_review.py",
        """\
        #!/usr/bin/env python3
        import json, sys
        from pathlib import Path
        args = sys.argv[1:]
        req = json.loads(Path(args[args.index('--request')+1]).read_text())
        resp = Path(args[args.index('--response')+1])
        candidate = req['candidate_sha']
        resp.write_text(json.dumps({
            'schema': 'IssueReviewerResponseV1',
            'runner_identity': {
                'profile': 'imposter',
                'provider': 'openai-codex',
                'model': 'gpt-5.6-sol',
                'reasoning_effort': 'xhigh',
                'service_tier': 'fast',
                'session_id': 'issue-review-session',
                'fresh_session': True,
            },
            'reviewed_sha': candidate,
            'resulting_sha': candidate,
            'verdict': 'PASS',
            'findings': [],
            'fixes': [],
            'reply_draft': f'Fixed at {candidate}. checks: unit. {{draft_pr_url}}',
            'pr_title': 'fix',
            'pr_body': 'Closes #7',
            'voice_gate': {
                'schema': 'VoiceGateV1',
                'shared_operator_contract_read': True,
                'operator_profile_read': True,
                'skill': 'joel-voice-writing',
                'reference': 'references/voice.md',
                'register': 'public-community-short-reply',
                'passed': True,
            },
        }))
        """,
    )
    cfg = make_issue_config(tmp_path)
    assert cfg.issue_automation is not None
    cfg.issue_automation.reviewer_command[:] = [
        sys_executable(),
        str(bad_review),
        "--request",
        "{request_path}",
        "--response",
        "{response_path}",
        "--worktree",
        "{worktree_path}",
    ]
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_pulls
    assert not fake.created_issue_comments


def test_issue_runner_identity_mismatch_blocks_all_mutation(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    stolen = _write_script(
        scripts / "stolen_session_review.py",
        """\
        #!/usr/bin/env python3
        import json, sys
        from pathlib import Path
        args = sys.argv[1:]
        req = json.loads(Path(args[args.index('--request')+1]).read_text())
        resp = Path(args[args.index('--response')+1])
        candidate = req['candidate_sha']
        resp.write_text(json.dumps({
            'schema': 'IssueReviewerResponseV1',
            'runner_identity': {
                'profile': 'review',
                'provider': 'openai-codex',
                'model': 'gpt-5.6-sol',
                'reasoning_effort': 'xhigh',
                'service_tier': 'fast',
                'session_id': 'issue-classification-session',
                'fresh_session': True,
            },
            'reviewed_sha': candidate,
            'resulting_sha': candidate,
            'verdict': 'PASS',
            'findings': [],
            'fixes': [],
            'reply_draft': f'Fixed at {candidate}. checks: unit. {{draft_pr_url}}',
            'pr_title': 'fix',
            'pr_body': 'Closes #7',
            'voice_gate': {
                'schema': 'VoiceGateV1',
                'shared_operator_contract_read': True,
                'operator_profile_read': True,
                'skill': 'joel-voice-writing',
                'reference': 'references/voice.md',
                'register': 'public-community-short-reply',
                'passed': True,
            },
        }))
        """,
    )
    cfg = make_issue_config(tmp_path)
    assert cfg.issue_automation is not None
    cfg.issue_automation.reviewer_command[:] = [
        sys_executable(),
        str(stolen),
        "--request",
        "{request_path}",
        "--response",
        "{response_path}",
        "--worktree",
        "{worktree_path}",
    ]
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_pulls


def test_issue_job_rejects_disallowed_changed_path(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    builder = _write_script(
        scripts / "wide_build.py",
        """\
        #!/usr/bin/env python3
        import hashlib, json, subprocess, sys
        from pathlib import Path
        args = sys.argv[1:]
        def get(flag):
            return Path(args[args.index(flag)+1])
        req = json.loads(get('--request').read_text())
        wt = get('--worktree')
        (wt / 'demo.txt').write_text('broken=0\\n')
        (wt / 'SECRET_OUTSIDE.txt').write_text('nope\\n')
        subprocess.run(['git', 'config', 'user.email', 't@e.i'], cwd=wt, check=True)
        subprocess.run(['git', 'config', 'user.name', 't'], cwd=wt, check=True)
        subprocess.run(['git', 'add', '-A'], cwd=wt, check=True)
        subprocess.run(['git', 'commit', '-m', 'too wide'], cwd=wt, check=True)
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=wt, check=True, capture_output=True, text=True).stdout.strip()
        token = req['continuation_token']
        get('--response').write_text(json.dumps({
            'schema': 'BuilderResponseV1',
            'runner_identity': {
                'profile': 'build',
                'provider': 'xai-oauth',
                'model': 'grok-composer-2.5-fast',
                'reasoning_effort': 'low',
                'service_tier': 'standard',
                'session_id': req['expected_session_id'],
                'fresh_session': False,
            },
            'base_sha': req['task']['base_sha'],
            'resulting_sha': head,
            'changed_paths': ['demo.txt', 'SECRET_OUTSIDE.txt'],
            'continuation_token_digest': hashlib.sha256(token.encode()).hexdigest(),
        }))
        """,
    )
    cfg = make_issue_config(tmp_path)
    assert cfg.issue_automation is not None
    cfg.issue_automation.builder_command[:] = [
        sys_executable(),
        str(builder),
        "--request",
        "{request_path}",
        "--response",
        "{response_path}",
        "--worktree",
        "{worktree_path}",
    ]
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_pulls
    # No agent branch pushed.
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent-ops/issue" not in refs


def test_issue_job_rejects_dirty_or_rewritten_history(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    dirty = _write_script(
        scripts / "dirty_build.py",
        """\
        #!/usr/bin/env python3
        import hashlib, json, subprocess, sys
        from pathlib import Path
        args = sys.argv[1:]
        def get(flag):
            return Path(args[args.index(flag)+1])
        req = json.loads(get('--request').read_text())
        wt = get('--worktree')
        (wt / 'demo.txt').write_text('broken=0\\n')
        subprocess.run(['git', 'config', 'user.email', 't@e.i'], cwd=wt, check=True)
        subprocess.run(['git', 'config', 'user.name', 't'], cwd=wt, check=True)
        subprocess.run(['git', 'add', 'demo.txt'], cwd=wt, check=True)
        subprocess.run(['git', 'commit', '-m', 'fix'], cwd=wt, check=True)
        (wt / 'demo.txt').write_text('broken=0\\ndirty\\n')
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=wt, check=True, capture_output=True, text=True).stdout.strip()
        token = req['continuation_token']
        get('--response').write_text(json.dumps({
            'schema': 'BuilderResponseV1',
            'runner_identity': {
                'profile': 'build',
                'provider': 'xai-oauth',
                'model': 'grok-composer-2.5-fast',
                'reasoning_effort': 'low',
                'service_tier': 'standard',
                'session_id': req['expected_session_id'],
                'fresh_session': False,
            },
            'base_sha': req['task']['base_sha'],
            'resulting_sha': head,
            'changed_paths': ['demo.txt'],
            'continuation_token_digest': hashlib.sha256(token.encode()).hexdigest(),
        }))
        """,
    )
    cfg = make_issue_config(tmp_path)
    assert cfg.issue_automation is not None
    cfg.issue_automation.builder_command[:] = [
        sys_executable(),
        str(dirty),
        "--request",
        "{request_path}",
        "--response",
        "{response_path}",
        "--worktree",
        "{worktree_path}",
    ]
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_pulls


def test_issue_job_holds_on_failing_verification(tmp_path: Path):
    cfg, fake, _issue, bare, _sha, _head = _base_fixture(tmp_path)
    cfg.repository_policies["operator/demo"].verification_commands["unit"] = [
        sys_executable(),
        "-c",
        "raise SystemExit(2)",
    ]
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_pulls
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent-ops/issue" not in refs


def test_issue_final_failure_receipt_uses_final_reviewer_check_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg, fake, _issue, bare, _sha, _head = _base_fixture(tmp_path)
    counter = tmp_path / "verification-count.txt"
    cfg.repository_policies["operator/demo"].verification_commands["unit"] = [
        sys_executable(),
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({str(counter)!r}); "
            "n=int(p.read_text()) + 1 if p.exists() else 1; "
            "p.write_text(str(n)); raise SystemExit(n)"
        ),
    ]
    captured = []
    original_receipt = issue_fix_module._receipt

    def capture_receipt(*args, **kwargs):
        captured.append(
            [
                (check.check_id, check.status, check.summary)
                for check in kwargs["checks"]
            ]
        )
        return original_receipt(*args, **kwargs)

    monkeypatch.setattr(issue_fix_module, "_receipt", capture_receipt)

    outcome = issue_sweep(cfg, client=fake)

    assert outcome.exit_code != 0
    assert "final_verification_failed" in outcome.message
    assert captured[-1] == [("unit", "HOLD", "exit=2")]
    assert not fake.created_pulls
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent-ops/issue" not in refs


def test_issue_recovery_requires_a_committed_reviewer_change(tmp_path: Path):
    cfg, fake, _issue, bare, _sha, _head = _base_fixture(tmp_path)
    counter = tmp_path / "verification-count.txt"
    cfg.repository_policies["operator/demo"].verification_commands["unit"] = [
        sys_executable(),
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({str(counter)!r}); "
            "n=int(p.read_text()) + 1 if p.exists() else 1; "
            "p.write_text(str(n)); raise SystemExit(1 if n == 1 else 0)"
        ),
    ]

    outcome = issue_sweep(cfg, client=fake)

    assert outcome.exit_code != 0
    assert "reviewer_recovery_missing_committed_change" in outcome.message
    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "held"
    assert receipt["named_checks"] == ["unit"]
    assert counter.read_text(encoding="utf-8") == "2"
    assert not fake.created_pulls
    assert not fake.created_issue_comments
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent-ops/issue" not in refs


def test_issue_recovery_reviewer_cannot_rewrite_the_exact_candidate(tmp_path: Path):
    cfg, fake, _issue, bare, _sha, _head = _base_fixture(tmp_path)
    assert cfg.issue_automation is not None
    cfg.repository_policies["operator/demo"].verification_commands["unit"] = [
        sys_executable(),
        "-c",
        "from pathlib import Path; raise SystemExit(0 if '# reviewer' in Path('demo.txt').read_text() else 1)",
    ]
    reviewer = Path(cfg.issue_automation.reviewer_command[1])
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
    'schema': 'IssueReviewerResponseV1',
    'runner_identity': {'profile': 'review', 'provider': 'openai-codex', 'model': 'gpt-5.6-sol', 'reasoning_effort': 'xhigh', 'service_tier': 'fast', 'session_id': 'rewriting-review-session', 'fresh_session': True},
    'reviewed_sha': request['candidate_sha'], 'resulting_sha': head, 'verdict': 'PASS', 'findings': [], 'fixes': ['rewritten repair'],
    'reply_draft': 'Fixed at ' + head + '. checks: unit. {draft_pr_url}',
    'pr_title': 'fix after review', 'pr_body': 'Closes #7',
    'voice_gate': {'schema': 'VoiceGateV1', 'shared_operator_contract_read': True, 'operator_profile_read': True, 'skill': 'joel-voice-writing', 'reference': 'references/voice.md', 'register': 'public-community-short-reply', 'passed': True},
}))
""",
        encoding="utf-8",
    )

    outcome = issue_sweep(cfg, client=fake)

    assert outcome.exit_code != 0
    assert "reviewer_recovery_rewrote_candidate" in outcome.message
    assert not fake.created_pulls
    assert not fake.created_issue_comments
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "agent-ops/issue" not in refs


def test_issue_failed_check_reaches_fresh_reviewer_and_recovers_once(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    reviewer = _write_script(
        scripts / "fixing_review.py",
        """\
        #!/usr/bin/env python3
        import json, subprocess, sys
        from pathlib import Path
        args = sys.argv[1:]
        def get(flag):
            return Path(args[args.index(flag)+1])
        req = json.loads(get('--request').read_text())
        wt = get('--worktree')
        (wt / 'demo.txt').write_text('broken=0\\n# reviewer\\n')
        subprocess.run(['git', 'config', 'user.email', 't@e.i'], cwd=wt, check=True)
        subprocess.run(['git', 'config', 'user.name', 't'], cwd=wt, check=True)
        subprocess.run(['git', 'add', 'demo.txt'], cwd=wt, check=True)
        subprocess.run(['git', 'commit', '-m', 'reviewer fix'], cwd=wt, check=True)
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=wt, check=True, capture_output=True, text=True).stdout.strip()
        check_ids = [row['check_id'] for row in req['verification']]
        get('--response').write_text(json.dumps({
            'schema': 'IssueReviewerResponseV1',
            'runner_identity': {
                'profile': 'review',
                'provider': 'openai-codex',
                'model': 'gpt-5.6-sol',
                'reasoning_effort': 'xhigh',
                'service_tier': 'fast',
                'session_id': 'issue-review-session',
                'fresh_session': True,
            },
            'reviewed_sha': req['candidate_sha'],
            'resulting_sha': head,
            'verdict': 'PASS',
            'findings': [],
            'fixes': ['reviewer tweak'],
            'reply_draft': f'Fixed at {head}. checks: ' + ', '.join(check_ids) + '. {draft_pr_url}',
            'pr_title': 'fix after review',
            'pr_body': 'Closes #7',
            'voice_gate': {
                'schema': 'VoiceGateV1',
                'shared_operator_contract_read': True,
                'operator_profile_read': True,
                'skill': 'joel-voice-writing',
                'reference': 'references/voice.md',
                'register': 'public-community-short-reply',
                'passed': True,
            },
        }))
        """,
    )
    cfg = make_issue_config(tmp_path)
    assert cfg.issue_automation is not None
    verification_counter = tmp_path / "verification-count.txt"
    cfg.repository_policies["operator/demo"].verification_commands["unit"] = [
        sys_executable(),
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({str(verification_counter)!r}); "
            "p.write_text((p.read_text() if p.exists() else '') + 'x'); "
            "raise SystemExit(0 if '# reviewer' in Path('demo.txt').read_text() else 1)"
        ),
    ]
    cfg.issue_automation.reviewer_command[:] = [
        sys_executable(),
        str(reviewer),
        "--request",
        "{request_path}",
        "--response",
        "{response_path}",
        "--worktree",
        "{worktree_path}",
    ]
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0, out.message
    receipt = json.loads(out.receipt_path.read_text(encoding="utf-8"))
    # Resulting SHA must be the reviewer-fixed tip present on remote.
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert receipt["resulting_sha"] in refs
    assert receipt["resulting_sha"] != sha
    assert receipt["named_checks"] == ["unit", "recovery.unit"]
    assert verification_counter.read_text(encoding="utf-8") == "xx"
    responses = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (cfg.state_dir / "requests").glob("*-response.json")
    ]
    fresh = [
        response["runner_identity"]
        for response in responses
        if response["runner_identity"]["fresh_session"]
    ]
    assert len(fresh) == 2
    assert len({identity["session_id"] for identity in fresh}) == 2
    report = build_audit_report(cfg)
    assert report.verdict == "PASS"
    assert report.items[0]["named_checks"] == ["recovery.unit", "unit"]


def test_issue_job_holds_when_issue_changes_before_push(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    issue = sample_issue()
    wire_fake_for_issue(fake, issue=issue, base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    fetches = {"n": 0}
    handlers = list(fake.graphql_handlers)

    def gql(query_text: str, variables: Dict[str, Any]):
        if "repository(owner" in query_text and "issue(number" in query_text:
            fetches["n"] += 1
            if fetches["n"] >= 3:
                issue["updatedAt"] = "2026-08-01T12:00:00Z"
                issue["comments"]["nodes"].append(
                    {
                        "id": "IC_LATE",
                        "author": {"login": "reporter"},
                        "body": "changed mind",
                        "createdAt": "2026-08-01T12:00:00Z",
                        "updatedAt": "2026-08-01T12:00:00Z",
                    }
                )
        for handler in handlers:
            result = handler(query_text, variables)
            if result is not None:
                return result
        return None

    fake.graphql_handlers = [gql]
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0


def test_issue_job_holds_when_base_sha_moves_before_push(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    _install_head_sync(fake, bare)
    ref_fetches = {"count": 0}

    def moving_base(query_text: str, _variables: Dict[str, Any]):
        if "ref(qualifiedName" not in query_text:
            return None
        ref_fetches["count"] += 1
        oid = sha if ref_fetches["count"] < 3 else "0" * 40
        return {"data": {"repository": {"ref": {"target": {"oid": oid}}}}}

    fake.graphql_handlers.insert(0, moving_base)
    out = issue_sweep(cfg, client=fake)

    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert ref_fetches["count"] == 3
    assert not fake.created_pulls


def test_issue_job_holds_when_target_branch_exists(tmp_path: Path):
    cfg, fake, issue, bare, sha, head_repo = _base_fixture(tmp_path)
    inspected = inspect_issue_work(cfg, client=fake)
    assert inspected.signals
    assert cfg.issue_automation is not None
    branch = _branch_name(cfg.issue_automation, inspected.signals[0])
    # Seed conflicting remote branch.
    _git(head_repo, "checkout", "-b", branch)
    _git(head_repo, "commit", "--allow-empty", "-m", "spoiler")
    subprocess.run(
        ["git", "push", str(bare), f"HEAD:refs/heads/{branch}"],
        cwd=str(head_repo),
        check=True,
        capture_output=True,
        text=True,
    )
    _git(head_repo, "checkout", "main")
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_pulls


def test_issue_job_creates_draft_pr_with_exact_head_and_base(tmp_path: Path):
    cfg, fake, _issue, bare, sha, _head = _base_fixture(tmp_path)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0, out.message
    assert fake.created_pulls
    created = fake.created_pulls[0]
    variables = created["variables"]
    pr = created["response"]
    assert variables["baseRefName"] == "main"
    assert str(variables["headRefName"]).startswith("agent-ops/issue-7-")
    assert pr["isDraft"] is True
    assert pr["baseRefName"] == "main"
    assert pr["headRefName"] == variables["headRefName"]
    receipt = json.loads(out.receipt_path.read_text(encoding="utf-8"))
    assert pr["headRefOid"] == receipt["resulting_sha"]
    assert "Closes #7" in str(variables.get("body") or "")
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert receipt["resulting_sha"] in refs
    assert receipt["resulting_sha"] != sha


def test_issue_job_never_merges_or_closes_issue(tmp_path: Path):
    cfg, fake, _issue, _bare, _sha, _head = _base_fixture(tmp_path)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0, out.message
    # Explicit forbidden mutation types never recorded.
    forbidden = (
        "mergePullRequest",
        "closeIssue",
        "addLabelsToLabelable",
        "removeLabelsFromLabelable",
        "updateRefs",
        "deleteRef",
        "markPullRequestReadyForReview",
    )
    raw = json.dumps({"mutations": fake.mutations, "pulls": fake.created_pulls})
    for token in forbidden:
        assert token not in raw
    # Only draft create + issue comment.
    assert fake.created_pulls and fake.created_issue_comments
    assert all(pr["response"]["isDraft"] for pr in fake.created_pulls)
    assert all(pr["response"].get("merged") is not True for pr in fake.created_pulls)


def test_issue_job_posts_and_reads_back_exact_reply(tmp_path: Path):
    cfg, fake, _issue, _bare, _sha, _head = _base_fixture(tmp_path)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0, out.message
    receipt = json.loads(out.receipt_path.read_text(encoding="utf-8"))
    assert fake.created_issue_comments
    body = fake.created_issue_comments[0]["body"]
    assert receipt["draft_pr_url"] in body
    assert receipt["resulting_sha"] in body
    assert "unit" in body
    assert receipt["issue_reply_node_id"] == fake.created_issue_comments[0]["id"]


def test_issue_job_rejects_non_draft_or_mismatched_pr_readback(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    git_root = tmp_path / "git"
    head_repo, sha = init_base_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, issue=sample_issue(), base_sha=sha, clone_url=str(bare))
    handlers = list(fake.graphql_handlers)

    def gql(query_text: str, variables: Dict[str, Any]):
        if "createPullRequest" in query_text:
            _sync_pr_head_oid(fake, bare)
        for handler in handlers:
            result = handler(query_text, variables)
            if result is None:
                continue
            if "createPullRequest" in query_text:
                _sync_pr_head_oid(fake, bare)
                pr = (result.get("data") or {}).get("createPullRequest", {}).get(
                    "pullRequest"
                )
                if isinstance(pr, dict):
                    state = getattr(fake, "_issue_draft_state", {})
                    if state.get("head_oid"):
                        pr["headRefOid"] = state["head_oid"]
                    pr["isDraft"] = False
                    state["pr"] = pr
            if (
                "pullRequest" in query_text
                and "isDraft" in query_text
                and "createPullRequest" not in query_text
            ):
                pr = ((result.get("data") or {}).get("repository") or {}).get(
                    "pullRequest"
                )
                if isinstance(pr, dict):
                    pr["isDraft"] = False
            return result
        return None

    fake.graphql_handlers = [gql]
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code != 0
    assert out.jobs_completed == 0
    assert not fake.created_issue_comments


def test_issue_request_files_are_owner_only(tmp_path: Path):
    path = tmp_path / "manual-request.json"
    write_owner_only_json(path, {"schema": "Probe", "ok": True})
    assert (path.stat().st_mode & 0o777) == 0o600

    cfg, fake, _issue, _bare, _sha, _head = _base_fixture(tmp_path)
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0, out.message
    reqs = list((cfg.state_dir / "requests").glob("*-request.json"))
    assert reqs
    for req in reqs:
        mode = req.stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"{req} mode={oct(mode)}"


def test_issue_second_sweep_launches_no_runner_and_writes_nothing(tmp_path: Path):
    cfg, fake, _issue, _bare, _sha, _head = _base_fixture(tmp_path)
    first = issue_sweep(cfg, client=fake)
    assert first.exit_code == 0 and first.jobs_completed == 1
    req_before = {
        p.name: p.stat().st_mtime_ns for p in (cfg.state_dir / "requests").glob("*.json")
    }
    receipt_before = {
        p.name: p.stat().st_mtime_ns for p in (cfg.state_dir / "receipts").glob("*.json")
    }
    pulls_before = len(fake.created_pulls)
    comments_before = len(fake.created_issue_comments)
    second = issue_sweep(cfg, client=fake)
    assert second.exit_code == 0
    assert second.jobs_completed == 0
    assert second.message == "no_actionable_signal"
    req_after = {
        p.name: p.stat().st_mtime_ns for p in (cfg.state_dir / "requests").glob("*.json")
    }
    receipt_after = {
        p.name: p.stat().st_mtime_ns for p in (cfg.state_dir / "receipts").glob("*.json")
    }
    assert req_after == req_before
    assert receipt_after == receipt_before
    assert len(fake.created_pulls) == pulls_before
    assert len(fake.created_issue_comments) == comments_before
