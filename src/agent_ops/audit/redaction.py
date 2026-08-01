"""Redaction helpers for receipts and operator output."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


_HOME_RE = re.compile(r"(?i)(/Users/[^/\s]+|/home/[^/\s]+|\\\\Users\\\\[^\\\s]+)")
_TOKEN_RE = re.compile(
    r"(?i)\b(gh[opsru]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
)
_ABS_PATH_RE = re.compile(
    r"(?P<p>/(?:Users|home|var|tmp|private|opt|Volumes|Applications|etc|usr|root)/[^\s\"']+|"
    r"[A-Za-z]:\\(?:[^\\\s\"']+\\)*[^\\\s\"']+)"
)


def redact_text(text: str, extra_substrings: Optional[Iterable[str]] = None) -> str:
    out = text
    out = _TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _HOME_RE.sub("[REDACTED_HOME]", out)
    out = _ABS_PATH_RE.sub("[REDACTED_PATH]", out)
    if extra_substrings:
        for s in extra_substrings:
            if s and s in out:
                out = out.replace(s, "[REDACTED]")
    return out


def build_redaction_record(
    *,
    stripped_fields: Iterable[str],
    path_roots_redacted: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    return {
        "stripped_fields": sorted(set(stripped_fields)),
        "path_roots_redacted": sorted(set(path_roots_redacted or [])),
        "tokens_redacted": True,
        "raw_bodies_excluded": True,
    }


def assert_no_private_material(
    text: str, private_markers: Optional[Iterable[str]] = None
) -> List[str]:
    """Return list of privacy findings (empty if clean)."""
    findings: List[str] = []
    if _TOKEN_RE.search(text):
        findings.append("token_like_secret")
    if _HOME_RE.search(text):
        findings.append("home_directory_path")
    elif _ABS_PATH_RE.search(text):
        findings.append("absolute_machine_path")
    if private_markers:
        for m in private_markers:
            if m and m in text:
                findings.append(f"private_marker:{m}")
    return findings
