from __future__ import annotations

import pytest

from github_watch.github import Notification, PullRequest, ResolvedNotification
from github_watch.loop import WatchLoop
from github_watch.state import StateStore
from github_watch.worker import WorkerResult

from .conftest import FakeBuzz, FakeGitHub, FakeWorker, note, resolved


def run_loop(config, github, worker, buzz):
    state = StateStore(config.state_dir)
    return WatchLoop(config, github, worker, state, buzz_send=buzz).run(), state


@pytest.mark.parametrize("state,merged", [("closed", False), ("open", True)])
def test_closed_or_merged_pr_is_marked_read_without_oscar_or_buzz(config, state, merged):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item, state=state, merged=merged)})
    worker = FakeWorker()
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    assert worker.calls == []
    assert buzz.messages == []
    assert ("mark", item.notification_id) in github.calls
    assert store.get(item.notification_id, item.updated_at).read_completed is True


def test_superseded_failed_ci_with_green_current_head_is_silent(config):
    item = note(kind="check")
    item = item.__class__(**{**item.__dict__, "source_head_sha": "old-head"})
    github = FakeGitHub([item], {item.notification_id: resolved(item, head_sha="new-head")}, green=True)
    worker = FakeWorker()
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    assert worker.calls == []
    assert buzz.messages == []
    assert ("green", item.notification_id) in github.calls
    assert store.get(item.notification_id, item.updated_at).outcome == "no_action"


@pytest.mark.parametrize("kind", ["review", "check"])
def test_current_review_or_failed_check_launches_exactly_one_oscar_session(config, kind):
    item = note(kind=kind)
    if kind == "check":
        item = item.__class__(**{**item.__dict__, "source_head_sha": "head-1"})
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(WorkerResult.no_action("already handled"))
    buzz = FakeBuzz()

    run_loop(config, github, worker, buzz)

    assert worker.calls == [item.notification_id]
    assert [call for call in github.calls if call[0] == "mark"] == [("mark", item.notification_id)]
    assert buzz.messages == []


def test_ambiguous_supported_notification_is_blocked_once_and_remains_unread(config):
    item = note(kind="check")
    github = FakeGitHub([item], {})
    worker = FakeWorker()
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    loop.run()

    row = store.get(item.notification_id, item.updated_at)
    assert row.outcome == "blocked"
    assert row.pull_number == 0
    assert row.read_completed is False
    assert row.buzz_completed is True
    assert worker.calls == []
    assert len(buzz.messages) == 1
    assert "could not be resolved safely" in buzz.messages[0]


def test_resolution_failure_is_deduplicated_blocked_work(config):
    item = note(kind="check")
    github = FakeGitHub([item], {})
    github.resolve = lambda _: (_ for _ in ()).throw(RuntimeError("read failed"))
    worker = FakeWorker()
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    loop.run()

    assert store.get(item.notification_id, item.updated_at).outcome == "blocked"
    assert len(buzz.messages) == 1
    assert worker.calls == []


def test_green_check_proof_failure_routes_to_oscar(config):
    item = note(kind="check")
    item = item.__class__(**{**item.__dict__, "source_head_sha": "old-head"})
    github = FakeGitHub([item], {item.notification_id: resolved(item, head_sha="new-head")})
    github.head_is_green = lambda _: (_ for _ in ()).throw(RuntimeError("read failed"))
    worker = FakeWorker(WorkerResult.no_action("inspected live state"))
    buzz = FakeBuzz()

    run_loop(config, github, worker, buzz)

    assert worker.calls == [item.notification_id]


