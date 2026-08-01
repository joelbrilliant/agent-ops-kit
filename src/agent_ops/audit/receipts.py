"""Receipt persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from agent_ops.audit.redaction import (
    assert_no_private_material,
    build_redaction_record,
    redact_text,
)
from agent_ops.contracts import ActionReceiptV1, dumps_json


def write_receipt(
    state_dir: Path,
    receipt: ActionReceiptV1,
    private_markers: Optional[Iterable[str]] = None,
) -> Path:
    receipts_dir = state_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    name = f"{receipt.signal_digest[:16]}-{receipt.outcome}.json"
    path = receipts_dir / name
    payload = receipt.to_dict()
    # Final pass: redact any accidental absolute paths in string fields.
    text = dumps_json(payload)
    text = redact_text(text, private_markers)
    findings = assert_no_private_material(text, private_markers)
    if findings:
        raise ValueError("receipt privacy validation failed:" + ",".join(findings))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    return path


def latest_receipt(state_dir: Path) -> Optional[Dict[str, Any]]:
    receipts_dir = state_dir / "receipts"
    if not receipts_dir.is_dir():
        return None
    files = sorted(receipts_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def default_redaction_record() -> Dict[str, Any]:
    return build_redaction_record(
        stripped_fields=[
            "comment_body",
            "transcript",
            "credentials",
            "private_machine_paths",
            "customer_material",
        ]
    )
