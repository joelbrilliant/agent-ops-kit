"""Public, read-only audit report tests."""

from __future__ import annotations

import hashlib
import json
import stat
import ast
from pathlib import Path

import pytest

from agent_ops.audit.report import build_audit_report, render_audit_report
from agent_ops.contracts import AuditReportV1
from agent_ops.cli import main
from agent_ops.exit_codes import HELD, OK

from conftest import make_config


SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = hashlib.sha256(b"a").hexdigest()
DIGEST_B = hashlib.sha256(b"b").hexdigest()


def _action(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "ActionReceiptV1", "signal_digest": DIGEST_B,
        "repository": "owner/demo", "pr_number": 9, "thread_node_id": "T_9",
        "base_sha": SHA_A, "resulting_sha": SHA_B, "named_checks": ["unit", "package"],
        "reply_node_id": "R_9", "outcome": "completed", "hold_reason": None,
        "redaction_record": {"tokens_redacted": True, "raw_bodies_excluded": True},
    }
    result.update(changes)
    return result


def _issue(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "IssueDraftReceiptV1", "signal_digest": DIGEST_A,
        "repository": "owner/demo", "issue_number": 3, "base_sha": SHA_A,
        "resulting_sha": SHA_B, "branch_name": "agent-ops/local-only", "draft_pr_number": 12,
        "draft_pr_url": "https://github.com/owner/demo/pull/12", "issue_reply_node_id": "I_3",
        "named_checks": ["unit"], "outcome": "held", "hold_reason": "private local reason",
        "redaction_record": {"tokens_redacted": True, "raw_bodies_excluded": True},
    }
    result.update(changes)
    return result


def _write_receipt(state: Path, name: str, payload: dict[str, object]) -> Path:
    receipts = state / "receipts"
    receipts.mkdir(exist_ok=True)
    path = receipts / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _report(tmp_path: Path):
    cfg = make_config(tmp_path)
    return build_audit_report(cfg)


def test_audit_report_mixed_receipts_is_deterministic(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "z.json", _action())
    _write_receipt(cfg.state_dir, "a.json", _issue())
    first = render_audit_report(build_audit_report(cfg), "json")
    second = render_audit_report(build_audit_report(cfg), "json")
    assert first == second
    report = json.loads(first)
    assert report["verdict"] == "PASS"
    assert report["summary"] == {"total": 2, "completed": 1, "held": 1, "pull_requests": 1, "issues": 1}
    assert [item["work_kind"] for item in report["items"]] == ["issue", "pull_request"]
    assert "agent-ops/local-only" not in first and "private local reason" not in first and "z.json" not in first


def test_audit_report_markdown_is_deterministic_and_escaped(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "receipt.json", _action())
    first = render_audit_report(build_audit_report(cfg), "markdown")
    assert first == render_audit_report(build_audit_report(cfg), "markdown")
    escaped = AuditReportV1("AuditReportV1", "1", "sha256:" + "a" * 64, "PASS", {"total": 0, "completed": 0, "held": 0, "pull_requests": 0, "issues": 0}, [{"receipt_schema": "X", "work_kind": "issue", "repository": "owner/demo", "number": 1, "base_sha": "a", "resulting_sha": "b", "signal_digest": "c", "outcome": "held", "named_checks": ["unit|safe"], "result_refs": []}], [], {"stripped_fields": [], "tokens_redacted": True, "raw_bodies_excluded": True})
    assert "unit\\|safe" in render_audit_report(escaped, "markdown")


def test_audit_report_rejects_unknown_schema_and_fields(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "bad.json", _action(schema="UnknownV1"))
    assert build_audit_report(cfg).verdict == "HOLD"
    _write_receipt(cfg.state_dir, "bad.json", _action(extra="nope"))
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_malformed_and_oversized_receipt(tmp_path: Path):
    cfg = make_config(tmp_path)
    path = _write_receipt(cfg.state_dir, "bad.json", _action())
    path.write_text("{", encoding="utf-8")
    assert build_audit_report(cfg).verdict == "HOLD"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_bad_types_digests_shas_and_outcomes(tmp_path: Path):
    cfg = make_config(tmp_path)
    for payload in (_action(pr_number="9"), _action(signal_digest="bad"), _action(base_sha="bad"), _action(outcome="PASS")):
        _write_receipt(cfg.state_dir, "bad.json", payload)
        assert build_audit_report(cfg).verdict == "HOLD"
    _write_receipt(cfg.state_dir, "bad.json", _issue(resulting_sha=""))
    assert build_audit_report(cfg).verdict == "PASS"