def test_valid_no_action_marks_read_and_sends_no_buzz(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(WorkerResult.no_action("not needed"))
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    row = store.get(item.notification_id, item.updated_at)
    assert row.outcome == "no_action"
    assert row.read_completed is True
    assert row.buzz_completed is False
    assert buzz.messages == []


def test_participated_non_owned_harmless_notification_becomes_no_action_and_is_read(config):
    item = Notification(
        "24571547895",
        "2026-08-01T22:15:03Z",
        "NousResearch/hermes-agent",
        62930,
        "PullRequest",
        "comment",
        "review",
        None,
        "/repos/NousResearch/hermes-agent/pulls/62930",
        "Harmless participated discussion",
        "/repos/NousResearch/hermes-agent/issues/comments/5153693059",
    )
    decision = ResolvedNotification(
        item,
        PullRequest("NousResearch/hermes-agent", 62930, "open", False, "head-62930", "https://github.com/NousResearch/hermes-agent/pull/62930", "another-user"),
        mutation_allowed=False,
        latest_comment_body="This comment needs no action.",
    )
    github = FakeGitHub([item], {item.notification_id: decision})
    worker = FakeWorker(WorkerResult.no_action("nothing remains"))
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    assert worker.calls == ["24571547895"]
    assert ("mark", "24571547895") in github.calls
    assert store.get(item.notification_id, item.updated_at).outcome == "no_action"
    assert buzz.messages == []


def test_non_owned_completed_result_is_rejected_before_remote_verification(config):
    item = note()
    decision = resolved(item, mutation_allowed=False, latest_comment_body="No mutation permission.")
    github = FakeGitHub([item], {item.notification_id: decision})
    worker = FakeWorker(WorkerResult.completed("made a change", "head-2", "issue", 44))
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    row = store.get(item.notification_id, item.updated_at)
    assert row.outcome == "blocked"
    assert row.read_completed is False
    assert [call for call in github.calls if call[0] == "verify"] == []
    assert "read-only triage" in buzz.messages[0]


def test_missing_latest_comment_context_still_reaches_read_only_oscar(config):
    item = note()
    item = item.__class__(**{**item.__dict__, "latest_comment_url": "/repos/acme/widget/issues/comments/4"})
    github = FakeGitHub([item], {item.notification_id: resolved(item, mutation_allowed=False)})
    worker = FakeWorker(WorkerResult.no_action("live pull needs no action"))
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    row = store.get(item.notification_id, item.updated_at)
    assert worker.calls == [item.notification_id]
    assert row.outcome == "no_action"
    assert row.read_completed is True
    assert buzz.messages == []


def test_completed_is_blocked_until_remote_head_and_comment_verify(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)}, verification=False)
    worker = FakeWorker(WorkerResult.completed("fixed formatter", "head-2", "issue", 44))
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    row = store.get(item.notification_id, item.updated_at)
    assert row.outcome == "blocked"
    assert row.read_completed is False
    assert ("mark", item.notification_id) not in github.calls
    assert buzz.messages == [
        "Oscar is blocked on acme/widget#7: completion verification failed. Proposed fix: inspect the remote head and GitHub reply before retrying. https://github.com/acme/widget/pull/7"
    ]


