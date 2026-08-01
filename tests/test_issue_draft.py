"""Issue-to-draft-PR slice tests."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_ops.audit.redaction import assert_no_private_material
from agent_ops.config import IssueAutomationConfig, RunnerIdentityPolicy, load_config, ConfigError
from agent_ops.github.client import FakeGitHub
from agent_ops.github.issues import discover_issue_signals
from agent_ops.maintenance.issue_fix import inspect_issues, issue_sweep
from agent_ops.maintenance.ledger import Ledger
from tests.conftest import make_config, sys_executable, write_executable
from tests.test_sweep_e2e import init_head_repo, make_bare_remote


IDENTITY_BUILD = RunnerIdentityPolicy(
    profile="oscar",
    provider="openai-codex",
    model="gpt-5.6-sol",
    reasoning_effort="xhigh",
    service_tier="fast",
)
IDENTITY_REVIEW = RunnerIdentityPolicy(
    profile="oscar",
    provider="openai-codex",
    model="gpt-5.6-sol",
    reasoning_effort="xhigh",
    service_tier="priority",
)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _issue_scripts(tmp: Path) -> Tuple[Path, Path, Path]:
    scripts = tmp / "issue-scripts"
    scripts.mkdir(exist_ok=True)
    classifier = scripts / "issue_classify.py"
    builder = scripts / "issue_build.py"
    reviewer = scripts / "issue_review.py"

    write_executable(
        classifier,
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path
            args = sys.argv[1:]
            def get(flag):
                return Path(args[args.index(flag)+1])
            req = get('--request'); resp = get('--response')
            data = json.loads(req.read_text())
            body = ((data.get('untrusted_issue_body') or '') + ' ' + (data.get('untrusted_issue_title') or '')).lower()
            hold = any(x in body for x in ['credential', 'force push', 'product direction', 'exfiltrat'])
            path = 'demo.txt'
            out = {
                'schema': 'ClassifierResponseV1',
                'decision': {
                    'schema': 'DecisionV1',
                    'verdict': 'HOLD' if hold else 'ROUTINE',
                    'reason': 'hold_marker' if hold else 'routine_issue',
                    'requested_allowed_paths': [] if hold else [path],
                    'proposed_verification_ids': ['unit'],
                },
                'continuation_token': 'issue-continuation-1',
                'runner_identity': {
                    'profile': 'oscar', 'provider': 'openai-codex', 'model': 'gpt-5.6-sol',
                    'reasoning_effort': 'xhigh', 'service_tier': 'fast',
                    'session_id': 'issue-classify-session', 'fresh_session': True,
                },
            }
            resp.write_text(json.dumps(out))
            """
        ),
    )
    write_executable(
        builder,
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import hashlib, json, subprocess, sys
            from pathlib import Path
            args = sys.argv[1:]
            def get(flag):
                return Path(args[args.index(flag)+1])
            req = get('--request'); resp = get('--response'); wt = get('--worktree')
            data = json.loads(req.read_text())
            path = 'demo.txt'
            target = wt / path
            text = target.read_text() if target.exists() else 'broken=1\\n'
            new = text.replace('broken=1', 'broken=0')
            if new == text:
                new = text.rstrip() + '\\n# fixed\\n'
            target.write_text(new)
            subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=wt, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Test Oscar'], cwd=wt, check=True)
            subprocess.run(['git', 'add', '--', path], cwd=wt, check=True)
            subprocess.run(['git', 'commit', '-m', 'fix: bounded issue fix'], cwd=wt, check=True)
            resulting = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=wt, check=True, capture_output=True, text=True
            ).stdout.strip()
            token = data['continuation_token']
            resp.write_text(json.dumps({
                'schema': 'BuilderResponseV1',
                'runner_identity': {
                    'profile': 'oscar', 'provider': 'openai-codex', 'model': 'gpt-5.6-sol',
                    'reasoning_effort': 'xhigh', 'service_tier': 'fast',
                    'session_id': data['expected_session_id'], 'fresh_session': False,
                },
                'base_sha': data['task']['base_sha'],
                'resulting_sha': resulting,
                'changed_paths': [path],
                'continuation_token_digest': hashlib.sha256(token.encode()).hexdigest(),
            }))
            """
        ),
    )
    write_executable(
        reviewer,
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path
            args = sys.argv[1:]
            def get(flag):
                return Path(args[args.index(flag)+1])
            req = get('--request'); resp = get('--response')
            data = json.loads(req.read_text())
            candidate = data['candidate_sha']
            check_ids = [row['check_id'] for row in data['verification']]
            issue_number = int((data.get('task') or {}).get('issue_number') or 42)
            reply = f'fixed at {candidate}. checks: ' + ', '.join(check_ids)
            resp.write_text(json.dumps({
                'schema': 'IssueReviewerResponseV1',
                'runner_identity': {
                    'profile': 'oscar', 'provider': 'openai-codex', 'model': 'gpt-5.6-sol',
                    'reasoning_effort': 'xhigh', 'service_tier': 'priority',
                    'session_id': 'issue-review-session', 'fresh_session': True,
                },
                'reviewed_sha': candidate,
                'resulting_sha': candidate,
                'verdict': 'PASS',
                'findings': [],
                'fixes': [],
                'reply_draft': reply,
                'pr_title': f'agent-ops: issue #{issue_number}',
                'pr_body': f'Bounded fix for issue.\\n\\nCloses #{issue_number}\\n',
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
            """
        ),
    )
    return classifier, builder, reviewer


