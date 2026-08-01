from __future__ import annotations

import json
import subprocess

import pytest

from github_watch.cli import main
from github_watch.github import GitHub, Notification, PullRequest, ResolvedNotification
from github_watch.worker import WorkerResult


def test_production_shaped_inspect_parses_notifications_without_mutation(config, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_login": config.github_login,
                "allowed_namespaces": list(config.allowed_namespaces),
                "state_dir": str(config.state_dir),
                "worktree_root": str(config.worktree_root),
                "oscar_command": config.oscar_command,
                "oscar_timeout_seconds": config.oscar_timeout_seconds,
                "buzz_channel": config.buzz_channel,
                "buzz_executable": config.buzz_executable,
                "batch_limit": config.batch_limit,
            }
        )
    )
    payload = [
        {
            "id": "thread-9",
            "updated_at": "2026-08-02T00:00:00Z",
            "unread": True,
            "reason": "review_requested",
            "subject": {
                "type": "PullRequest",
                "url": "https://api.github.com/repos/acme/widget/pulls/7",
            },
            "repository": {"full_name": "acme/widget"},
        }
    ]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    exit_code = main(["--config", str(config_path), "inspect"], github=GitHub(runner=runner))

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "id": "thread-9",
            "repository": "acme/widget",
            "pull_number": 7,
            "updated_at": "2026-08-02T00:00:00Z",
            "kind": "review",
        }
    ]
    assert calls == [["gh", "api", "--method", "GET", "/notifications?all=false&participating=false&per_page=20"]]


def test_real_api_shaped_check_resolution_and_completion_verification_use_fixed_argv(config):
    notification_payload = [
        {
            "id": "thread-10",
            "updated_at": "2026-08-02T00:00:00Z",
            "unread": True,
            "reason": "ci_activity",
            "subject": {
                "type": "CheckSuite",
                "url": "https://api.github.com/repos/acme/widget/check-suites/50",
                "title": "CI is red",
                "latest_comment_url": "https://api.github.com/repos/acme/widget/issues/comments/43",
            },
            "repository": {"full_name": "acme/widget"},
        }
    ]
    pull_heads = iter(["old-head", "new-head"])
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        endpoint = argv[-1]
        if endpoint.startswith("/notifications?"):
            payload = notification_payload
        elif endpoint == "/repos/acme/widget/check-suites/50":
            payload = {"head_sha": "old-head", "pull_requests": [{"number": 7}]}
        elif endpoint == "/repos/acme/widget/pulls/7":
            payload = {
                "state": "open",
                "merged_at": None,
                "head": {"sha": next(pull_heads)},
                "html_url": "https://github.com/acme/widget/pull/7",
                "user": {"login": "joelbrilliant"},
                "title": "Fix CI",
                "body": "A safe pull description.",
            }
        elif endpoint == "/repos/acme/widget/issues/comments/43":
            payload = {"body": "The latest CI detail."}
        elif endpoint == "/repos/acme/widget/commits/old-head/check-runs":
            payload = {"check_runs": [{"status": "completed", "conclusion": "success"}]}
        elif endpoint == "/repos/acme/widget/issues/comments/44":
            payload = {
                "id": 44,
                "user": {"login": "joelbrilliant"},
                "issue_url": "https://api.github.com/repos/acme/widget/issues/7",
            }
        else:
            raise AssertionError(endpoint)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    github = GitHub(config, runner=runner)
    item = github.list_notifications(20)[0]
    resolved = github.resolve(item)

    assert resolved is not None
    assert resolved.source_head_sha == "old-head"
    assert resolved.latest_comment_body == "The latest CI detail."
    assert github.head_is_green(resolved) is True
    assert github.verify_completion(resolved, WorkerResult.completed("fixed", "new-head", "issue", 44)) is True
    assert calls == [
        ["gh", "api", "--method", "GET", "/notifications?all=false&participating=false&per_page=20"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/check-suites/50"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/pulls/7"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/issues/comments/43"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/commits/old-head/check-runs"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/pulls/7"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/issues/comments/44"],
    ]


