from __future__ import annotations

import json
import subprocess

import pytest

from github_watch.cli import main
from github_watch.github import GitHub, Notification, PullRequest, ResolvedNotification, StaleNotification
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
                "max_notification_age_hours": config.max_notification_age_hours,
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
        },
        {
            "id": "thread-10",
            "updated_at": "2026-08-02T00:01:00Z",
            "unread": True,
            "reason": "ci_activity",
            "subject": {
                "type": "CheckSuite",
                "url": None,
                "title": "CI workflow run failed for fix/widget branch",
                "latest_comment_url": None,
            },
            "repository": {"full_name": "acme/widget"},
        },
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
        },
        {
            "id": "thread-10",
            "repository": "acme/widget",
            "pull_number": None,
            "updated_at": "2026-08-02T00:01:00Z",
            "kind": "check",
        },
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
        elif endpoint == "/repos/acme/widget/commits/old-head/check-runs?per_page=100":
            payload = {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "success"}]}
        elif endpoint == "/repos/acme/widget/commits/old-head/status":
            payload = {"state": "pending", "statuses": []}
        elif endpoint == "/repos/acme/widget/issues/comments/44":
            payload = {
                "id": 44,
                "user": {"login": "joelbrilliant"},
                "issue_url": "https://api.github.com/repos/acme/widget/issues/7",
                "body": "Fixed the formatter and verified the check.",
                "created_at": "2026-08-02T00:01:00Z",
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
        ["gh", "api", "--method", "GET", "/repos/acme/widget/commits/old-head/check-runs?per_page=100"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/commits/old-head/status"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/pulls/7"],
        ["gh", "api", "--method", "GET", "/repos/acme/widget/issues/comments/44"],
    ]


@pytest.mark.parametrize(
    "comment",
    [
        {"id": 44, "user": {"login": "someone-else"}, "issue_url": "/repos/acme/widget/issues/7", "body": "fixed", "created_at": "2026-08-02T00:01:00Z"},
        {"id": 44, "user": {"login": "joelbrilliant"}, "issue_url": "/repos/acme/widget/issues/8", "body": "fixed", "created_at": "2026-08-02T00:01:00Z"},
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


def test_completion_rejects_an_old_or_empty_reply(config):
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
            else {"id": 44, "user": {"login": "joelbrilliant"}, "issue_url": "/repos/acme/widget/issues/7", "body": "", "created_at": "2026-08-01T23:59:59Z"}
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    assert GitHub(config, runner=runner).verify_completion(item, WorkerResult.completed("fixed", "new-head", "issue", 44)) is False


@pytest.mark.parametrize(
    "kind,endpoint,link_key,timestamp_key",
    [
        ("review", "/repos/acme/widget/pulls/comments/44", "pull_request_url", "created_at"),
        ("review_summary", "/repos/acme/widget/pulls/7/reviews/44", None, "submitted_at"),
    ],
)
def test_completion_verifies_exact_pr_review_api_shapes(config, kind, endpoint, link_key, timestamp_key):
    item = ResolvedNotification(
        Notification("thread-11", "2026-08-02T00:00:00Z", "acme/widget", 7, "PullRequest", "comment", "review", None),
        PullRequest("acme/widget", 7, "open", False, "new-head", "https://github.com/acme/widget/pull/7", "joelbrilliant"),
        mutation_allowed=True,
    )

    def runner(argv, **kwargs):
        current = argv[-1]
        if current == "/repos/acme/widget/pulls/7":
            payload = {"state": "open", "merged_at": None, "head": {"sha": "new-head"}, "html_url": item.pull.url, "user": {"login": "joelbrilliant"}}
        else:
            assert current == endpoint
            payload = {"id": 44, "user": {"login": "joelbrilliant"}, "body": "Fixed and verified.", timestamp_key: "2026-08-02T00:01:00Z"}
            if link_key:
                payload[link_key] = "/repos/acme/widget/pulls/7"
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    result = WorkerResult.completed("fixed", "new-head", kind, 44)
    assert GitHub(config, runner=runner).verify_completion(item, result) is True


def test_null_url_checksuite_resolves_through_exact_workflow_run_and_head_ref(config):
    payload = [{
        "id": "thread-null-check",
        "updated_at": "2026-08-02T00:02:00Z",
        "unread": True,
        "reason": "ci_activity",
        "subject": {"type": "CheckSuite", "url": None, "latest_comment_url": None, "title": "CI workflow run failed for fix/widget branch"},
        "repository": {"full_name": "acme/widget"},
    }]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        endpoint = argv[-1]
        if endpoint.startswith("/notifications?"):
            response = payload
        elif endpoint == "/repos/acme/widget/actions/runs?branch=fix%2Fwidget&event=pull_request&status=failure&per_page=100":
            response = {"workflow_runs": [{
                "updated_at": "2026-08-02T00:01:30Z",
                "head_branch": "fix/widget",
                "head_sha": "failed-head",
                "head_repository": {"owner": {"login": "joelbrilliant"}},
            }]}
        elif endpoint == "/repos/acme/widget/pulls?state=all&head=joelbrilliant%3Afix%2Fwidget&per_page=100":
            response = [{"number": 7}]
        elif endpoint == "/repos/acme/widget/pulls/7":
            response = {"state": "open", "merged_at": None, "head": {"sha": "current-head"}, "html_url": "https://github.com/acme/widget/pull/7", "user": {"login": "joelbrilliant"}}
        else:
            raise AssertionError(endpoint)
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    github = GitHub(config, runner=runner)
    item = github.resolve(github.list_notifications(20)[0])

    assert item is not None
    assert item.pull.number == 7
    assert item.source_head_sha == "failed-head"
    assert calls[1][-1].startswith("/repos/acme/widget/actions/runs?")


def test_null_url_pull_request_is_retained_for_blocked_handling(config):
    payload = [{
        "id": "thread-null-pull",
        "updated_at": "2026-08-02T00:02:00Z",
        "unread": True,
        "reason": "comment",
        "subject": {"type": "PullRequest", "url": None, "latest_comment_url": None, "title": "Unavailable pull"},
        "repository": {"full_name": "acme/widget"},
    }]

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    item = GitHub(config, runner=runner).list_notifications(20)[0]

    assert item.kind == "review"
    assert item.pull_number is None


def test_mark_read_requires_the_same_notification_update(config):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        payload = {"updated_at": "2026-08-02T00:01:00Z"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    github = GitHub(config, runner=runner)
    github.mark_read("thread-1", "2026-08-02T00:01:00Z")
    with pytest.raises(StaleNotification):
        github.mark_read("thread-1", "2026-08-02T00:00:00Z")

    assert calls[0][-1] == "/notifications/threads/thread-1"
    assert calls[1] == ["gh", "api", "--method", "PATCH", "/notifications/threads/thread-1"]


def test_green_head_rejects_truncated_commit_statuses(config):
    item = ResolvedNotification(
        Notification("thread-check", "2026-08-02T00:00:00Z", "acme/widget", 7, "CheckSuite", "ci_activity", "check", "old-head"),
        PullRequest("acme/widget", 7, "open", False, "new-head", "https://github.com/acme/widget/pull/7", "joelbrilliant"),
    )

    def runner(argv, **kwargs):
        payload = (
            {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "success"}]}
            if "check-runs" in argv[-1]
            else {"state": "success", "total_count": 2, "statuses": [{"state": "success"}]}
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    assert GitHub(config, runner=runner).head_is_green(item) is False


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
