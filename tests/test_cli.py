"""CLI behaviour tests."""

from __future__ import annotations

import json
from pathlib import Path

from agent_ops.cli import main
from agent_ops.exit_codes import OK, USAGE_OR_TOOLING


def _cfg(tmp_path: Path) -> Path:
    p = tmp_path / "cfg.json"
    p.write_text(
        json.dumps(
            {
                "operator_logins": ["operator"],
                "owned_namespaces": ["operator"],
                "excluded_repositories": [],
                "trusted_reviewer_logins": ["reviewer"],
                "workspace_root": str(tmp_path / "w"),
                "state_dir": str(tmp_path / "s"),
                "protected_path_patterns": [],
                "classifier_command": ["true"],
                "builder_command": ["true"],
                "reviewer_command": ["true"],
                "notification_mode": "quiet",
                "default_verification_commands": {"unit": ["true"]},
            }
        ),
        encoding="utf-8",
    )
    return p


def test_cli_missing_config(tmp_path: Path):
    code = main(["pr", "status", "--config", str(tmp_path / "missing.json")])
    assert code == USAGE_OR_TOOLING


def test_cli_status_and_pause(tmp_path: Path, capsys):
    cfg = _cfg(tmp_path)
    assert main(["pr", "status", "--config", str(cfg)]) == OK
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "circuit_open" in data
    assert main(["pr", "pause", "--config", str(cfg)]) == OK
    assert main(["pr", "resume", "--config", str(cfg)]) == OK
    assert main(["pr", "clear-circuit", "--config", str(cfg)]) == OK
