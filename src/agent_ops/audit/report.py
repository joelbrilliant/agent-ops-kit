"""Read-only, public-safe reports over local redacted receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from agent_ops.audit.redaction import assert_no_private_material, build_redaction_record
from agent_ops.config import Config
from agent_ops.contracts import AuditReportV1


_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_RECEIPTS = 10_000
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RAW_FIELD_TERMS = ("body", "transcript", "prompt", "diff", "log")
_ACTION_FIELDS = frozenset({
    "schema", "signal_digest", "repository", "pr_number", "thread_node_id", "base_sha",
    "resulting_sha", "named_checks", "reply_node_id", "outcome", "hold_reason", "redaction_record",
})
_ISSUE_FIELDS = frozenset({
    "schema", "signal_digest", "repository", "issue_number", "base_sha", "resulting_sha",
    "branch_name", "draft_pr_number", "draft_pr_url", "issue_reply_node_id", "named_checks",
    "outcome", "hold_reason", "redaction_record",
})


def _redaction_record() -> Dict[str, Any]:
    return build_redaction_record(stripped_fields=[
        "branch_name", "hold_reason", "receipt_filename", "local_path", "raw_body",
        "transcript", "prompt", "diff", "log", "credentials",
    ])


def _report_id(items: Sequence[Dict[str, Any]]) -> str:
    canonical = json.dumps(list(items), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hold(code: str) -> AuditReportV1:
    items: List[Dict[str, Any]] = []
    return AuditReportV1(
        schema="AuditReportV1", schema_version="1", report_id=_report_id(items), verdict="HOLD",
        summary={"total": 0, "completed": 0, "held": 0, "pull_requests": 0, "issues": 0},
        items=items, findings=[{"code": code, "summary": "Receipt validation failed."}],
        redaction_record=_redaction_record(),
    )


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_optional_string(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _valid_common(data: Mapping[str, Any], number_name: str) -> bool:
    outcome = data.get("outcome")
    resulting_sha = data.get("resulting_sha")
    valid_resulting_sha = (
        outcome == "completed" and isinstance(resulting_sha, str) and bool(_SHA_RE.fullmatch(resulting_sha))
    ) or (
        outcome == "held" and isinstance(resulting_sha, str)
        and (resulting_sha == "" or bool(_SHA_RE.fullmatch(resulting_sha)))
    )
    return (
        isinstance(data.get("signal_digest"), str) and bool(_DIGEST_RE.fullmatch(data["signal_digest"]))
        and isinstance(data.get("repository"), str) and bool(_REPOSITORY_RE.fullmatch(data["repository"]))
        and isinstance(data.get(number_name), int) and not isinstance(data[number_name], bool) and data[number_name] > 0
        and isinstance(data.get("base_sha"), str) and bool(_SHA_RE.fullmatch(data["base_sha"]))
        and valid_resulting_sha
        and isinstance(data.get("named_checks"), list) and all(isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value)) for value in data["named_checks"])
        and isinstance(data.get("outcome"), str) and data["outcome"] in {"completed", "held"}
        and _is_optional_string(data.get("hold_reason")) and isinstance(data.get("redaction_record"), dict)
    )


def _validated_item(data: Any) -> Dict[str, Any] | None:
    if not isinstance(data, dict) or not isinstance(data.get("schema"), str):
        return None
    if any(any(term in field.lower() for term in _RAW_FIELD_TERMS) for field in data):
        return None
    schema = data["schema"]
    if schema == "ActionReceiptV1":
        if set(data) != _ACTION_FIELDS or not _valid_common(data, "pr_number"):
            return None
        if (not isinstance(data["thread_node_id"], str) or not _IDENTIFIER_RE.fullmatch(data["thread_node_id"])
                or (data["reply_node_id"] is not None and (not isinstance(data["reply_node_id"], str) or not _IDENTIFIER_RE.fullmatch(data["reply_node_id"])) )):
            return None
        return {
            "receipt_schema": schema, "signal_digest": data["signal_digest"], "repository": data["repository"],
            "work_kind": "pull_request", "number": data["pr_number"], "base_sha": data["base_sha"],
            "resulting_sha": data["resulting_sha"], "named_checks": sorted(data["named_checks"]),
            "outcome": data["outcome"], "result_refs": ([data["reply_node_id"]] if data["reply_node_id"] else []),
        }
    if schema == "IssueDraftReceiptV1":
        if set(data) != _ISSUE_FIELDS or not _valid_common(data, "issue_number"):
            return None
        if not isinstance(data["branch_name"], str) or not _is_optional_string(data["draft_pr_url"]):
            return None
        if data["draft_pr_number"] is not None and (not isinstance(data["draft_pr_number"], int) or isinstance(data["draft_pr_number"], bool) or data["draft_pr_number"] < 1):
            return None
        if data["issue_reply_node_id"] is not None and (not isinstance(data["issue_reply_node_id"], str) or not _IDENTIFIER_RE.fullmatch(data["issue_reply_node_id"])):
            return None
        if data["draft_pr_url"] is not None:
            expected_url = f"https://github.com/{data['repository']}/pull/{data['draft_pr_number']}"
            if data["draft_pr_number"] is None or data["draft_pr_url"] != expected_url:
                return None
        refs = [value for value in (data["draft_pr_url"], data["issue_reply_node_id"]) if value]
        return {
            "receipt_schema": schema, "signal_digest": data["signal_digest"], "repository": data["repository"],
            "work_kind": "issue", "number": data["issue_number"], "base_sha": data["base_sha"],
            "resulting_sha": data["resulting_sha"], "named_checks": sorted(data["named_checks"]),
            "outcome": data["outcome"], "result_refs": refs,
        }
    return None


def _receipt_files(state_dir: Path) -> Tuple[List[Path], str | None]:
    receipts_dir = state_dir / "receipts"
    if not _is_real_directory(state_dir) or not _is_real_directory(receipts_dir):
        return [], "unsafe_filesystem"
    try:
        entries = list(os.scandir(receipts_dir))
    except OSError:
        return [], "unsafe_filesystem"
    if not entries:
        return [], "empty_receipts"
    if len(entries) > _MAX_RECEIPTS:
        return [], "receipt_limit_exceeded"
    files: List[Path] = []
    for entry in entries:
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError:
            return [], "unsafe_filesystem"
        if entry.is_symlink() or not stat.S_ISREG(mode) or mode & (stat.S_IRWXG | stat.S_IRWXO):
            return [], "unsafe_filesystem"
        files.append(Path(entry.path))
    return sorted(files, key=lambda item: item.name), None


def _read_receipt(path: Path) -> str | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
                or metadata.st_size > _MAX_RECEIPT_BYTES):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_RECEIPT_BYTES + 1)
        if len(raw) > _MAX_RECEIPT_BYTES:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(descriptor)


def build_audit_report(config: Config) -> AuditReportV1:
    """Validate local receipts and return a deterministic portable report."""
    files, failure = _receipt_files(config.state_dir)
    if failure:
        return _hold(failure)
    items: List[Dict[str, Any]] = []
    for path in files:
        text = _read_receipt(path)
        if text is None:
            return _hold("receipt_validation_failed")
        try:
            if assert_no_private_material(text, config.private_markers):
                return _hold("privacy_validation_failed")
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return _hold("receipt_validation_failed")
        item = _validated_item(parsed)
        if item is None:
            return _hold("receipt_validation_failed")
        items.append(item)
    items.sort(key=lambda item: (item["work_kind"], item["repository"], item["number"], item["signal_digest"], item["outcome"], item["resulting_sha"]))
    identities = [(item["work_kind"], item["repository"], item["number"], item["signal_digest"]) for item in items]
    if len(set(identities)) != len(identities):
        return _hold("duplicate_receipt")
    summary = {
        "total": len(items), "completed": sum(item["outcome"] == "completed" for item in items),
        "held": sum(item["outcome"] == "held" for item in items),
        "pull_requests": sum(item["work_kind"] == "pull_request" for item in items),
        "issues": sum(item["work_kind"] == "issue" for item in items),
    }
    return AuditReportV1("AuditReportV1", "1", _report_id(items), "PASS", summary, items, [], _redaction_record())


def _markdown_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_audit_report(
    report: AuditReportV1, output_format: str, private_markers: Iterable[str] | None = None
) -> str:
    if output_format == "json":
        output = json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    elif output_format == "markdown":
        lines = [
            "# Agent Ops audit report", "", f"Schema: {report.schema} v{report.schema_version}",
            f"Verdict: {report.verdict}", f"Report ID: {report.report_id}", "", "## Summary", "",
            "| Total | Completed | Held | Pull requests | Issues |", "| ---: | ---: | ---: | ---: | ---: |",
            f"| {report.summary['total']} | {report.summary['completed']} | {report.summary['held']} | {report.summary['pull_requests']} | {report.summary['issues']} |",
            "", "## Items", "", "| Receipt schema | Kind | Repository | Number | Base SHA | Resulting SHA | Signal digest | Outcome | Checks | References |",
            "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {_markdown_escape(item['receipt_schema'])} | {_markdown_escape(item['work_kind'])} | {_markdown_escape(item['repository'])} | {item['number']} | {_markdown_escape(item['base_sha'])} | {_markdown_escape(item['resulting_sha'])} | {_markdown_escape(item['signal_digest'])} | {item['outcome']} | {_markdown_escape(', '.join(item['named_checks']))} | {_markdown_escape(', '.join(item['result_refs']))} |"
            for item in report.items
        )
        lines.extend(["", "## Findings", ""])
        lines.extend(f"- `{_markdown_escape(finding['code'])}`: {_markdown_escape(finding['summary'])}" for finding in report.findings)
        if not report.findings:
            lines.append("- None")
        lines.extend(["", "## Redaction record", "", "| Excluded fields | Token redaction | Raw bodies excluded |", "| --- | --- | --- |", f"| {_markdown_escape(', '.join(report.redaction_record['stripped_fields']))} | {str(report.redaction_record['tokens_redacted']).lower()} | {str(report.redaction_record['raw_bodies_excluded']).lower()} |"])
        output = "\n".join(lines) + "\n"
    else:
        raise ValueError("unsupported report format")
    if assert_no_private_material(output, private_markers):
        raise ValueError("rendered report privacy validation failed")
    return output
