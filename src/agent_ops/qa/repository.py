"""Pinned repository access and mutation snapshots for the local QA gate."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Mapping, Sequence, Tuple
from urllib.parse import urlsplit


class RepositorySafetyError(RuntimeError):
    """A repository path or state cannot be proved safe."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_without_symlink_components(path: Path) -> Tuple[Path, os.stat_result]:
    absolute = _absolute_path(path)
    cursor = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            cursor /= part
            item = os.lstat(cursor)
            if stat.S_ISLNK(item.st_mode):
                raise RepositorySafetyError("source_path_unsafe")
        final = os.lstat(absolute)
    except OSError as exc:
        raise RepositorySafetyError("source_path_unsafe") from exc
    if not stat.S_ISDIR(final.st_mode):
        raise RepositorySafetyError("source_path_unsafe")
    return absolute, final


@dataclass
class PinnedRepository:
    """An open directory descriptor that survives path replacement races."""

    requested_path: Path
    absolute_path: Path
    fd: int
    device: int
    inode: int
    command_path: Path

    @classmethod
    def open(cls, path: Path) -> "PinnedRepository":
        absolute, before = _path_without_symlink_components(path)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if os.name != "posix" or nofollow is None or directory is None:
            raise RepositorySafetyError("source_path_pinning_unavailable")
        flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(absolute, flags)
        except OSError as exc:
            raise RepositorySafetyError("source_path_unsafe") from exc
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RepositorySafetyError("source_path_changed")
            _, rebound = _path_without_symlink_components(absolute)
            if (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino):
                raise RepositorySafetyError("source_path_changed")
            return cls(
                requested_path=path,
                absolute_path=absolute,
                fd=fd,
                device=opened.st_dev,
                inode=opened.st_ino,
                command_path=absolute,
            )
        except BaseException:
            os.close(fd)
            raise

    @property
    def pass_fds(self) -> Tuple[int, ...]:
        return (self.fd,)

    def still_bound(self) -> bool:
        try:
            _, current = _path_without_symlink_components(self.absolute_path)
            opened = os.fstat(self.fd)
        except (OSError, RepositorySafetyError):
            return False
        expected = (self.device, self.inode)
        return (current.st_dev, current.st_ino) == expected and (opened.st_dev, opened.st_ino) == expected

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def run_git(
    git: str,
    cwd: Path,
    *args: str,
    env: Mapping[str, str],
    pass_fds: Sequence[int] = (),
) -> bytes:
    preexec_fn = None
    argv = [git, "-C", str(cwd), *args]
    if len(pass_fds) == 1:
        source_fd = int(pass_fds[0])

        def enter_pinned_directory() -> None:
            os.fchdir(source_fd)

        preexec_fn = enter_pinned_directory
        argv = [git, *args]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            shell=False,
            env=dict(env),
            pass_fds=tuple(pass_fds),
            preexec_fn=preexec_fn,
        )
    except OSError as exc:
        raise RepositorySafetyError("git_unavailable") from exc
    if proc.returncode:
        raise RepositorySafetyError("git_command_failed")
    return proc.stdout


def canonical_identity(
    git: str,
    path: Path,
    env: Mapping[str, str],
    pass_fds: Sequence[int] = (),
) -> str:
    try:
        raw = run_git(git, path, "remote", "get-url", "origin", env=env, pass_fds=pass_fds).decode(
            "utf-8", "strict"
        ).strip()
    except UnicodeError as exc:
        raise RepositorySafetyError("repository_identity_invalid") from exc
    candidate = raw
    if raw.startswith("git@github.com:"):
        candidate = raw[len("git@github.com:") :]
    else:
        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise RepositorySafetyError("repository_identity_invalid") from exc
        if parsed.scheme not in {"https", "ssh", "git"} or parsed.hostname is None:
            raise RepositorySafetyError("repository_identity_invalid")
        if parsed.hostname.casefold() != "github.com" or parsed.query or parsed.fragment or port is not None:
            raise RepositorySafetyError("repository_identity_invalid")
        if parsed.password is not None or parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
            raise RepositorySafetyError("repository_identity_invalid")
        if parsed.scheme != "ssh" and parsed.username is not None:
            raise RepositorySafetyError("repository_identity_invalid")
        candidate = parsed.path.lstrip("/")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    pieces = candidate.split("/")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    if (
        len(pieces) != 2
        or any(not 1 <= len(piece) <= 80 for piece in pieces)
        or any(piece in {".", ".."} or set(piece) - allowed for piece in pieces)
    ):
        raise RepositorySafetyError("repository_identity_invalid")
    return candidate.casefold()