def test_verified_completed_marks_read_sends_one_buzz_and_records_effects(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(WorkerResult.completed("fixed formatter", "head-2", "issue", 44))
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    row = store.get(item.notification_id, item.updated_at)
    assert row.outcome == "completed"
    assert row.read_completed is True
    assert row.buzz_completed is True
    assert buzz.messages == ["Oscar completed acme/widget#7: fixed formatter. https://github.com/acme/widget/pull/7"]


@pytest.mark.parametrize(
    "result",
    [
        WorkerResult.blocked("Oscar timed out", "retry after checking the PR"),
        WorkerResult.blocked("Oscar returned invalid output", "inspect the Oscar result"),
    ],
)
def test_blocked_worker_outcomes_remain_unread_and_notify_once(config, result):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(result)
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    row = store.get(item.notification_id, item.updated_at)
    assert row.outcome == "blocked"
    assert row.read_completed is False
    assert row.buzz_completed is True
    assert ("mark", item.notification_id) not in github.calls
    assert len(buzz.messages) == 1
    assert "Proposed fix:" in buzz.messages[0]


def test_repeated_same_notification_update_does_not_repeat_actions(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(WorkerResult.no_action("not needed"))
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    first_calls = list(github.calls)
    loop.run()

    assert worker.calls == [item.notification_id]
    assert [call for call in github.calls if call[0] == "mark"] == [("mark", item.notification_id)]
    assert github.calls == first_calls + [("list", str(config.batch_limit)), ("resolve", item.notification_id)]
    assert buzz.messages == []


def test_same_pr_notifications_in_one_poll_run_one_oscar_and_one_buzz(config):
    older = note(
        notification_id="thread-old",
        updated_at="2026-08-02T00:00:00Z",
        kind="check",
    )
    newer = note(
        notification_id="thread-new",
        updated_at="2026-08-02T01:00:00Z",
        kind="check",
    )
    github = FakeGitHub(
        [older, newer],
        {
            older.notification_id: resolved(older),
            newer.notification_id: resolved(newer),
        },
    )
    worker = FakeWorker(
        WorkerResult.completed("retriggered CI", "head-2", "issue", 44)
    )
    buzz = FakeBuzz()

    result, store = run_loop(config, github, worker, buzz)

    assert result.processed == 2
    assert worker.calls == ["thread-new"]
    assert [call for call in github.calls if call[0] == "mark"] == [
        ("mark", "thread-new"),
        ("mark", "thread-old"),
    ]
    assert buzz.messages == [
        "Oscar completed acme/widget#7: retriggered CI. https://github.com/acme/widget/pull/7"
    ]
    older_row = store.get(older.notification_id, older.updated_at)
    assert older_row.outcome == "completed"
    assert older_row.read_completed is True
    assert older_row.buzz_completed is True


def test_blocked_same_pr_notifications_do_not_repeat_worker_or_buzz(config):
    newer = note("thread-new", "2026-08-02T01:00:00Z")
    older = note("thread-old", "2026-08-02T00:00:00Z")
    github = FakeGitHub(
        [newer, older],
        {
            newer.notification_id: resolved(newer),
            older.notification_id: resolved(older),
        },
    )
    worker = FakeWorker(
        WorkerResult.blocked("Oscar timed out", "inspect the pull request")
    )
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    loop.run()

    assert worker.calls == ["thread-new"]
    assert len(buzz.messages) == 1
    assert [call for call in github.calls if call[0] == "mark"] == []
    assert store.get(older.notification_id, older.updated_at).outcome == "blocked"


def test_new_notification_in_later_poll_still_gets_live_triage(config):
    first = note("thread-first", "2026-08-02T00:00:00Z")
    github = FakeGitHub(
        [first],
        {first.notification_id: resolved(first)},
    )
    worker = FakeWorker(
        WorkerResult.completed("fixed first request", "head-2", "issue", 44),
        WorkerResult.no_action("new comment already handled"),
    )
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    second = note("thread-second", "2026-08-02T02:00:00Z")
    github.notifications = [second]
    github.resolutions[second.notification_id] = resolved(second, head_sha="head-2")
    loop.run()

    assert worker.calls == ["thread-first", "thread-second"]
    assert ("mark", "thread-second") in github.calls


def test_newer_completed_work_supersedes_an_older_blocked_notification(config):
    newer = note("thread-newer", "2026-08-02T02:00:00Z")
    older = note("thread-older", "2026-08-02T01:00:00Z")
    github = FakeGitHub(
        [newer],
        {newer.notification_id: resolved(newer)},
    )
    worker = FakeWorker(
        WorkerResult.completed("fixed current head", "head-2", "issue", 44)
    )
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    older_item = resolved(older, head_sha="head-2")
    store.record(
        older_item,
        "blocked",
        "head-2",
        "Oscar is blocked on stale work",
    )
    store.set_buzz(store.get(older.notification_id, older.updated_at))
    github.notifications = [older]
    github.resolutions[older.notification_id] = older_item
    loop.run()

    row = store.get(older.notification_id, older.updated_at)
    assert worker.calls == ["thread-newer"]
    assert len(buzz.messages) == 1
    assert row.outcome == "completed"
    assert row.read_completed is True
    assert row.buzz_completed is True
    assert ("mark", "thread-older") in github.calls


def test_same_notification_update_with_changed_head_does_not_repeat_actions(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(WorkerResult.blocked("manual check", "inspect the pull request"))
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    github.resolutions[item.notification_id] = resolved(item, head_sha="new-head")
    loop.run()

    assert worker.calls == [item.notification_id]
    assert len(buzz.messages) == 1


def test_changed_notification_before_mark_read_supersedes_old_effects(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    github.mark_stale = True
    worker = FakeWorker(WorkerResult.completed("fixed formatter", "head-2", "issue", 44))
    buzz = FakeBuzz()

    _, store = run_loop(config, github, worker, buzz)

    row = store.get(item.notification_id, item.updated_at)
    assert row.read_completed is True
    assert row.buzz_completed is True
    assert row.last_error == "superseded before mark-read"
    assert buzz.messages == ["Oscar completed acme/widget#7: fixed formatter. https://github.com/acme/widget/pull/7"]


def test_failed_mark_read_retries_only_mark_read(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    github.mark_failures = 1
    worker = FakeWorker(WorkerResult.no_action("not needed"))
    buzz = FakeBuzz()
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    loop.run()

    assert worker.calls == [item.notification_id]
    assert [call for call in github.calls if call[0] == "mark"] == [
        ("mark", item.notification_id),
        ("mark", item.notification_id),
    ]
    assert store.get(item.notification_id, item.updated_at).read_completed is True


def test_failed_buzz_retries_only_buzz(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(WorkerResult.completed("fixed formatter", "head-2", "issue", 44))
    buzz = FakeBuzz()
    buzz.failures = 1
    store = StateStore(config.state_dir)
    loop = WatchLoop(config, github, worker, store, buzz_send=buzz)

    loop.run()
    loop.run()

    assert worker.calls == [item.notification_id]
    assert [call for call in github.calls if call[0] == "verify"] == [("verify", item.notification_id)]
    assert [call for call in github.calls if call[0] == "mark"] == [("mark", item.notification_id)]
    assert len(buzz.messages) == 2
    assert store.get(item.notification_id, item.updated_at).buzz_completed is True


def test_concurrent_invocation_exits_quietly_while_os_lock_is_held(config):
    item = note()
    github = FakeGitHub([item], {item.notification_id: resolved(item)})
    worker = FakeWorker(WorkerResult.no_action("not needed"))
    buzz = FakeBuzz()
    holder = StateStore(config.state_dir)
    assert holder.lock.acquire() is True

    result = WatchLoop(config, github, worker, StateStore(config.state_dir), buzz_send=buzz).run()

    holder.lock.release()
    assert result.locked is True
    assert github.calls == []
    assert worker.calls == []
    assert buzz.messages == []


def test_buzz_delivery_uses_the_fixed_wrapper_argv(config, monkeypatch):
    calls = []

    def sender(argv, **kwargs):
        calls.append((argv, kwargs))

    monkeypatch.setattr("github_watch.loop.subprocess.run", sender)
    WatchLoop(config, None, None, StateStore(config.state_dir))._send_buzz("completed safely")

    assert calls == [
        (
            [config.buzz_executable, "completed safely", config.buzz_channel],
            {"check": True, "capture_output": True, "text": True, "shell": False},
        )
    ]
