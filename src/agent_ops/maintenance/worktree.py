"""Isolated clone + worktree management."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from agent_ops.paths import filter_changed_paths, normalize_repo_path
from agent_ops.process import ProcResult, RunnerError, run_argv


_SAFE_REF = re.compile(r"^[A-Za-z0-9._/\-]+$")
_SAFE_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _git(git_cmd: str, args: Sequence[str], *, cwd: Optional[Path] = None, check: bool = True) -> ProcResult:
    return run_argv([git_cmd, *args], cwd=cwd, check=check, timeout=600)


def repo_slug_dir(repository: str) -> str:
    if not _SAFE_REPOSITORY.fullmatch(repository):
        raise RunnerError("invalid repository name")
    if any(part in (".", "..") for part in repository.split("/")):
        raise RunnerError("invalid repository name")
    return repository.replace("/", "__")


def mirror_path(workspace_root: Path, repository: str) -> Path:
    return workspace_root / "mirrors" / repo_slug_dir(repository)


def _validate_head_ref(git_cmd: str, head_ref: str) -> None:
    if (
        not _SAFE_REF.fullmatch(head_ref)
        or head_ref.startswith(("-", ".", "/"))
        or head_ref.endswith(("/", ".", ".lock"))
        or ".." in head_ref
        or "@{" in head_ref
        or "//" in head_ref
    ):
        raise RunnerError("invalid head ref")
    ref_check = _git(git_cmd, ["check-ref-format", "--branch", head_ref], check=False)
    if not ref_check.ok:
        raise RunnerError("invalid head ref")


def worktree_path_for(workspace_root: Path, repository: str, sha: str, pr_number: int) -> Path:
    short = sha[:12]
    return workspace_root / "worktrees" / f"{repo_slug_dir(repository)}-pr{pr_number}-{short}"


def ensure_mirror(
    *,
    git_cmd: str,
    workspace_root: Path,
    repository: str,
    clone_url: str,
) -> Path:
    path = mirror_path(workspace_root, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RunnerError("mirror path must not be a symlink")
    if (path / ".git").exists() or (path / "HEAD").exists():
        remote = _git(
            git_cmd,
            ["-C", str(path), "remote", "get-url", "origin"],
            check=False,
        )
        if not remote.ok or remote.stdout.strip() != clone_url:
            raise RunnerError("mirror origin does not match expected head repository")
        # fetch updates
        _git(git_cmd, ["-C", str(path), "fetch", "--all", "--prune"], check=True)
        return path
    # Prefer bare-ish mirror via clone --mirror if fresh; use regular clone for simplicity
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # incomplete
        shutil.rmtree(path)
    _git(
        git_cmd,
        ["clone", "--mirror", "--", clone_url, str(path)],
        check=True,
    )
    return path


def create_worktree(
    *,
    git_cmd: str,
    workspace_root: Path,
    repository: str,
    clone_url: str,
    head_sha: str,
    pr_number: int,
    branch_name: str,
) -> Path:
    if not _SAFE_SHA.fullmatch(head_sha):
        raise RunnerError("invalid head sha")
    _validate_head_ref(git_cmd, branch_name)
    mirror = ensure_mirror(
        git_cmd=git_cmd,
        workspace_root=workspace_root,
        repository=repository,
        clone_url=clone_url,
    )
    wt = worktree_path_for(workspace_root, repository, head_sha, pr_number)
    if wt.exists():
        # Reuse only a clean worktree on the exact branch and SHA.
        head = _git(git_cmd, ["-C", str(wt), "rev-parse", "HEAD"], check=False)
        status = _git(git_cmd, ["-C", str(wt), "status", "--porcelain"], check=False)
        branch = _git(
            git_cmd,
            ["-C", str(wt), "branch", "--show-current"],
            check=False,
        )
        if (
            head.ok
            and head.stdout.strip() == head_sha
            and status.ok
            and not status.stdout.strip()
            and branch.ok
            and branch.stdout.strip() == branch_name
        ):
            return wt
        # Remove stale worktree
        _git(git_cmd, ["-C", str(mirror), "worktree", "remove", "--force", str(wt)], check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    # Ensure sha exists in mirror
    _git(git_cmd, ["-C", str(mirror), "fetch", "origin", head_sha], check=False)
    _git(
        git_cmd,
        ["-C", str(mirror), "worktree", "add", "--detach", str(wt), head_sha],
        check=True,
    )
    # Create local branch tracking the PR head name for non-force push
    _git(git_cmd, ["-C", str(wt), "checkout", "-B", branch_name, head_sha], check=True)
    return wt


def current_head(git_cmd: str, worktree: Path) -> str:
    r = _git(git_cmd, ["-C", str(worktree), "rev-parse", "HEAD"], check=True)
    return r.stdout.strip()


def worktree_is_clean(git_cmd: str, worktree: Path) -> bool:
    result = _git(git_cmd, ["-C", str(worktree), "status", "--porcelain"], check=True)
    return not result.stdout.strip()


def base_is_ancestor(git_cmd: str, worktree: Path, base_sha: str) -> bool:
    if not _SAFE_SHA.fullmatch(base_sha):
        return False
    result = _git(
        git_cmd,
        ["-C", str(worktree), "merge-base", "--is-ancestor", base_sha, "HEAD"],
        check=False,
    )
    return result.ok


def remote_ref_sha(
    git_cmd: str, remote_url: str, head_ref: str
) -> Optional[str]:
    _validate_head_ref(git_cmd, head_ref)
    result = _git(
        git_cmd,
        ["ls-remote", "--heads", remote_url, f"refs/heads/{head_ref}"],
        check=False,
    )
    if not result.ok:
        raise RunnerError("unable to read head repository ref")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    sha = lines[0].split()[0]
    return sha if _SAFE_SHA.fullmatch(sha) else None


def remove_worktree(
    git_cmd: str,
    workspace_root: Path,
    repository: str,
    worktree: Path,
) -> None:
    mirror = mirror_path(workspace_root, repository)
    if mirror.exists():
        _git(
            git_cmd,
            ["-C", str(mirror), "worktree", "remove", "--force", str(worktree)],
            check=False,
        )
    if worktree.exists():
        shutil.rmtree(worktree)


def changed_files(git_cmd: str, worktree: Path, base_sha: str) -> List[str]:
    r = _git(git_cmd, ["-C", str(worktree), "diff", "--name-only", f"{base_sha}..HEAD"], check=True)
    return [normalize_repo_path(x) for x in r.stdout.splitlines() if x.strip()]


def diff_text(git_cmd: str, worktree: Path, base_sha: str) -> str:
    r = _git(git_cmd, ["-C", str(worktree), "diff", f"{base_sha}..HEAD"], check=True)
    return r.stdout


def commit_if_needed(git_cmd: str, worktree: Path, message: str) -> Optional[str]:
    status = _git(git_cmd, ["-C", str(worktree), "status", "--porcelain"], check=True)
    if not status.stdout.strip():
        return None
    _git(git_cmd, ["-C", str(worktree), "add", "-A"], check=True)
    _git(
        git_cmd,
        [
            "-C",
            str(worktree),
            "-c",
            "user.email=agent-ops@localhost",
            "-c",
            "user.name=agent-ops",
            "commit",
            "-m",
            message,
        ],
        check=True,
    )
    return current_head(git_cmd, worktree)


def push_head_no_force(
    *,
    git_cmd: str,
    worktree: Path,
    remote_url: str,
    head_ref: str,
) -> ProcResult:
    _validate_head_ref(git_cmd, head_ref)
    # Set remote and push without force
    _git(git_cmd, ["-C", str(worktree), "remote", "remove", "pushorigin"], check=False)
    _git(git_cmd, ["-C", str(worktree), "remote", "add", "pushorigin", remote_url], check=True)
    # Explicit non-force push of HEAD to refs/heads/<head_ref>
    return _git(
        git_cmd,
        ["-C", str(worktree), "push", "pushorigin", f"HEAD:refs/heads/{head_ref}"],
        check=False,
    )


def validate_path_bounds(
    changed: Sequence[str],
    *,
    allowed: Sequence[str],
    protected: Sequence[str],
) -> Tuple[bool, List[str]]:
    bad = filter_changed_paths(changed, allowed=allowed, protected=protected)
    return (len(bad) == 0, bad)
