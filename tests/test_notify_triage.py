"""Notification triage front-door tests (paste-workflow automation)."""

from __future__ import annotations

from pathlib import Path

from agent_ops.config import Config, NotificationTriagePolicy
from agent_ops.github.client import FakeGitHub
from agent_ops.maintenance.ledger import Ledger
from agent_ops.maintenance.notify_triage import (
    DECISION_NO_ACTION,
    DECISION_NEEDS_JOEL,
    inspect_notifications,
    triage_notifications,
)
from tests.conftest import make_config


def _open_pr_payload(*, number: int = 7, title: str = "fix: demo", ref: str = "feat/x") -> dict:
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/operator/demo/pull/{number}",
        "user": {"login": "operator"},
        "title": title,
        "draft": False,
        "merged": False,
        "head": {
            "sha": "c" * 40,
            "ref": ref,
            "repo": {"full_name": "operator/demo"},
        },
        "base": {"repo": {"full_name": "operator/demo"}},
    }


def test_ci_failure_on_green_pr_is_no_action(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    gh = FakeGitHub()
    gh.login = "operator"
    gh.notifications = [
        {
            "id": "ci-1",
            "reason": "ci_activity",
            "unread": True,
            "updated_at": "2026-08-02T01:00:00Z",
            "subject": {
                "title": "controlled-canary-review: failed on main branch",
                "type": "CheckSuite",
                "url": "",
                "latest_comment_url": "",
            },
            "repository": {"full_name": "operator/demo"},
        }
    ]
    # Must match notify_triage._find_open_prs_for_branch query construction.
    gh.search_pages["is:pr is:open repo:operator/demo head:main"] = [
        [
            {
                "number": 1,
                "pull_request": {},
                "html_url": "https://github.com/operator/demo/pull/1",
                "user": {"login": "operator"},
            }
        ]
    ]
    gh.rest_handlers["repos/operator/demo/pulls/1"] = {
        "number": 1,
        "state": "open",
        "html_url": "https://github.com/operator/demo/pull/1",
        "user": {"login": "operator"},
        "title": "canary",
        "draft": False,
        "merged": False,
        "head": {
            "sha": "b" * 40,
            "ref": "main",
            "repo": {"full_name": "operator/demo"},
        },
        "base": {"repo": {"full_name": "operator/demo"}},
    }
    gh.required_check_rows["operator/demo#1"] = [
        {"bucket": "pass", "name": "ci", "state": "SUCCESS"}
    ]

    outcome = triage_notifications(cfg, client=gh, run_fix_sweep=False)
    assert outcome.dismissed >= 1
    assert outcome.needs_joel == 0
    assert "ci-1" in gh.marked_read
    ledger = Ledger(cfg.state_dir / "ledger.sqlite3")
    row = ledger.get_notification("ci-1")
    assert row is not None
    assert row["decision"] == DECISION_NO_ACTION
    assert row["reason"] == "check_failure_superseded_or_current_green"


def test_validation_comment_is_no_action(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    gh = FakeGitHub()
    gh.login = "operator"
    comment_path = "repos/operator/demo/issues/comments/99"
    gh.notifications = [
        {
            "id": "val-1",
            "reason": "comment",
            "unread": True,
            "updated_at": "2026-08-01T19:28:01Z",
            "subject": {
                "title": "fix: demo",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/operator/demo/pulls/7",
                "latest_comment_url": f"https://api.github.com/{comment_path}",
            },
            "repository": {"full_name": "operator/demo"},
        }
    ]
    gh.rest_handlers["repos/operator/demo/pulls/7"] = _open_pr_payload()
    gh.rest_handlers[comment_path] = {
        "user": {"login": "reviewer"},
        "body": "Independent validation on f978: looks good, no action required.",
        "created_at": "2026-08-01T19:28:01Z",
        "html_url": "https://github.com/operator/demo/pull/7#issuecomment-1",
        "author_association": "COLLABORATOR",
    }

    note = triage_notifications(cfg, client=gh, run_fix_sweep=False)
    assert note.dismissed == 1
    assert note.needs_joel == 0
    assert "val-1" in gh.marked_read


def test_question_comment_needs_joel_and_notifies(tmp_path: Path) -> None:
    ping_log = tmp_path / "ping.log"
    cfg = make_config(tmp_path)
    data = cfg.__dict__.copy()
    data["notification_triage"] = NotificationTriagePolicy(
        enabled=True,
        needs_joel_command=[
            "python3",
            "-c",
            (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).write_text(sys.argv[2]); print('ok')"
            ),
            str(ping_log),
            "{message}",
        ],
        mark_read_on_needs_joel=True,
    )
    cfg = Config(**data)
    gh = FakeGitHub()
    gh.login = "operator"
    comment_path = "repos/operator/demo/issues/comments/100"
    gh.notifications = [
        {
            "id": "q-1",
            "reason": "comment",
            "unread": True,
            "updated_at": "2026-08-02T02:00:00Z",
            "subject": {
                "title": "fix: demo",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/operator/demo/pulls/7",
                "latest_comment_url": f"https://api.github.com/{comment_path}",
            },
            "repository": {"full_name": "operator/demo"},
        }
    ]
    gh.rest_handlers["repos/operator/demo/pulls/7"] = _open_pr_payload()
    gh.rest_handlers[comment_path] = {
        "user": {"login": "maintainer"},
        "body": "Which option do you want for the cloud install path?",
        "created_at": "2026-08-02T02:00:00Z",
        "html_url": "https://github.com/operator/demo/pull/7#issuecomment-2",
        "author_association": "MEMBER",
    }

    outcome = triage_notifications(cfg, client=gh, run_fix_sweep=False)
    assert outcome.needs_joel == 1
    assert outcome.notified_joel == 1
    assert ping_log.exists()
    body = ping_log.read_text()
    assert "NEEDS_JOEL" in body or "option" in body.lower()


def test_lgtm_with_question_still_needs_joel(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    gh = FakeGitHub()
    gh.login = "operator"
    comment_path = "repos/operator/demo/issues/comments/101"
    gh.notifications = [
        {
            "id": "mixed-1",
            "reason": "comment",
            "unread": True,
            "updated_at": "2026-08-02T02:30:00Z",
            "subject": {
                "title": "fix: demo",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/operator/demo/pulls/7",
                "latest_comment_url": f"https://api.github.com/{comment_path}",
            },
            "repository": {"full_name": "operator/demo"},
        }
    ]
    gh.rest_handlers["repos/operator/demo/pulls/7"] = _open_pr_payload()
    gh.rest_handlers[comment_path] = {
        "user": {"login": "maintainer"},
        "body": "LGTM overall - which option should we ship for the install path?",
        "created_at": "2026-08-02T02:30:00Z",
        "html_url": "https://github.com/operator/demo/pull/7#issuecomment-3",
        "author_association": "MEMBER",
    }

    outcome = triage_notifications(cfg, client=gh, run_fix_sweep=False)
    assert outcome.needs_joel == 1
    assert outcome.dismissed == 0


def test_plain_issue_notification_needs_joel_not_silent_drop(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    gh = FakeGitHub()
    gh.login = "operator"
    gh.notifications = [
        {
            "id": "issue-1",
            "reason": "mention",
            "unread": True,
            "updated_at": "2026-08-02T04:00:00Z",
            "subject": {
                "title": "Please decide on rollout",
                "type": "Issue",
                "url": "https://api.github.com/repos/operator/demo/issues/42",
                "latest_comment_url": "",
            },
            "repository": {"full_name": "operator/demo"},
        }
    ]

    def _gone(path: str = "repos/operator/demo/pulls/42") -> dict:
        from agent_ops.github.client import GitHubError

        raise GitHubError("HTTP 404: Not Found")

    gh.rest_handlers["repos/operator/demo/pulls/42"] = _gone

    outcome = triage_notifications(cfg, client=gh, run_fix_sweep=False)
    assert outcome.needs_joel == 1
    assert outcome.dismissed == 0
    assert "issue-1" not in gh.marked_read
    ledger = Ledger(cfg.state_dir / "ledger.sqlite3")
    row = ledger.get_notification("issue-1")
    assert row is not None
    assert row["decision"] == DECISION_NEEDS_JOEL
    assert row["reason"] == "issue_notification_needs_human_scan"


def test_inspect_only_does_not_mark_read(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    gh = FakeGitHub()
    gh.login = "operator"
    gh.notifications = [
        {
            "id": "i-1",
            "reason": "state_change",
            "unread": True,
            "updated_at": "2026-08-02T03:00:00Z",
            "subject": {
                "title": "fix: demo",
                "type": "PullRequest",
                "url": "https://api.github.com/repos/operator/demo/pulls/7",
                "latest_comment_url": "",
            },
            "repository": {"full_name": "operator/demo"},
        }
    ]
    gh.rest_handlers["repos/operator/demo/pulls/7"] = {
        **_open_pr_payload(),
        "state": "closed",
        "merged": True,
    }
    outcome = inspect_notifications(cfg, client=gh)
    assert outcome.dismissed + outcome.needs_joel + outcome.acted_fix >= 1
    assert gh.marked_read == []


def test_needs_joel_command_rejects_embedded_message_placeholder(tmp_path: Path) -> None:
    from agent_ops.maintenance.notify_triage import _notify_joel

    sink = tmp_path / "sink.txt"
    cfg = make_config(tmp_path)
    data = cfg.__dict__.copy()
    data["notification_triage"] = NotificationTriagePolicy(
        enabled=True,
        needs_joel_command=["/bin/sh", "-c", "echo {message} > " + str(sink)],
    )
    cfg = Config(**data)
    assert _notify_joel(cfg, "NEEDS_JOEL: hello; rm -rf /") is False
    assert not sink.exists()

    # Whole-token placeholder remains safe and delivers as argv.
    log = tmp_path / "ok.log"
    data_ok = make_config(tmp_path).__dict__.copy()
    data_ok["notification_triage"] = NotificationTriagePolicy(
        enabled=True,
        needs_joel_command=[
            "python3",
            "-c",
            "import sys; open(sys.argv[1], 'w').write(sys.argv[2])",
            str(log),
            "{message}",
        ],
    )
    cfg_ok = Config(**data_ok)
    assert _notify_joel(cfg_ok, "safe message") is True
    assert "safe message" in log.read_text()