def _hash_regular_file(path: Path, expected: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RepositorySafetyError("repository_snapshot_failed") from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise RepositorySafetyError("repository_snapshot_race")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(fd)
        if (
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise RepositorySafetyError("repository_snapshot_race")
    finally:
        os.close(fd)
    return digest.hexdigest()


def _entry_row(path: Path, relative: bytes, *, hash_content: bool) -> bytes:
    try:
        item = os.lstat(path)
    except FileNotFoundError:
        return relative + b"\0missing"
    kind = "other"
    payload = ""
    if stat.S_ISREG(item.st_mode):
        kind = "file"
        payload = _hash_regular_file(path, item) if hash_content else str(item.st_size)
    elif stat.S_ISDIR(item.st_mode):
        kind = "directory"
    elif stat.S_ISLNK(item.st_mode):
        kind = "symlink"
        try:
            payload = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        except OSError as exc:
            raise RepositorySafetyError("repository_snapshot_failed") from exc
    return b"\0".join(
        (
            relative,
            kind.encode("ascii"),
            str(stat.S_IMODE(item.st_mode)).encode("ascii"),
            str(item.st_dev).encode("ascii"),
            str(item.st_ino).encode("ascii"),
            payload.encode("ascii"),
        )
    )


def _tree_digest(root: Path, *, hash_content: bool) -> str:
    rows = [_entry_row(root, b".", hash_content=hash_content)]
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        return hashlib.sha256(b"\n".join(rows)).hexdigest()
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        return hashlib.sha256(b"\n".join(rows)).hexdigest()
    pending = [(root, b"")]
    while pending:
        directory_path, prefix = pending.pop()
        directory_fd = -1
        try:
            directory_fd = os.open(
                directory_path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            with os.scandir(directory_fd) as entries:
                ordered = sorted(
                    ((entry.name, entry.stat(follow_symlinks=False)) for entry in entries),
                    key=lambda item: os.fsencode(item[0]),
                )
        except OSError as exc:
            raise RepositorySafetyError("repository_snapshot_failed") from exc
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)
        child_directories = []
        for entry_name, child_stat in ordered:
            name = os.fsencode(entry_name)
            relative = prefix + name
            child = directory_path / entry_name
            rows.append(_entry_row(child, relative, hash_content=hash_content))
            if stat.S_ISDIR(child_stat.st_mode) and not stat.S_ISLNK(child_stat.st_mode):
                child_directories.append((child, relative + b"/"))
        pending.extend(reversed(child_directories))
    return hashlib.sha256(b"\n".join(rows)).hexdigest()


def _safe_tracked_path(root: Path, name: str) -> Path:
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RepositorySafetyError("tracked_path_unsafe")
    return root.joinpath(*pure.parts)


def _symlink_stays_inside(root: Path, link: Path) -> bool:
    try:
        target_text = os.readlink(link)
        if os.path.isabs(target_text):
            return False
        root_real = root.resolve(strict=True)
        target_real = (link.parent / target_text).resolve(strict=False)
        return os.path.commonpath((os.fspath(root_real), os.fspath(target_real))) == os.fspath(root_real)
    except (OSError, RuntimeError, ValueError):
        return False


def tracked_snapshot(
    git: str,
    path: Path,
    env: Mapping[str, str],
    pass_fds: Sequence[int] = (),
) -> str:
    index = run_git(git, path, "ls-files", "-s", "-z", env=env, pass_fds=pass_fds)
    rows = [b"index\0" + index]
    directories: Dict[bytes, Path] = {b".": path}
    for entry in index.split(b"\0"):
        if not entry:
            continue
        if b"\t" not in entry:
            raise RepositorySafetyError("repository_index_invalid")
        raw_name = entry.split(b"\t", 1)[1]
        name = os.fsdecode(raw_name)
        target = _safe_tracked_path(path, name)
        try:
            item = os.lstat(target)
        except FileNotFoundError:
            item = None
        if item is not None and stat.S_ISLNK(item.st_mode) and not _symlink_stays_inside(path, target):
            raise RepositorySafetyError("tracked_symlink_escapes_clone")
        rows.append(_entry_row(target, b"path\0" + raw_name, hash_content=True))
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            encoded = os.fsencode(parent.as_posix())
            directories[encoded] = path.joinpath(*parent.parts)
            parent = parent.parent
    for relative in sorted(directories):
        rows.append(_entry_row(directories[relative], b"directory\0" + relative, hash_content=False))
    return hashlib.sha256(b"\n".join(rows)).hexdigest()


def _git_metadata_digest(
    git: str,
    path: Path,
    env: Mapping[str, str],
    pass_fds: Sequence[int],
) -> Tuple[str, str]:
    try:
        git_dir = Path(
            run_git(git, path, "rev-parse", "--absolute-git-dir", env=env, pass_fds=pass_fds)
            .decode("utf-8", "strict")
            .strip()
        )
        common_dir = Path(
            run_git(
                git,
                path,
                "rev-parse",
                "--git-common-dir",
                env=env,
                pass_fds=pass_fds,
            )
            .decode("utf-8", "strict")
            .strip()
        )
    except UnicodeError as exc:
        raise RepositorySafetyError("git_metadata_invalid") from exc
    if not common_dir.is_absolute():
        common_dir = path / common_dir
    hooks_configured = run_git(git, path, "rev-parse", "--git-path", "hooks", env=env, pass_fds=pass_fds)
    try:
        configured = Path(hooks_configured.decode("utf-8", "strict").strip())
    except UnicodeError as exc:
        raise RepositorySafetyError("git_metadata_invalid") from exc
    if not configured.is_absolute():
        configured = path / configured
    hook_rows = (
        _tree_digest(configured, hash_content=True),
        _tree_digest(git_dir / "hooks", hash_content=True),
    )
    metadata_roots: Iterable[Tuple[Path, bool]] = (
        (git_dir / "HEAD", True),
        (git_dir / "config", True),
        (git_dir / "logs", True),
        (common_dir / "logs", True),
        (common_dir / "refs", True),
        (common_dir / "packed-refs", True),
        (common_dir / "shallow", True),
        (common_dir / "objects", True),
    )
    metadata = [f"{index}:{_tree_digest(root, hash_content=content)}" for index, (root, content) in enumerate(metadata_roots)]
    return hashlib.sha256("\n".join(hook_rows).encode()).hexdigest(), hashlib.sha256("\n".join(metadata).encode()).hexdigest()


@dataclass(frozen=True)
class RepositoryState:
    head: str
    status: bytes
    tracked: str
    config: str
    hooks: str
    refs: str
    metadata: str

    def mutation_key(self) -> Tuple[str, str, str, str, str, str]:
        return self.head, self.tracked, self.config, self.hooks, self.refs, self.metadata

    def evidence_digest(self) -> str:
        fields = (self.head, self.status.hex(), self.tracked, self.config, self.hooks, self.refs, self.metadata)
        return hashlib.sha256("\n".join(fields).encode()).hexdigest()


def repository_state(
    git: str,
    path: Path,
    env: Mapping[str, str],
    pass_fds: Sequence[int] = (),
) -> RepositoryState:
    head = run_git(git, path, "rev-parse", "HEAD", env=env, pass_fds=pass_fds).decode("ascii", "strict").strip()
    status = run_git(
        git,
        path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        env=env,
        pass_fds=pass_fds,
    )
    config = hashlib.sha256(
        run_git(git, path, "config", "--local", "--null", "--list", env=env, pass_fds=pass_fds)
    ).hexdigest()
    refs = hashlib.sha256(
        run_git(
            git,
            path,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)%00",
            env=env,
            pass_fds=pass_fds,
        )
    ).hexdigest()
    hooks, metadata = _git_metadata_digest(git, path, env, tuple(pass_fds))
    return RepositoryState(
        head=head,
        status=status,
        tracked=tracked_snapshot(git, path, env, pass_fds),
        config=config,
        hooks=hooks,
        refs=refs,
        metadata=metadata,
    )


def operation_in_progress(
    git: str,
    path: Path,
    env: Mapping[str, str],
    pass_fds: Sequence[int] = (),
) -> bool:
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        try:
            raw = run_git(git, path, "rev-parse", "--git-path", marker, env=env, pass_fds=pass_fds)
            marker_path = Path(raw.decode("utf-8", "strict").strip())
        except UnicodeError as exc:
            raise RepositorySafetyError("git_metadata_invalid") from exc
        if not marker_path.is_absolute():
            marker_path = path / marker_path
        if os.path.lexists(marker_path):
            return True
    return False