def make_issue_config(tmp: Path, **overrides: Any):
    cfg = make_config(tmp)
    classifier, builder, reviewer = _issue_scripts(tmp)
    issue = IssueAutomationConfig(
        enabled=True,
        enabled_repositories={"operator/demo": "main"},
        require_labels=["agent-ops:ready"],
        ignore_labels=["do-not-touch"],
        branch_prefix="agent-ops/issue",
        draft_pr_title_template="agent-ops: issue #{issue_number}",
        issue_reply_template="Draft PR ready: {pr_url}\\nSHA: {resulting_sha}\\nChecks: {named_checks}\\n{reply_draft}",
        max_paths_per_issue=8,
        max_changed_files=20,
        max_diff_lines=400,
        classifier_command=[
            sys_executable(),
            str(classifier),
            "--request",
            "{request_path}",
            "--response",
            "{response_path}",
        ],
        builder_command=[
            sys_executable(),
            str(builder),
            "--request",
            "{request_path}",
            "--response",
            "{response_path}",
            "--worktree",
            "{worktree_path}",
        ],
        reviewer_command=[
            sys_executable(),
            str(reviewer),
            "--request",
            "{request_path}",
            "--response",
            "{response_path}",
            "--worktree",
            "{worktree_path}",
        ],
        build_runner_identity=IDENTITY_BUILD,
        review_runner_identity=IDENTITY_REVIEW,
    )
    data = cfg.__dict__.copy()
    data["issue_automation"] = issue
    # Ensure base branch name matches local default; init_head_repo uses feat/fix after checkout -B
    # Issue flow checks out from observed base SHA on default branch tip - we pin base_ref main but
    # local repo may be on feat/fix only. Force repository policy and override after git setup.
    data.update(overrides)
    from agent_ops.config import Config

    return Config(**data)


