from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

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

    def wait(self, timeout=None):
        return 0


class RunningProcess:
    pid = 43211

    def poll(self):
        return None

    def communicate(self):
        return "", ""

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("oscar", timeout)


def git_runner(argv, **kwargs):
    stdout = "head-1\n" if "rev-parse" in argv else ""
    return subprocess.CompletedProcess(argv, 0, stdout, "")


def test_worker_uses_fixed_headless_argv_and_blocks_observed_gui(config, monkeypatch, tmp_path):
    item = note()
    commands = []
    launches = []
    killed = []
    fake_gui = tmp_path / "Harmless.app" / "Contents" / "MacOS" / "fake-gui"

    def runner(argv, **kwargs):
        commands.append(argv)
        return git_runner(argv, **kwargs)

    def process_factory(argv, **kwargs):
        launches.append((argv, kwargs))
        return RunningProcess()

    def killpg(pid, stop_signal):
        killed.append((pid, stop_signal))
        if stop_signal == 0:
            raise ProcessLookupError

    monkeypatch.setattr("github_watch.worker.os.killpg", killpg)
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
    assert commands[1][3:6] == ["fetch", "--quiet", "origin"]
    assert commands[2][3:] == ["rev-parse", "FETCH_HEAD"]
    assert commands[3][3:] == ["checkout", "--detach", "head-1"]
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
    assert killed[0] == (RunningProcess.pid, signal.SIGTERM)
    assert OscarWorker._is_gui_command("/usr/bin/open harmless-path") is True


def test_worker_accepts_only_one_valid_json_object(config):
    item = note()

    def process_factory(argv, **kwargs):
        return FinishedProcess(json.dumps({"outcome": "completed", "summary": "fixed", "head_sha": "new-head", "comment_kind": "issue", "comment_id": 4}))

    result = OscarWorker(config, runner=git_runner, process_factory=process_factory).run(resolved(item))
    assert result.outcome == "completed"
    assert result.comment_id == 4


def test_worker_accepts_minimal_no_action_result(config):
    item = note()

    result = OscarWorker(
        config,
        runner=git_runner,
        process_factory=lambda *a, **k: FinishedProcess('{"outcome":"no_action"}'),
    ).run(resolved(item))

    assert result.outcome == "no_action"
    assert result.summary == "No action needed"


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
        captured.append((argv, kwargs))
        return git_runner(argv, **kwargs)

    def process_factory(argv, **kwargs):
        captured.append((argv, kwargs))
        assert Path(kwargs["env"]["GH_CONFIG_DIR"]).is_dir()
        return FinishedProcess(json.dumps({"outcome": "no_action", "summary": "nothing remains"}))

    result = OscarWorker(config, runner=runner, process_factory=process_factory).run(
        resolved(item, mutation_allowed=False, latest_comment_body="A harmless latest comment.")
    )

    clone_calls = [call for call in captured if call[0][0] == "git"]
    argv, kwargs = captured[-1]
    environment = kwargs["env"]
    assert result.outcome == "no_action"
    assert all(key not in environment for key in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"))
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_0"] == ""
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"
    assert environment["HOME"] != os.environ["HOME"]
    assert clone_calls[0][0][:3] == ["git", "clone", "--quiet"]
    assert clone_calls[0][1]["env"]["GH_TOKEN"] == "must-not-reach-oscar"
    assert clone_calls[1][1]["env"]["GH_TOKEN"] == "must-not-reach-oscar"
    assert all("must-not-reach-oscar" not in call[1]["env"].values() for call in clone_calls[2:])
    assert '"mutation_allowed": false' in argv[4]
    assert "This is read-only triage." in argv[4]
    assert "A harmless latest comment." in argv[4]


def test_worker_converts_malformed_output_and_timeout_to_blocked(config):
    item = note()

    malformed = OscarWorker(config, runner=git_runner, process_factory=lambda *a, **k: FinishedProcess("not json")).run(resolved(item))
    timeout_config = config.__class__(**{**config.__dict__, "oscar_timeout_seconds": 0})
    timeout = OscarWorker(timeout_config, runner=git_runner, process_factory=lambda *a, **k: RunningProcess(), clock=lambda: 0.0, sleep=lambda _: None).run(resolved(item))

    assert malformed.outcome == "blocked"
    assert "invalid output" in malformed.blocker.lower()
    assert timeout.outcome == "blocked"
    assert "timed out" in timeout.blocker.lower()


def test_real_timeout_terminates_the_exact_process_group(config, tmp_path):
    executable = tmp_path / "harmless-oscar"
    executable.write_text("#!/bin/sh\ntrap '' TERM\nwhile :; do /bin/sleep 1; done\n", encoding="utf-8")
    executable.chmod(0o755)
    timeout_config = config.__class__(
        **{**config.__dict__, "oscar_command": str(executable), "oscar_timeout_seconds": 1}
    )
    launched = []

    def factory(argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        launched.append(process)
        return process

    worker = OscarWorker(timeout_config, process_factory=factory, gui_scan=lambda _: None)
    environment = worker._environment(tmp_path, True)
    result = worker._launch(tmp_path, resolved(note()), "harmless timeout probe", environment)

    assert result.outcome == "blocked"
    assert "timed out" in result.blocker.lower()
    assert launched[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(launched[0].pid, 0)
