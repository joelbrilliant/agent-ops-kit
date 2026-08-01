"""Unit tests: contracts, paths, redaction, config, process safety."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_ops.audit.redaction import assert_no_private_material, redact_text
from agent_ops.config import ConfigError, expand_runner_argv, load_config
from agent_ops.contracts import DecisionV1, SignalV1, body_digest, signal_digest
from agent_ops.paths import filter_changed_paths, is_path_allowed, resolve_under
from agent_ops.process import run_argv


def test_body_and_signal_digest_stable():
    s = SignalV1(
        repository="o/r",
        pr_number=1,
        thread_node_id="t",
        latest_comment_node_id="c",
        trusted_author_login="rev",
        path="a.py",
        line=1,
        observed_head_sha="abc",
        body_digest=body_digest("hello"),
    )
    assert s.body_digest == body_digest("hello")
    assert signal_digest(s) == signal_digest(s)
    assert s.to_public_dict()["untrusted"] is True
    assert "_raw_body" not in s.to_public_dict()


def test_decision_from_dict_invalid_becomes_hold():
    d = DecisionV1.from_dict({"verdict": "YEET", "reason": "x"})
    assert d.verdict == "HOLD"


def test_path_allow_and_escape():
    assert is_path_allowed("src/a.py", ["src/*"])
    assert is_path_allowed("src/a.py", ["src/"])
    assert not is_path_allowed("secrets/x", ["src/*"])
    bad = filter_changed_paths(
        ["src/a.py", "secrets/k", ".github/workflows/ci.yml"],
        allowed=["src/*"],
        protected=[".github/workflows/*", "secrets/*"],
    )
    assert "secrets/k" in bad
    assert ".github/workflows/ci.yml" in bad
    assert "src/a.py" not in bad


def test_resolve_under_blocks_escape(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    ok = resolve_under(root, "a/b")
    assert str(ok).startswith(str(root.resolve()))
    with pytest.raises(ValueError):
        resolve_under(root, "../outside")


def test_redaction_strips_home_and_tokens():
    text = "see /Users/example/Projects/x and ghp_abcdefghijklmnopqrstuvwxyz012345"
    out = redact_text(text)
    assert "/Users/example" not in out
    assert "ghp_" not in out
    findings = assert_no_private_material(text)
    assert "token_like_secret" in findings
    assert "home_directory_path" in findings


def test_expand_runner_placeholders_only(tmp_path: Path):
    argv = expand_runner_argv(
        ["tool", "--in", "{request_path}", "--out", "{response_path}", "--wt", "{worktree_path}"],
        request_path=tmp_path / "r.json",
        response_path=tmp_path / "s.json",
        worktree_path=tmp_path / "wt",
    )
    assert str(tmp_path / "r.json") in argv
    assert "{request_path}" not in "".join(argv)


def test_load_config_roundtrip(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(
        json.dumps(
            {
                "operator_logins": ["joel"],
                "owned_namespaces": ["joelbrilliant"],
                "excluded_repositories": [],
                "trusted_reviewer_logins": ["copilot"],
                "workspace_root": str(tmp_path / "w"),
                "state_dir": str(tmp_path / "s"),
                "protected_path_patterns": [".env"],
                "classifier_command": ["echo", "{request_path}"],
                "builder_command": ["echo", "{response_path}"],
                "reviewer_command": ["echo", "{worktree_path}"],
                "notification_mode": "quiet",
                "default_verification_commands": {"unit": ["true"]},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.operator_logins == ["joel"]
    assert cfg.default_verification_commands["unit"] == ["true"]


def test_load_config_missing_required(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_run_argv_never_shell(tmp_path: Path):
    # Ensure shell metacharacters are literal args, not executed
    r = run_argv(["echo", "a; rm -rf /"], check=True)
    assert "a; rm -rf /" in r.stdout
    # shell=True would be a bug - we assert process module source
    src = Path(__file__).resolve().parents[1] / "src" / "agent_ops" / "process.py"
    text = src.read_text(encoding="utf-8")
    assert "shell=False" in text
    assert "shell=True" not in text.replace("never enable shell mode", "")
