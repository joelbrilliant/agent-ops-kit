"""Public, read-only audit report tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import ast
import time
from pathlib import Path

import pytest

from agent_ops.audit.report import build_audit_report, render_audit_report
from agent_ops.contracts import AuditReportV1
from agent_ops.cli import main
from agent_ops.exit_codes import HELD, OK, USAGE_OR_TOOLING

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
        "redaction_record": {},
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
        "redaction_record": {
            "stripped_fields": ["comment_body", "customer_material"],
            "path_roots_redacted": [],
            "tokens_redacted": True,
            "raw_bodies_excluded": True,
        },
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
    assert report["schema_version"] == 1
    assert [item["work_kind"] for item in report["items"]] == ["issue", "pull_request"]
    assert "agent-ops/local-only" not in first and "private local reason" not in first and "z.json" not in first


def test_audit_report_completed_receipts_require_public_result_evidence(tmp_path: Path):
    cfg = make_config(tmp_path)
    invalid = (
        _action(reply_node_id=None),
        _issue(outcome="completed", draft_pr_number=None, draft_pr_url=None),
        _issue(outcome="completed", issue_reply_node_id=None),
    )
    for payload in invalid:
        _write_receipt(cfg.state_dir, "receipt.json", payload)
        assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_accepts_early_held_action_receipt(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(
        cfg.state_dir,
        "early-held.json",
        _action(
            outcome="held",
            resulting_sha="",
            named_checks=[],
            reply_node_id=None,
            hold_reason="classification held",
            redaction_record={},
        ),
    )
    report = build_audit_report(cfg)
    assert report.verdict == "PASS"
    assert report.summary == {
        "total": 1,
        "completed": 0,
        "held": 1,
        "pull_requests": 1,
        "issues": 0,
    }
    assert report.items[0]["resulting_sha"] == ""
    assert report.items[0]["result_refs"] == []


def test_audit_report_parent_swap_cannot_redirect_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import agent_ops.audit.report as report_module

    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "receipt.json", _action())
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_receipt(outside, "receipt.json", _action(repository="attacker/redirected"))
    original_receipts = cfg.state_dir / "receipts"
    moved_receipts = cfg.state_dir / "receipts-original"
    real_open = os.open
    swapped = False

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if not swapped and os.fspath(path).endswith("receipt.json"):
            original_receipts.rename(moved_receipts)
            original_receipts.symlink_to(outside / "receipts", target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(report_module.os, "open", swapping_open)
    report = build_audit_report(cfg)
    assert swapped
    assert report.verdict == "HOLD"
    assert not report.items


def test_audit_report_state_swap_cannot_redirect_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import agent_ops.audit.report as report_module

    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "receipt.json", _action())
    outside = tmp_path / "outside-state"
    outside.mkdir()
    _write_receipt(outside, "receipt.json", _action(repository="attacker/redirected"))
    moved_state = tmp_path / "state-original"
    real_open = os.open
    swapped = False

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if not swapped and os.fspath(path) == "receipts" and kwargs.get("dir_fd") is not None:
            cfg.state_dir.rename(moved_state)
            cfg.state_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(report_module.os, "open", swapping_open)
    report = build_audit_report(cfg)
    assert swapped
    assert report.verdict == "HOLD"
    assert not report.items


def test_audit_report_file_swap_cannot_redirect_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import agent_ops.audit.report as report_module

    cfg = make_config(tmp_path)
    receipt = _write_receipt(cfg.state_dir, "receipt.json", _action())
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_action(repository="attacker/redirected")), encoding="utf-8")
    outside.chmod(0o600)
    real_open = os.open
    swapped = False

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if not swapped and os.fspath(path) == "receipt.json" and kwargs.get("dir_fd") is not None:
            receipt.unlink()
            receipt.symlink_to(outside)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(report_module.os, "open", swapping_open)
    report = build_audit_report(cfg)
    assert swapped
    assert report.verdict == "HOLD"
    assert not report.items


def test_audit_report_markdown_is_deterministic_and_escaped(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "receipt.json", _action())
    first = render_audit_report(build_audit_report(cfg), "markdown")
    assert first == render_audit_report(build_audit_report(cfg), "markdown")
    escaped = AuditReportV1("AuditReportV1", 1, "sha256:" + "a" * 64, "PASS", {"total": 0, "completed": 0, "held": 0, "pull_requests": 0, "issues": 0}, [{"receipt_schema": "X", "work_kind": "issue", "repository": "owner/demo", "number": 1, "base_sha": "a", "resulting_sha": "b", "signal_digest": "c", "outcome": "held", "named_checks": ["unit|safe"], "result_refs": []}], [], {"stripped_fields": [], "tokens_redacted": True, "raw_bodies_excluded": True})
    assert "unit\\|safe" in render_audit_report(escaped, "markdown")


def test_audit_report_json_and_markdown_have_same_safe_semantics(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "action.json", _action())
    _write_receipt(cfg.state_dir, "issue.json", _issue())
    report = build_audit_report(cfg)
    json_output = render_audit_report(report, "json")
    markdown = render_audit_report(report, "markdown")
    payload = json.loads(json_output)
    assert f"Verdict: {payload['verdict']}" in markdown
    assert f"Report ID: {payload['report_id']}" in markdown
    for value in payload["summary"].values():
        assert str(value) in markdown
    for item in payload["items"]:
        for field in (
            "receipt_schema",
            "work_kind",
            "repository",
            "base_sha",
            "resulting_sha",
            "signal_digest",
            "outcome",
        ):
            assert item[field] in markdown
        for value in item["named_checks"] + item["result_refs"]:
            assert value in markdown
    hold = build_audit_report(make_config(tmp_path / "empty"))
    hold_json = json.loads(render_audit_report(hold, "json"))
    hold_markdown = render_audit_report(hold, "markdown")
    assert hold_json["verdict"] == "HOLD"
    assert hold_json["findings"][0]["code"] in hold_markdown
    assert hold_json["findings"][0]["summary"] in hold_markdown


def test_audit_report_determinism_ignores_names_times_locale_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    first_cfg = make_config(tmp_path / "first")
    second_cfg = make_config(tmp_path / "second")
    first_paths = (
        _write_receipt(first_cfg.state_dir, "z-last.json", _action()),
        _write_receipt(first_cfg.state_dir, "a-first.json", _issue()),
    )
    second_paths = (
        _write_receipt(second_cfg.state_dir, "other-one.json", _issue()),
        _write_receipt(second_cfg.state_dir, "other-two.json", _action()),
    )
    now = time.time_ns()
    os.utime(first_paths[0], ns=(now - 5_000_000_000, now - 5_000_000_000))
    os.utime(first_paths[1], ns=(now - 1_000_000_000, now - 1_000_000_000))
    os.utime(second_paths[0], ns=(now - 9_000_000_000, now - 9_000_000_000))
    os.utime(second_paths[1], ns=(now - 3_000_000_000, now - 3_000_000_000))
    monkeypatch.setenv("LANG", "zz_ZZ.invalid")
    monkeypatch.setenv("LC_ALL", "zz_ZZ.invalid")
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    monkeypatch.setenv("AGENT_OPS_UNRELATED", "different")
    first_json = render_audit_report(build_audit_report(first_cfg), "json")
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("AGENT_OPS_UNRELATED", "changed")
    second_json = render_audit_report(build_audit_report(second_cfg), "json")
    assert first_json == second_json
    assert render_audit_report(build_audit_report(first_cfg), "markdown") == render_audit_report(
        build_audit_report(second_cfg), "markdown"
    )


def test_audit_report_rejects_unknown_schema_and_fields(tmp_path: Path):
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "bad.json", _action(schema="UnknownV1"))
    assert build_audit_report(cfg).verdict == "HOLD"
    _write_receipt(cfg.state_dir, "bad.json", _action(extra="nope"))
    assert build_audit_report(cfg).verdict == "HOLD"
    missing = _action()
    del missing["base_sha"]
    _write_receipt(cfg.state_dir, "bad.json", missing)
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_duplicate_keys_and_invalid_nested_values(tmp_path: Path):
    cfg = make_config(tmp_path)
    path = _write_receipt(cfg.state_dir, "bad.json", _action())
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"schema":', '"schema":"ActionReceiptV1","schema":', 1), encoding="utf-8")
    assert build_audit_report(cfg).verdict == "HOLD"
    for record in (
        {"tokens_redacted": 1},
        {
            "stripped_fields": [{}],
            "path_roots_redacted": [],
            "tokens_redacted": True,
            "raw_bodies_excluded": True,
        },
        {
            "stripped_fields": [],
            "path_roots_redacted": [[]],
            "tokens_redacted": True,
            "raw_bodies_excluded": True,
        },
        {
            "stripped_fields": ["comment_body"],
            "path_roots_redacted": [],
            "tokens_redacted": True,
            "raw_bodies_excluded": False,
        },
        {
            "stripped_fields": ["comment_body"],
            "path_roots_redacted": [],
            "tokens_redacted": True,
            "raw_bodies_excluded": True,
            "prompt": "harmless",
        },
    ):
        _write_receipt(cfg.state_dir, "bad.json", _action(redaction_record=record))
        assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_malformed_and_oversized_receipt(tmp_path: Path):
    cfg = make_config(tmp_path)
    path = _write_receipt(cfg.state_dir, "bad.json", _action())
    path.write_text("{", encoding="utf-8")
    assert build_audit_report(cfg).verdict == "HOLD"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    assert build_audit_report(cfg).verdict == "HOLD"
    path.write_bytes(b"{\"schema\":\"\xff\"}")
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_file_crossing_size_limit_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import agent_ops.audit.report as report_module

    cfg = make_config(tmp_path)
    path = _write_receipt(cfg.state_dir, "receipt.json", _action())
    real_read = os.read
    expanded = False

    def growing_read(descriptor: int, size: int) -> bytes:
        nonlocal expanded
        chunk = real_read(descriptor, size)
        if not expanded:
            with path.open("ab") as handle:
                handle.write(b" " * (1024 * 1024 + 1))
            expanded = True
        return chunk

    monkeypatch.setattr(report_module.os, "read", growing_read)
    assert build_audit_report(cfg).verdict == "HOLD"
    assert expanded


def test_audit_report_rejects_bad_types_digests_shas_and_outcomes(tmp_path: Path):
    cfg = make_config(tmp_path)
    for payload in (
        _action(pr_number="9"),
        _action(pr_number=True),
        _action(signal_digest="bad"),
        _action(signal_digest="a" * 63),
        _action(base_sha="bad"),
        _action(base_sha="a" * 39),
        _action(named_checks=[{}]),
        _action(outcome="PASS"),
        _issue(draft_pr_number=True),
        _issue(draft_pr_number=12, draft_pr_url=None),
    ):
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
    report = build_audit_report(cfg)
    assert report.verdict == "HOLD"
    output = render_audit_report(report, "json", cfg.private_markers)
    assert "PRIVATE_SENTINEL" not in output
    assert "bad.json" not in output
    _write_receipt(cfg.state_dir, "bad.json", _action(raw_body="harmless"))
    assert build_audit_report(cfg).verdict == "HOLD"
    _write_receipt(cfg.state_dir, "bad.json", _action(hold_reason="/srv/private/receipt"))
    assert build_audit_report(cfg).verdict == "HOLD"


def test_audit_report_rejects_unbounded_exported_strings(tmp_path: Path):
    cfg = make_config(tmp_path)
    for payload in (
        _action(named_checks=["ordinary arbitrary text"]),
        _action(reply_node_id="ordinary arbitrary text"),
        _action(thread_node_id="ordinary arbitrary text"),
        _action(reply_node_id="R_9\nsecond-line"),
        _action(reply_node_id="R_9|table"),
        _action(repository="owner/" + "x" * 250),
        _action(repository="owner/demo extra"),
        _issue(issue_reply_node_id="ordinary arbitrary text"),
        _issue(draft_pr_url="https://github.com/elsewhere/demo/pull/12"),
        _issue(draft_pr_url="https://github.com/owner/demo/pull/12?redirect=1"),
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
    cfg = make_config(tmp_path / "non-regular")
    receipts = cfg.state_dir / "receipts"
    receipts.mkdir()
    (receipts / "nested").mkdir()
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
    content = path.read_bytes()
    receipts_dir = cfg.state_dir / "receipts"
    topology_before = sorted(
        (p.relative_to(cfg.state_dir).as_posix(), p.lstat().st_mode)
        for p in cfg.state_dir.rglob("*")
    )
    before_stat = path.stat()
    before_state_stat = cfg.state_dir.stat()
    before_receipts_stat = receipts_dir.stat()
    before = (
        content,
        stat.S_IMODE(before_stat.st_mode),
        before_stat.st_size,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
        (before_state_stat.st_mode, before_state_stat.st_mtime_ns, before_state_stat.st_ctime_ns),
        (before_receipts_stat.st_mode, before_receipts_stat.st_mtime_ns, before_receipts_stat.st_ctime_ns),
        topology_before,
    )
    assert build_audit_report(cfg).verdict == "PASS"
    after_stat = path.stat()
    after_state_stat = cfg.state_dir.stat()
    after_receipts_stat = receipts_dir.stat()
    after = (
        content,
        stat.S_IMODE(after_stat.st_mode),
        after_stat.st_size,
        after_stat.st_mtime_ns,
        after_stat.st_ctime_ns,
        (after_state_stat.st_mode, after_state_stat.st_mtime_ns, after_state_stat.st_ctime_ns),
        (after_receipts_stat.st_mode, after_receipts_stat.st_mtime_ns, after_receipts_stat.st_ctime_ns),
        sorted((p.relative_to(cfg.state_dir).as_posix(), p.lstat().st_mode) for p in cfg.state_dir.rglob("*")),
    )
    assert after == before
    assert after_stat.st_atime_ns >= before_stat.st_atime_ns
    assert after_state_stat.st_atime_ns >= before_state_stat.st_atime_ns
    assert after_receipts_stat.st_atime_ns >= before_receipts_stat.st_atime_ns


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


def test_audit_report_invalid_config_does_not_echo_local_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    missing = tmp_path / "private-machine-name.json"
    assert main(["audit", "report", "--config", str(missing)]) == USAGE_OR_TOOLING
    captured = capsys.readouterr()
    assert not captured.out
    assert str(missing) not in captured.err


def test_audit_report_production_code_has_no_network_or_subprocess_boundary_imports():
    root = Path(__file__).parents[1] / "src" / "agent_ops"
    forbidden = {"subprocess", "socket", "http", "urllib", "requests", "github"}
    for relative in (Path("audit/report.py"), Path("cli.py")):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                if relative == Path("cli.py") and node.module.startswith("agent_ops"):
                    imported.add(node.module)
        assert not imported.intersection(forbidden)
        if relative == Path("cli.py"):
            assert not any(
                module.startswith(("agent_ops.github", "agent_ops.maintenance", "agent_ops.process", "agent_ops.runners"))
                for module in imported
            )


def test_audit_report_public_example_matches_renderer(tmp_path: Path):
    example = Path(__file__).parents[1] / "examples" / "audit-report-example.json"
    assert example.is_file()
    cfg = make_config(tmp_path)
    _write_receipt(cfg.state_dir, "example-action.json", _action())
    _write_receipt(cfg.state_dir, "example-issue.json", _issue())
    assert example.read_text(encoding="utf-8") == render_audit_report(build_audit_report(cfg), "json")