def test_audit_report_rejects_duplicate_logical_receipt(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "one.json", _action())
    _write_receipt(cfg.state_dir, "two.json", _action())
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_private_material_and_raw_content_fields(tmp_path: Path):
    cfg = make_config(tmp_path)
    cfg.private_markers.append("PRIVATE_SENTINEL")
    _write_receipt(cfg.state_dir, "bad.json", _action(repository="PRIVATE_SENTINEL"))
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_unbounded_exported_strings(tmp_path: Path):
    cfg = make_config(tmp_path)
    for payload in (
        _action(named_checks=["ordinary arbitrary text"]),
        _action(reply_node_id="ordinary arbitrary text"),
        _action(thread_node_id="ordinary arbitrary text"),
        _issue(issue_reply_node_id="ordinary arbitrary text"),
        _issue(draft_pr_url="https://github.com/elsewhere/demo/pull/12"),
    ):
        _write_receipt(cfg.state_dir, "bad.json", payload)
        assert build_audit_report(cfg).verdict == "HOLD"
    _write_receipt(cfg.state_dir, "bad.json", _action(raw_body="harmless"))
    assert build_audit_report(cfg).verdict == "HOLD"
    _write_receipt(cfg.state_dir, "bad.json", _issue(branch_name="ghp_abcdefghijklmnopqrstuvwx"))
    assert build_audit_report(cfg).verdict == "HOLD"
    _write_receipt(cfg.state_dir, "bad.json", _issue(branch_name="/Users/local-machine"))
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_symlink_and_unsafe_permissions(tmp_path: Path):
    cfg = make_config(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (cfg.state_dir / "receipts").mkdir()
    (cfg.state_dir / "receipts").rmdir()
    (cfg.state_dir / "receipts").symlink_to(target, target_is_directory=True)
    assert build_audit_report(cfg).verdict == "HOLD"
    (cfg.state_dir / "receipts").unlink()
    _write_receipt(cfg.state_dir, "bad.json", _action())
    (cfg.state_dir / "receipts" / "bad.json").chmod(0o640)
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_state_and_file_symlinks(tmp_path: Path):
    cfg = make_config(tmp_path)
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    cfg.state_dir.rmdir()
    cfg.state_dir.symlink_to(real_state, target_is_directory=True)
    assert build_audit_report(cfg).verdict == "HOLD"
    cfg = make_config(tmp_path / "file")
    target = _write_receipt(cfg.state_dir, "target.json", _action())
    (cfg.state_dir / "receipts" / "link.json").symlink_to(target)
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_more_than_maximum_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "receipt.json", _action())
    import agent_ops.audit.report as report_module
    monkeypatch.setattr(report_module, "_MAX_RECEIPTS", 0)
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_is_read_only(tmp_path: Path):
    cfg = make_config(tmp_path)
    path = _write_receipt(cfg.state_dir, "receipt.json", _action())
    before = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), sorted(p.name for p in cfg.state_dir.rglob("*")))
    assert build_audit_report(cfg).verdict == "PASS"
    after = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), sorted(p.name for p in cfg.state_dir.rglob("*")))
    assert after == before


def test_audit_report_empty_directory_holds(tmp_path: Path):
    assert _report(tmp_path).verdict == "HOLD"


def test_audit_report_cli_needs_no_git_gh_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "receipt.json", _action())
    config_path = tmp_path / "config.json"
    config = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))
    config["workspace_root"] = str(cfg.workspace_root)
    config["state_dir"] = str(cfg.state_dir)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert main(["audit", "report", "--config", str(config_path), "--format", "json"]) == OK
    assert json.loads(capsys.readouterr().out)["verdict"] == "PASS"


def test_audit_report_production_code_has_no_network_or_subprocess_boundary_imports():
    source = Path(__file__).parents[1] / "src" / "agent_ops" / "audit" / "report.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported.intersection({"subprocess", "socket", "http", "urllib", "requests", "github"})


def test_audit_report_public_example_matches_renderer(tmp_path: Path):
    example = Path(__file__).parents[1] / "examples" / "audit-report-example.json"
    assert example.is_file()
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "example-action.json", _action())
    _write_receipt(cfg.state_dir, "example-issue.json", _issue())
    assert example.read_text(encoding="utf-8") == render_audit_report(build_audit_report(cfg), "json")
