"""Path allowlist and protected-path checks."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Sequence, Set


def normalize_repo_path(path: str) -> str:
    """Normalise repo-relative paths without stripping leading dots (e.g. .github)."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    while "//" in p:
        p = p.replace("//", "/")
    return p


def is_safe_repo_path(path: str) -> bool:
    normal = normalize_repo_path(path)
    if not normal or normal.startswith("/") or "\x00" in normal:
        return False
    if ":" in normal.split("/", 1)[0]:
        return False
    parts = PurePosixPath(normal).parts
    return all(part not in ("", ".", "..") for part in parts)


def is_safe_path_pattern(pattern: str) -> bool:
    normal = normalize_repo_path(pattern)
    if not normal or normal.startswith("/") or "\x00" in normal:
        return False
    if ":" in normal.split("/", 1)[0]:
        return False
    return all(part not in ("", ".", "..") for part in PurePosixPath(normal).parts)


def path_matches(pattern: str, path: str) -> bool:
    path_n = normalize_repo_path(path)
    pat = normalize_repo_path(pattern)
    if not pat:
        return False
    # Expand simple ** prefix/suffix for directory-recursive allowlists
    if "**/" in pat or pat.startswith("**/"):
        suffix = pat.split("**/", 1)[-1]
        if fnmatch.fnmatch(path_n, suffix) or fnmatch.fnmatch(path_n, pat.replace("**/", "")):
            return True
        # any path segment prefix match e.g. **/secrets* -> secrets, a/secrets
        if any(fnmatch.fnmatch(part, suffix.rstrip("*") + "*") or fnmatch.fnmatch(path_n, f"*/{suffix}") for part in path_n.split("/")):
            return True
        if fnmatch.fnmatch(path_n, f"*/{suffix}") or path_n.startswith(suffix.rstrip("*")):
            return True
    if fnmatch.fnmatch(path_n, pat):
        return True
    # directory prefix: "src/foo" matches "src/foo/bar.py"
    if pat.endswith("/"):
        return path_n.startswith(pat) or path_n.startswith(pat.rstrip("/"))
    if path_n == pat or path_n.startswith(pat + "/"):
        return True
    # bare directory name without slash: "src" matches "src/a.py"
    if "/" not in pat and "*" not in pat and "?" not in pat:
        if path_n == pat or path_n.startswith(pat + "/"):
            return True
    return False


def is_path_allowed(path: str, allowed: Sequence[str]) -> bool:
    if not allowed:
        return False
    return any(path_matches(a, path) for a in allowed)


def is_path_protected(path: str, protected_patterns: Sequence[str]) -> bool:
    return any(path_matches(p, path) for p in protected_patterns)


def filter_changed_paths(
    changed: Iterable[str],
    *,
    allowed: Sequence[str],
    protected: Sequence[str],
) -> List[str]:
    """Return paths that violate allowlist or hit protected patterns."""
    bad: List[str] = []
    for raw in changed:
        path = normalize_repo_path(raw)
        if not is_safe_repo_path(path):
            bad.append(path or "<empty>")
            continue
        if is_path_protected(path, protected):
            bad.append(path)
            continue
        if not is_path_allowed(path, allowed):
            bad.append(path)
    return sorted(set(bad))


def resolve_under(root: Path, relative: str) -> Path:
    """Resolve relative path under root; raise if escape attempted."""
    root_r = root.resolve()
    candidate = (root_r / relative).resolve()
    try:
        candidate.relative_to(root_r)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {relative}") from exc
    return candidate
