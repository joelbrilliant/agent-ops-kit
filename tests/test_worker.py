from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from github_watch.worker import OscarWorker

from .conftest import note, resolved


class FinishedProcess:
    pid = 43210

    def __init__(self, stdout: str):
        self.stdout = stdout

    def poll(self):
        return 0

    def communicate(self):
        return self.stdout, ""


class RunningProcess:
    pid = 43211

    def poll(self):
        return None

    def communicate(self):
        return "", ""


def test_worker_uses_fixed_headless_argv_and_blocks_observed_gui(config, monkeypatch, tmp_path):
    item = note()
    commands = []
    launches = []
    killed = []
    fake_gui = tmp_path / "Harmless.app" / "Contents" / "MacOS" / "fake-gui"

    def runner(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def process_factory(argv, **kwargs):
        launches.append((argv, kwargs))
        return RunningProcess()

    monkeypatch.setattr("github_watch.worker.os.killpg", lambda pid, signal: killed.append(pid))
    worker = OscarWorker(
        config,
        runner=runner,
        process_factory=process_factory,
        gui_scan=lambda _: str(fake_gui),
        clock=lambda: 0.0,
        sleep=lambda _: None,
    )

    result = worker.run(resolved(item))

    assert result.outcome == "blocked"
    assert fake_gui.name in result.blocker
    assert commands[0][:3] == ["git", "clone", "--quiet"]
    assert commands[1][:4] == ["git", "-C", commands[1][2], "checkout"]
    argv, kwargs = launches[0]
    assert argv[:3] == ["/usr/bin/sandbox-exec", "-f", argv[2]]
    assert argv[3] == config.oscar_command
    assert len(argv) == 5
    assert '"repository": "acme/widget"' in argv[4]
    assert "--profile" not in argv
    assert "--one-shot" not in argv
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["CI"] == "1"
    assert "DISPLAY" not in kwargs["env"]
    assert "WAYLAND_DISPLAY" not in kwargs["env"]
    assert killed == [RunningProcess.pid]
    assert OscarWorker._is_gui_command("/usr/bin/open harmless-path") is True


def test_worker_accepts_only_one_valid_json_object(config):
    item = note()

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")

    def process_factory(argv, **kwargs):
        return FinishedProcess(json.dumps({"outcome": "completed", "summary": "fixed", "head_sha": "new-head", "comment_kind": "issue", "comment_id": 4}))

    result = OscarWorker(config, runner=runner, process_factory=process_factory).run(resolved(item))
    assert result.outcome == "completed"
    assert result.comment_id == 4


def test_read_only_worker_strips_credentials_and_receives_parent_context(config, monkeypatch):
    item = note()
    item = item.__class__(
        **{
            **item.__dict__,
            "subject_title": "A participated comment",
            "latest_comment_url": "/repos/acme/widget/issues/comments/4",
        }
    )
    captured = []
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "must-not-reach-oscar")

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")

    def process_factory(argv, **kwargs):
        captured.append((argv, kwargs))
        assert Path(kwargs["env"]["GH_CONFIG_DIR"]).is_dir()
        return FinishedProcess(json.dumps({"outcome": "no_action", "summary": "nothing remains"}))

    result = OscarWorker(config, runner=runner, process_factory=process_factory).run(
        resolved(item, mutation_allowed=False, latest_comment_body="A harmless latest comment.")
    )

    argv, kwargs = captured[0]
    environment = kwargs["env"]
    assert result.outcome == "no_action"
    assert all(key not in environment for key in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"))
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_0"] == ""
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"
    assert '"mutation_allowed": false' in argv[4]
    assert "This is read-only triage." in argv[4]
    assert "A harmless latest comment." in argv[4]


def test_worker_converts_malformed_output_and_timeout_to_blocked(config):
    item = note()

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")

    malformed = OscarWorker(config, runner=runner, process_factory=lambda *a, **k: FinishedProcess("not json")).run(resolved(item))
    timeout_config = config.__class__(**{**config.__dict__, "oscar_timeout_seconds": 0})
    timeout = OscarWorker(timeout_config, runner=runner, process_factory=lambda *a, **k: RunningProcess(), clock=lambda: 0.0, sleep=lambda _: None).run(resolved(item))

    assert malformed.outcome == "blocked"
    assert "invalid output" in malformed.blocker.lower()
    assert timeout.outcome == "blocked"
    assert "timed out" in timeout.blocker.lower()