def wire_fake_for_issue(
    fake: FakeGitHub,
    *,
    repository: str = "operator/demo",
    issue_number: int = 7,
    base_sha: str,
    base_ref: str = "main",
    clone_url: str,
    title: str = "Please set broken=0",
    body: str = "demo.txt still has broken=1",
    labels: Optional[List[str]] = None,
    updated_at: str = "2026-08-01T00:00:00Z",
    pushable: bool = True,
    is_private: bool = False,
) -> Dict[str, Any]:
    labels = labels or ["agent-ops:ready"]
    owner, name = repository.split("/", 1)
    issue_node = f"ISSUE_NODE_{issue_number}"
    issue = {
        "id": issue_node,
        "number": issue_number,
        "url": f"https://github.com/{repository}/issues/{issue_number}",
        "title": title,
        "body": body,
        "state": "OPEN",
        "updatedAt": updated_at,
        "author": {"login": "operator"},
        "labels": {
            "nodes": [{"name": label} for label in labels],
            "pageInfo": {"hasNextPage": False},
        },
        "comments": {
            "nodes": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }
    query = f'is:issue is:open repo:{repository} label:"agent-ops:ready"'
    fake.search_pages[query] = [
        [
            {
                "id": 700,
                "number": issue_number,
                "html_url": issue["url"],
                "repository_url": f"https://api.github.com/repos/{repository}",
                "state": "open",
            }
        ]
    ]

    created_prs: List[Dict[str, Any]] = []
    comments: List[Dict[str, Any]] = []

    def gql(query: str, variables: Dict[str, Any]):
        if "createPullRequest" in query:
            number = 9000 + len(created_prs) + 1
            pr = {
                "id": f"PR_NODE_{number}",
                "number": number,
                "url": f"https://github.com/{repository}/pull/{number}",
                "isDraft": True,
                "baseRefName": variables.get("baseRefName"),
                "headRefName": variables.get("headRefName"),
                "headRefOid": base_sha,  # updated after push via side channel
                "state": "OPEN",
                "merged": False,
            }
            # Prefer live resulting sha if tracked
            if getattr(fake, "_last_head_sha", None):
                pr["headRefOid"] = fake._last_head_sha  # type: ignore[attr-defined]
            created_prs.append(pr)
            fake.created_pulls.append(pr)
            return {"data": {"createPullRequest": {"pullRequest": pr}}}
        if "addComment" in query:
            cid = f"ICOM_{len(comments)+1}"
            node = {
                "id": cid,
                "body": variables.get("body"),
                "url": f"https://github.com/{repository}/issues/{issue_number}#issuecomment-{len(comments)+1}",
            }
            comments.append(node)
            fake.created_issue_comments.append(node)
            return {"data": {"addComment": {"commentEdge": {"node": node}}}}
        if "IssueComment" in query and "$id" in query:
            cid = variables.get("id")
            for node in comments:
                if node["id"] == cid:
                    return {"data": {"node": node}}
            return None
        if "pullRequest(number" in query or ("pullRequest" in query and "isDraft" in query and "createPullRequest" not in query):
            number = int(variables.get("number") or 0)
            for pr in created_prs:
                if pr["number"] == number:
                    # refresh head oid if available
                    if getattr(fake, "_last_head_sha", None):
                        pr["headRefOid"] = fake._last_head_sha  # type: ignore[attr-defined]
                    return {"data": {"repository": {"pullRequest": pr}}}
            return None
        if "repository(owner" in query and "issue(number" in query:
            return {
                "data": {
                    "repository": {
                        "isPrivate": is_private,
                        "url": clone_url if clone_url.endswith(".git") else clone_url,
                        "issue": issue,
                    }
                }
            }
        if "ref(qualifiedName" in query:
            return {
                "data": {
                    "repository": {
                        "ref": {"target": {"oid": base_sha}}
                    }
                }
            }
        if "repository(owner" in query and "id" in query and "issue" not in query and "pullRequest" not in query:
            return {"data": {"repository": {"id": f"REPO_{repository}"}}}
        return None

    fake.graphql_handlers.append(gql)
    fake.rest_handlers[f"repos/{repository}"] = {
        "permissions": {"push": pushable, "admin": False, "maintain": False},
        "full_name": repository,
        "private": is_private,
        "owner": {"login": owner},
    }
    # After push, tests can set fake._last_head_sha for PR readback
    fake._last_head_sha = None  # type: ignore[attr-defined]
    return issue


def _align_base_ref(cfg, head_repo: Path, bare: Path) -> Tuple[str, str]:
    """Ensure default branch tip exists as main for issue base_ref."""
    # Create main from current HEAD
    sha = _git(head_repo, "rev-parse", "HEAD")
    _git(head_repo, "branch", "-f", "main", sha)
    # Update bare
    subprocess.run(["git", "push", str(bare), "main:main"], cwd=str(head_repo), check=True, capture_output=True)
    # Update enabled base ref already main
    return sha, "main"


def test_issue_config_missing_fails_closed(tmp_path: Path):
    cfg_path = tmp_path / "bad.json"
    # Minimal invalid issue_automation
    base = make_config(tmp_path)
    # Write incomplete JSON with issue_automation malformed
    payload = {
        "operator_logins": ["operator"],
        "owned_namespaces": ["operator"],
        "excluded_repositories": [],
        "trusted_reviewer_logins": ["reviewer"],
        "workspace_root": str(tmp_path / "w"),
        "state_dir": str(tmp_path / "s"),
        "protected_path_patterns": [],
        "classifier_command": ["true", "{request_path}", "{response_path}"],
        "builder_command": ["true", "{request_path}", "{response_path}", "{worktree_path}"],
        "reviewer_command": ["true", "{request_path}", "{response_path}", "{worktree_path}"],
        "required_runner_identity": {
            "profile": "oscar",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "service_tier": "fast",
        },
        "capability_isolation": {
            "enabled": True,
            "environment_allowlist": ["PATH"],
            "private_markers": [],
        },
        "issue_automation": {"enabled": True},
    }
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        load_config(cfg_path)
        assert False, "expected ConfigError"
    except ConfigError as exc:
        assert "issue_automation" in str(exc)


def test_issue_public_signal_redacts_bodies(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    fake = FakeGitHub()
    git_root = tmp_path / "git"
    head_repo, sha, _ref = init_head_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    sha, base_ref = _align_base_ref(cfg, head_repo, bare)
    # rewrite enabled base if needed
    assert cfg.issue_automation is not None
    issue = cfg.issue_automation
    data = cfg.__dict__.copy()
    data["issue_automation"] = IssueAutomationConfig(
        **{**issue.__dict__, "enabled_repositories": {"operator/demo": base_ref}}
    )
    from agent_ops.config import Config

    cfg = Config(**data)

    secret_body = "fix demo.txt /Users/private/secret token ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    wire_fake_for_issue(
        fake,
        base_sha=sha,
        base_ref=base_ref,
        clone_url=str(bare),
        body=secret_body,
    )
    discovered = discover_issue_signals(fake, cfg)
    assert discovered.signals
    public = discovered.signals[0].to_public_dict()
    blob = json.dumps(public)
    assert "ghp_" not in blob
    assert "/Users/private" not in blob
    assert secret_body not in blob
    assert public["schema"] == "IssueSignalV1"
    assert public["labels"] == ["agent-ops:ready"]


def test_issue_inspect_and_happy_path_draft_pr(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    git_root = tmp_path / "git"
    head_repo, sha, _ref = init_head_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    sha, base_ref = _align_base_ref(cfg, head_repo, bare)
    assert cfg.issue_automation is not None
    issue = cfg.issue_automation
    data = cfg.__dict__.copy()
    data["issue_automation"] = IssueAutomationConfig(
        **{**issue.__dict__, "enabled_repositories": {"operator/demo": base_ref}}
    )
    from agent_ops.config import Config

    cfg = Config(**data)

    fake = FakeGitHub()
    wire_fake_for_issue(
        fake,
        base_sha=sha,
        base_ref=base_ref,
        clone_url=str(bare),
    )

    inspected = inspect_issues(cfg, client=fake)
    assert inspected.inspected_issues >= 1
    assert len(inspected.signals) == 1
    assert inspected.signals[0].issue_number == 7

    # Hook push to capture resulting sha for PR readback
    import agent_ops.maintenance.issue_fix as issue_mod

    original_push = issue_mod.push_head_no_force

    def push_and_track(**kwargs):
        result = original_push(**kwargs)
        if result.ok:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(kwargs["worktree"]),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            fake._last_head_sha = head  # type: ignore[attr-defined]
        return result

    issue_mod.push_head_no_force = push_and_track  # type: ignore[assignment]
    try:
        out = issue_sweep(cfg, client=fake)
    finally:
        issue_mod.push_head_no_force = original_push  # type: ignore[assignment]

    assert out.exit_code == 0, out.message
    assert out.receipt_path is not None
    receipt = json.loads(out.receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "IssueDraftReceiptV1"
    assert receipt["outcome"] == "completed"
    assert receipt["draft_pr_number"]
    assert receipt["draft_pr_url"]
    assert receipt["issue_reply_node_id"]
    assert receipt["resulting_sha"]
    text = out.receipt_path.read_text(encoding="utf-8")
    assert "broken=1" not in text
    assert "ghp_" not in text
    assert assert_no_private_material(text) == []

    # Dedup
    out2 = issue_sweep(cfg, client=fake)
    assert out2.exit_code == 0
    assert out2.message == "no_actionable_signal"


def test_issue_hold_on_security_marker(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    git_root = tmp_path / "git"
    head_repo, sha, _ref = init_head_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    sha, base_ref = _align_base_ref(cfg, head_repo, bare)
    assert cfg.issue_automation is not None
    issue = cfg.issue_automation
    data = cfg.__dict__.copy()
    data["issue_automation"] = IssueAutomationConfig(
        **{**issue.__dict__, "enabled_repositories": {"operator/demo": base_ref}}
    )
    from agent_ops.config import Config

    cfg = Config(**data)
    fake = FakeGitHub()
    wire_fake_for_issue(
        fake,
        base_sha=sha,
        base_ref=base_ref,
        clone_url=str(bare),
        body="Please rotate the production credential store",
    )
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 1
    assert out.receipt_path is not None
    receipt = json.loads(out.receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "held"
    assert not fake.created_pulls


def test_issue_disabled_is_inspect_only(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    git_root = tmp_path / "git"
    head_repo, sha, _ref = init_head_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    sha, base_ref = _align_base_ref(cfg, head_repo, bare)
    assert cfg.issue_automation is not None
    issue = cfg.issue_automation
    data = cfg.__dict__.copy()
    data["issue_automation"] = IssueAutomationConfig(
        **{
            **issue.__dict__,
            "enabled": False,
            "enabled_repositories": {"operator/demo": base_ref},
        }
    )
    from agent_ops.config import Config

    cfg = Config(**data)
    fake = FakeGitHub()
    wire_fake_for_issue(
        fake,
        base_sha=sha,
        base_ref=base_ref,
        clone_url=str(bare),
    )
    inspected = inspect_issues(cfg, client=fake)
    assert inspected.inspected_issues >= 1
    out = issue_sweep(cfg, client=fake)
    assert out.exit_code == 0
    assert out.message == "issue_automation_disabled"
    assert not fake.created_pulls


def test_issue_shares_global_lock_with_pr_loop(tmp_path: Path):
    cfg = make_issue_config(tmp_path)
    led = Ledger(cfg.state_dir / "ledger.sqlite3")
    result, job_id, _ = led.try_claim(
        repository="operator/demo",
        pr_number=1,
        thread_node_id="thread",
        latest_comment_node_id="c1",
        observed_head_sha="a" * 40,
        signal_digest="d" * 64,
        reclaim_after_seconds=3600,
    )
    assert result == "claimed"
    assert job_id
    # Issue sweep should see busy lock
    git_root = tmp_path / "git"
    head_repo, sha, _ref = init_head_repo(git_root)
    bare = make_bare_remote(git_root, head_repo)
    sha, base_ref = _align_base_ref(cfg, head_repo, bare)
    assert cfg.issue_automation is not None
    issue = cfg.issue_automation
    data = cfg.__dict__.copy()
    data["issue_automation"] = IssueAutomationConfig(
        **{**issue.__dict__, "enabled_repositories": {"operator/demo": base_ref}}
    )
    from agent_ops.config import Config

    cfg = Config(**data)
    fake = FakeGitHub()
    wire_fake_for_issue(fake, base_sha=sha, base_ref=base_ref, clone_url=str(bare))
    out = issue_sweep(cfg, client=fake)
    assert out.message == "busy_inspect_only"