@pytest.mark.parametrize(
    "comment",
    [
        {"id": 44, "user": {"login": "someone-else"}, "issue_url": "/repos/acme/widget/issues/7"},
        {"id": 44, "user": {"login": "joelbrilliant"}, "issue_url": "/repos/acme/widget/issues/8"},
    ],
)
def test_completion_rejects_comment_author_or_pull_mismatch(config, comment):
    item = ResolvedNotification(
        Notification("thread-11", "2026-08-02T00:00:00Z", "acme/widget", 7, "PullRequest", "comment", "review", None),
        PullRequest("acme/widget", 7, "open", False, "new-head", "https://github.com/acme/widget/pull/7", "joelbrilliant"),
        mutation_allowed=True,
    )

    def runner(argv, **kwargs):
        endpoint = argv[-1]
        payload = (
            {"state": "open", "merged_at": None, "head": {"sha": "new-head"}, "html_url": item.pull.url, "user": {"login": "joelbrilliant"}}
            if endpoint == "/repos/acme/widget/pulls/7"
            else comment
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    assert GitHub(config, runner=runner).verify_completion(item, WorkerResult.completed("fixed", "new-head", "issue", 44)) is False


def test_joel_authored_upstream_pr_is_supported_without_an_allowed_base_namespace(config):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        payload = {
            "state": "open",
            "merged_at": None,
            "head": {"sha": "head-1"},
            "html_url": "https://github.com/NousResearch/hermes-agent/pull/74993",
            "user": {"login": "joelbrilliant"},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    notification = GitHub(config, runner=runner).resolve(
        Notification(
            notification_id="thread-upstream",
            updated_at="2026-08-02T00:00:00Z",
            repository="NousResearch/hermes-agent",
            pull_number=74993,
            subject_type="PullRequest",
            reason="review_requested",
            kind="review",
            source_head_sha=None,
        )
    )

    assert notification is not None
    assert notification.mutation_allowed is True
    assert calls == [["gh", "api", "--method", "GET", "/repos/NousResearch/hermes-agent/pulls/74993"]]


def test_non_authored_pr_outside_allowed_namespace_is_read_only_triage(config):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        payload = {
            "state": "open",
            "merged_at": None,
            "head": {"sha": "head-1"},
            "html_url": "https://github.com/NousResearch/hermes-agent/pull/74994",
            "user": {"login": "someone-else"},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    notification = GitHub(config, runner=runner).resolve(
        Notification(
            notification_id="thread-other",
            updated_at="2026-08-02T00:00:00Z",
            repository="NousResearch/hermes-agent",
            pull_number=74994,
            subject_type="PullRequest",
            reason="review_requested",
            kind="review",
            source_head_sha=None,
        )
    )

    assert notification is not None
    assert notification.mutation_allowed is False
    assert calls == [["gh", "api", "--method", "GET", "/repos/NousResearch/hermes-agent/pulls/74994"]]


def test_non_authored_pr_needs_allowed_namespace_and_push_permission(config):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        endpoint = argv[-1]
        if endpoint == "/repos/acme/widget/pulls/7":
            payload = {
                "state": "open",
                "merged_at": None,
                "head": {"sha": "head-1"},
                "html_url": "https://github.com/acme/widget/pull/7",
                "user": {"login": "someone-else"},
            }
        elif endpoint == "/repos/acme/widget":
            payload = {"permissions": {"push": True}}
        else:
            raise AssertionError(endpoint)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    resolved = GitHub(config, runner=runner).resolve(
        Notification(
            notification_id="thread-pushable",
            updated_at="2026-08-02T00:00:00Z",
            repository="acme/widget",
            pull_number=7,
            subject_type="PullRequest",
            reason="review_requested",
            kind="review",
            source_head_sha=None,
        )
    )

    assert resolved is not None
    assert resolved.mutation_allowed is True
    assert calls[-1] == ["gh", "api", "--method", "GET", "/repos/acme/widget"]


def test_live_shaped_participated_non_owned_notification_resolves_with_parent_context(config):
    payload = [
        {
            "id": "24571547895",
            "reason": "comment",
            "updated_at": "2026-08-01T22:15:03Z",
            "unread": True,
            "subject": {
                "type": "PullRequest",
                "url": "/repos/NousResearch/hermes-agent/pulls/62930",
                "title": "Harmless participated discussion",
                "latest_comment_url": "/repos/NousResearch/hermes-agent/issues/comments/5153693059",
            },
            "repository": {"full_name": "NousResearch/hermes-agent"},
        }
    ]

    def runner(argv, **kwargs):
        endpoint = argv[-1]
        if endpoint.startswith("/notifications?"):
            response = payload
        elif endpoint == "/repos/NousResearch/hermes-agent/pulls/62930":
            response = {
                "state": "open",
                "merged_at": None,
                "head": {"sha": "head-62930"},
                "html_url": "https://github.com/NousResearch/hermes-agent/pull/62930",
                "user": {"login": "another-user"},
                "title": "Participated pull request",
                "body": "Context from the parent GitHub API fetch.",
            }
        elif endpoint == "/repos/NousResearch/hermes-agent/issues/comments/5153693059":
            response = {"body": "This comment needs no action."}
        else:
            raise AssertionError(endpoint)
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    github = GitHub(config, runner=runner)
    item = github.list_notifications(20)[0]
    resolved = github.resolve(item)

    assert resolved is not None
    assert resolved.mutation_allowed is False
    assert resolved.notification.latest_comment_url == "/repos/NousResearch/hermes-agent/issues/comments/5153693059"
    assert resolved.latest_comment_body == "This comment needs no action."
