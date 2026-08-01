"""Bounded parsing and trusted-command validation for QaGateSpecV1."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple, Union


MAX_SPEC_BYTES = 1024 * 1024
MAX_SPEC_DEPTH = 16
MAX_CRITERIA = 256
MAX_CHECKS_PER_CRITERION = 64
MAX_CHECK_REFERENCES = 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,79}$")
_WRAPPERS = frozenset(
    {
        "sh", "bash", "zsh", "dash", "fish", "cmd", "command", "powershell", "pwsh", "env",
        "xargs", "sudo", "nohup", "timeout", "nice", "setsid", "busybox", "toybox",
    }
)
_CAPABILITY_EXECUTABLES = frozenset(
    {"gh", "hub", "curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "netcat", "claude", "codex", "hermes"}
)


class QaGateUsageError(ValueError):
    """The invocation or untrusted spec is structurally invalid."""


@dataclass(frozen=True)
class QaGateSpecV1:
    repository: str
    base_sha: str
    criteria: Dict[str, List[str]]
    canonical: str


def _pairs_no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise QaGateUsageError("duplicate_json_key")
        out[key] = value
    return out


def _reject_nonfinite(_: str) -> None:
    raise QaGateUsageError("nonfinite_json_value")


def _check_structure(value: Any) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > MAX_SPEC_DEPTH or nodes > MAX_CHECK_REFERENCES + MAX_CRITERIA + 16:
            raise QaGateUsageError("spec_structure_oversized")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _read_bounded_regular_file(path: Path) -> bytes:
    absolute = Path(os.path.abspath(os.fspath(path)))
    cursor = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            cursor /= part
            item = os.lstat(cursor)
            if stat.S_ISLNK(item.st_mode):
                raise QaGateUsageError("spec_path_unsafe")
        before = os.lstat(absolute)
    except OSError as exc:
        raise QaGateUsageError("spec_unreadable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise QaGateUsageError("spec_oversized_or_invalid")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(absolute, flags)
    except OSError as exc:
        raise QaGateUsageError("spec_unreadable") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > MAX_SPEC_BYTES
        ):
            raise QaGateUsageError("spec_oversized_or_invalid")
        chunks = []
        remaining = MAX_SPEC_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_SPEC_BYTES:
            raise QaGateUsageError("spec_oversized_or_invalid")
        final = os.fstat(fd)
        if (
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise QaGateUsageError("spec_changed_during_read")
        try:
            rebound = os.lstat(absolute)
        except OSError as exc:
            raise QaGateUsageError("spec_changed_during_read") from exc
        if (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino):
            raise QaGateUsageError("spec_changed_during_read")
        return data
    finally:
        os.close(fd)


def _valid_repository(repository: object) -> bool:
    if not isinstance(repository, str):
        return False
    pieces = repository.split("/")
    return len(pieces) == 2 and all(_REPO_PART_RE.fullmatch(piece) and piece not in {".", ".."} for piece in pieces)


def parse_spec(raw: Union[Mapping[str, Any], bytes, str, Path]) -> QaGateSpecV1:
    if isinstance(raw, Path):
        return parse_spec(_read_bounded_regular_file(raw))
    if isinstance(raw, Mapping):
        value: Any = dict(raw)
    else:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(data, bytes) or len(data) > MAX_SPEC_BYTES:
            raise QaGateUsageError("spec_oversized_or_invalid")
        try:
            value = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_pairs_no_duplicates,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QaGateUsageError("malformed_spec") from exc
    _check_structure(value)
    expected = {"schema", "schema_version", "repository", "base_sha", "criteria"}
    if not isinstance(value, dict) or set(value) != expected:
        raise QaGateUsageError("invalid_spec_shape")
    repository, base_sha, criteria = value["repository"], value["base_sha"], value["criteria"]
    if value["schema"] != "QaGateSpecV1" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise QaGateUsageError("invalid_spec_schema")
    if not _valid_repository(repository):
        raise QaGateUsageError("invalid_repository")
    if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
        raise QaGateUsageError("invalid_base_sha")
    if not isinstance(criteria, dict) or not criteria or len(criteria) > MAX_CRITERIA:
        raise QaGateUsageError("invalid_criteria")
    normalised: Dict[str, List[str]] = {}
    references = 0
    for criterion, check_ids in criteria.items():
        if not isinstance(criterion, str) or not _ID_RE.fullmatch(criterion):
            raise QaGateUsageError("invalid_criterion_id")
        if (
            not isinstance(check_ids, list)
            or not check_ids
            or len(check_ids) > MAX_CHECKS_PER_CRITERION
            or not all(isinstance(item, str) and _ID_RE.fullmatch(item) for item in check_ids)
        ):
            raise QaGateUsageError("invalid_check_mapping")
        if len(set(check_ids)) != len(check_ids):
            raise QaGateUsageError("duplicate_check_mapping")
        references += len(check_ids)
        if references > MAX_CHECK_REFERENCES:
            raise QaGateUsageError("criteria_map_oversized")
        normalised[criterion] = sorted(check_ids)
    canonical = json.dumps(
        {
            "schema": "QaGateSpecV1",
            "schema_version": 1,
            "repository": repository,
            "base_sha": base_sha,
            "criteria": {key: normalised[key] for key in sorted(normalised)},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return QaGateSpecV1(repository, base_sha, normalised, canonical)


def _normalised_executable(executable: str) -> Tuple[str, str]:
    basename = executable.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    suffix = ""
    for candidate in (".exe", ".com", ".cmd", ".bat"):
        if basename.endswith(candidate):
            basename = basename[: -len(candidate)]
            suffix = candidate
            break
    return basename, suffix


def safe_argv(argv: Sequence[str]) -> bool:
    if not argv or not all(isinstance(part, str) and part for part in argv):
        return False
    if any(any(unicodedata.category(character) == "Cc" for character in part) for part in argv):
        return False
    executable, suffix = _normalised_executable(argv[0])
    if (
        not executable
        or suffix in {".cmd", ".bat"}
        or executable in _WRAPPERS
        or executable in _CAPABILITY_EXECUTABLES
    ):
        return False
    if len(argv) > 1 and (
        executable in {"uv", "pipx", "poetry"} and argv[1] == "run"
        or executable == "npm" and argv[1] in {"exec", "x"}
    ):
        return False
    return not any(character in "{}" for part in argv for character in part)


def public_identifiers_safe(criteria: Mapping[str, Sequence[str]], private_markers: Sequence[str]) -> bool:
    markers = [marker.casefold() for marker in private_markers if marker]
    public_values = list(criteria)
    public_values.extend(check_id for check_ids in criteria.values() for check_id in check_ids)
    return not any(marker in value.casefold() for marker in markers for value in public_values)
