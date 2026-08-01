"""Isolated, local-only repository QA release gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple, Union

from agent_ops.config import Config
from agent_ops.contracts import CheckResultV1, EvidenceBundleV1
from agent_ops.exit_codes import HELD, OK, USAGE_OR_TOOLING


_MAX_SPEC_BYTES = 1024 * 1024
_MAX_DEPTH = 16
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80}$")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,79}$")
_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "fish", "cmd", "powershell", "pwsh", "env", "xargs", "sudo", "nohup", "timeout"})


class QaGateUsageError(ValueError):
    """The invocation or untrusted spec is structurally invalid."""


@dataclass(frozen=True)
class QaGateSpecV1:
    repository: str
    base_sha: str
    criteria: Dict[str, List[str]]
    canonical: str


@dataclass(frozen=True)
class QaGateOutcome:
    exit_code: int
    bundle: EvidenceBundleV1

    def render(self) -> str:
        return json.dumps(self.bundle.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def _pairs_no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise QaGateUsageError("duplicate_json_key")
        out[key] = value
    return out


def _reject_nonfinite(_: str) -> None:
    raise QaGateUsageError("nonfinite_json_value")


def _depth(value: Any, current: int = 0) -> int:
    if current > _MAX_DEPTH:
        raise QaGateUsageError("spec_too_deep")
    if isinstance(value, dict):
        for child in value.values():
            _depth(child, current + 1)
    elif isinstance(value, list):
        for child in value:
            _depth(child, current + 1)
    return current


def parse_spec(raw: Union[Mapping[str, Any], bytes, str, Path]) -> QaGateSpecV1:
    if isinstance(raw, Path):
        try:
            data = raw.read_bytes()
        except OSError as exc:
            raise QaGateUsageError("spec_unreadable") from exc
        return parse_spec(data)
    if isinstance(raw, Mapping):
        value: Any = dict(raw)
    else:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(data, bytes) or len(data) > _MAX_SPEC_BYTES:
            raise QaGateUsageError("spec_oversized_or_invalid")
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
                               parse_constant=_reject_nonfinite)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QaGateUsageError("malformed_spec") from exc
    _depth(value)
    if not isinstance(value, dict) or set(value) != {"schema", "schema_version", "repository", "base_sha", "criteria"}:
        raise QaGateUsageError("invalid_spec_shape")
    repository, base_sha, criteria = value["repository"], value["base_sha"], value["criteria"]
    if value["schema"] != "QaGateSpecV1" or value["schema_version"] != 1:
        raise QaGateUsageError("invalid_spec_schema")
    if not isinstance(repository, str) or not _REPO_RE.fullmatch(repository):
        raise QaGateUsageError("invalid_repository")
    if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
        raise QaGateUsageError("invalid_base_sha")
    if not isinstance(criteria, dict) or not criteria:
        raise QaGateUsageError("invalid_criteria")
    normalised: Dict[str, List[str]] = {}
    for criterion, check_ids in criteria.items():
        if not isinstance(criterion, str) or not _ID_RE.fullmatch(criterion):
            raise QaGateUsageError("invalid_criterion_id")
        if not isinstance(check_ids, list) or not check_ids or not all(isinstance(item, str) and _ID_RE.fullmatch(item) for item in check_ids):
            raise QaGateUsageError("invalid_check_mapping")
        if len(set(check_ids)) != len(check_ids):
            raise QaGateUsageError("duplicate_check_mapping")
        normalised[criterion] = sorted(check_ids)
    canonical = json.dumps({"schema": "QaGateSpecV1", "schema_version": 1,
                            "repository": repository, "base_sha": base_sha,
                            "criteria": {key: normalised[key] for key in sorted(normalised)}},
                           sort_keys=True, separators=(",", ":"))
    return QaGateSpecV1(repository, base_sha, normalised, canonical)


def _safe_argv(argv: Sequence[str]) -> bool:
    if not argv or not all(isinstance(part, str) and part and not any(ord(ch) < 32 for ch in part) for part in argv):
        return False
    executable = Path(argv[0]).name.lower()
    if executable in _WRAPPERS or "{" in "\n".join(argv) or "}" in "\n".join(argv):
        return False
    return True


def _git(git: str, cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> bytes:
    try:
        proc = subprocess.run([git, "-C", str(cwd), *args], capture_output=True, check=False,
                              shell=False, env=dict(env) if env else None)
    except OSError as exc:
        raise RuntimeError("git_unavailable") from exc
    if proc.returncode:
        raise RuntimeError("git_command_failed")
    return proc.stdout


def _identity(git: str, path: Path, env: Mapping[str, str] | None = None) -> str:
    raw = _git(git, path, "remote", "get-url", "origin", env=env).decode("utf-8", "strict").strip()
    if raw.endswith(".git"):
        raw = raw[:-4]
    if raw.startswith("git@github.com:"):
        raw = raw.split(":", 1)[1]
    elif raw.startswith("https://github.com/"):
        raw = raw.split("https://github.com/", 1)[1]
    if not _REPO_RE.fullmatch(raw):
        raise RuntimeError("repository_identity_invalid")
    return raw.lower()


def _tracked_snapshot(git: str, path: Path, env: Mapping[str, str] | None = None) -> str:
    index = _git(git, path, "ls-files", "-s", "-z", env=env)
    signatures: List[bytes] = [index]
    for entry in index.split(b"\0"):
        if not entry or b"\t" not in entry:
            continue
        name = entry.split(b"\t", 1)[1].decode("utf-8", "surrogateescape")
        target = path / name
        try:
            item = os.lstat(target)
        except OSError:
            signatures.append(b"missing:" + entry)
            continue
        digest = ("symlink:" + hashlib.sha256(os.readlink(target).encode("utf-8", "surrogateescape")).hexdigest() if stat.S_ISLNK(item.st_mode) else hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else "nonregular")
        signatures.append((name + "\0" + str(stat.S_IMODE(item.st_mode)) + "\0" + str(item.st_ino) + "\0" + digest).encode())
    return hashlib.sha256(b"\n".join(signatures)).hexdigest()


def _state(git: str, path: Path, env: Mapping[str, str] | None = None) -> Tuple[str, str, str, str, str]:
    head = _git(git, path, "rev-parse", "HEAD", env=env).decode().strip()
    status = _git(git, path, "status", "--porcelain=v1", "--untracked-files=all", env=env).decode()
    config = hashlib.sha256(_git(git, path, "config", "--local", "--null", "--list", env=env)).hexdigest()
    hooks = _git(git, path, "rev-parse", "--git-path", "hooks", env=env).decode().strip()
    actual_hooks = str(Path(_git(git, path, "rev-parse", "--absolute-git-dir", env=env).decode().strip()) / "hooks")
    hooks_path = (path / hooks).resolve() if not os.path.isabs(hooks) else Path(hooks)
    actual_hooks_path = (path / actual_hooks).resolve() if not os.path.isabs(actual_hooks) else Path(actual_hooks)
    hook_rows: List[bytes] = []
    for root in {hooks_path, actual_hooks_path}:
        if root.exists() and root.is_dir():
            for item in sorted(root.rglob("*")):
                if item.is_file():
                    hook_rows.append(item.relative_to(root).as_posix().encode() + b":" + hashlib.sha256(item.read_bytes()).digest())
    return head, status, _tracked_snapshot(git, path, env), config, hashlib.sha256(b"\n".join(hook_rows)).hexdigest()


def _source_ready(git: str, source: Path, spec: QaGateSpecV1) -> Tuple[bool, str, Tuple[str, str, str, str, str] | None]:
    if source.is_symlink() or not source.is_dir():
        return False, "source_path_unsafe", None
    try:
        if _identity(git, source) != spec.repository.lower():
            return False, "repository_identity_mismatch", None
        current = _state(git, source)
        if current[0] != spec.base_sha:
            return False, "base_sha_mismatch", None
        if current[1]:
            return False, "source_not_clean", None
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
            marker_path = _git(git, source, "rev-parse", "--git-path", marker).decode().strip()
            marker_file = Path(marker_path) if os.path.isabs(marker_path) else source / marker_path
            if marker_file.exists():
                return False, "git_operation_in_progress", None
        return True, "", current
    except (RuntimeError, UnicodeError):
        return False, "source_not_git_worktree", None


def _check_env() -> Dict[str, str]:
    keep = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if os.environ.get(key)}
    keep.update({"HOME": tempfile.mkdtemp(prefix="agent-ops-qa-home-"), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": os.devnull,
                 "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
                 "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": os.devnull,
                 "GH_CONFIG_DIR": tempfile.mkdtemp(prefix="agent-ops-qa-gh-"), "XDG_CONFIG_HOME": tempfile.mkdtemp(prefix="agent-ops-qa-xdg-")})
    return keep


def _result(check_id: str, sha: str, status: str, summary: str, refs: List[str]) -> CheckResultV1:
    return CheckResultV1(check_id=check_id, subject_ref=sha, status=status, summary=summary, evidence_refs=refs)


def _bundle(spec_text: str, sha: str, checks: List[CheckResultV1], verdict: str) -> EvidenceBundleV1:
    records = [item.to_dict() for item in checks]
    digest = hashlib.sha256((spec_text + "\n" + sha + "\n" + json.dumps(records, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
    return EvidenceBundleV1(run_id="sha256:" + digest, schema_version=1, base_sha=sha, resulting_sha=sha,
                            checks=checks, verdict=verdict,
                            redaction_record={"raw_output_excluded": True, "commands_excluded": True,
                                              "paths_excluded": True, "credentials_excluded": True,
                                              "repository_contents_excluded": True})


def run_gate(config: Config, repository: Union[str, Path], raw_spec: Union[Mapping[str, Any], bytes, str, Path]) -> QaGateOutcome:
    """Run trusted configured checks in a disposable no-hardlink local clone."""
    try:
        spec = parse_spec(raw_spec)
    except QaGateUsageError as exc:
        check = _result("repository-state", "", "HOLD", "invalid_spec", ["hold_code=" + str(exc)])
        return QaGateOutcome(USAGE_OR_TOOLING, _bundle("invalid", "", [check], "HOLD"))
    source = Path(repository)
    policy = config.exact_policy_for(spec.repository)
    if policy is None:
        check = _result("repository-state", spec.base_sha, "HOLD", "exact_policy_required", [])
        return QaGateOutcome(USAGE_OR_TOOLING, _bundle(spec.canonical, spec.base_sha, [check], "HOLD"))
    commands = dict(config.default_verification_commands)
    commands.update(policy.verification_commands)
    check_to_criteria: Dict[str, List[str]] = {}
    for criterion, check_ids in spec.criteria.items():
        for check_id in check_ids:
            check_to_criteria.setdefault(check_id, []).append(criterion)
    if any(check not in commands or not _safe_argv(commands[check]) for check in check_to_criteria):
        check = _result("repository-state", spec.base_sha, "HOLD", "invalid_trusted_check", [])
        return QaGateOutcome(USAGE_OR_TOOLING, _bundle(spec.canonical, spec.base_sha, [check], "HOLD"))
    ready, code, source_before = _source_ready(config.git_command, source, spec)
    if not ready:
        check = _result("repository-state", spec.base_sha, "HOLD", code, [])
        return QaGateOutcome(HELD, _bundle(spec.canonical, spec.base_sha, [check], "HOLD"))
    state_check = _result("repository-state", spec.base_sha, "PASS", "source_bound", ["state_digest=sha256:" + hashlib.sha256(repr(source_before).encode()).hexdigest()])
    checks: List[CheckResultV1] = [state_check]
    clone_root = Path(tempfile.mkdtemp(prefix="agent-ops-qa-clone-"))
    clone = clone_root / "repository"
    env = _check_env()
    verdict = "PASS"
    try:
        clone_proc = subprocess.run([config.git_command, "clone", "--no-hardlinks", "--no-local", "--no-checkout", str(source), str(clone)],
                                    capture_output=True, check=False, shell=False, env=env)
        if clone_proc.returncode:
            raise RuntimeError("clone_failed")
        subprocess.run([config.git_command, "-C", str(clone), "remote", "set-url", "origin", "https://github.com/" + spec.repository + ".git"], capture_output=True, check=True, shell=False, env=env)
        if _identity(config.git_command, clone, env) != spec.repository.lower():
            raise RuntimeError("clone_identity_mismatch")
        subprocess.run([config.git_command, "-C", str(clone), "checkout", "--detach", spec.base_sha], capture_output=True,
                       check=True, shell=False, env=env)
        clone_before = _state(config.git_command, clone, env)
        if clone_before[0] != spec.base_sha or clone_before[1]:
            raise RuntimeError("clone_not_clean")
        for check_id in sorted(check_to_criteria):
            argv = commands[check_id]
            proc = subprocess.run(argv, cwd=str(clone), capture_output=True, check=False, shell=False, env=env)
            out_digest = hashlib.sha256(proc.stdout + proc.stderr).hexdigest()
            refs = ["criteria=" + ",".join(sorted(check_to_criteria[check_id])), "return_code=" + str(proc.returncode),
                    "stdout_bytes=" + str(len(proc.stdout)), "stderr_bytes=" + str(len(proc.stderr)),
                    "output_sha256=" + out_digest]
            clone_after = _state(config.git_command, clone, env)
            if proc.returncode:
                checks.append(_result(check_id, spec.base_sha, "HOLD", "exit=" + str(proc.returncode), refs))
                verdict = "HOLD"
                break
            if (clone_after[0], clone_after[2], clone_after[3], clone_after[4]) != (clone_before[0], clone_before[2], clone_before[3], clone_before[4]):
                checks.append(_result(check_id, spec.base_sha, "HOLD", "clone_mutation", refs))
                verdict = "HOLD"
                break
            checks.append(_result(check_id, spec.base_sha, "PASS", "exit=0", refs))
        if verdict == "PASS" and len(checks) != len(check_to_criteria) + 1:
            verdict = "HOLD"
    except (OSError, RuntimeError, subprocess.SubprocessError):
        checks.append(_result("repository-state", spec.base_sha, "HOLD", "clone_or_git_failure", []))
        verdict = "HOLD"
    finally:
        cleanup_failed = False
        for item in [env.get("HOME"), env.get("GH_CONFIG_DIR"), env.get("XDG_CONFIG_HOME"), str(clone_root)]:
            try:
                shutil.rmtree(str(item))
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            checks.append(_result("repository-state", spec.base_sha, "HOLD", "cleanup_failed", []))
            verdict = "HOLD"
    ready_after, after_code, source_after = _source_ready(config.git_command, source, spec)
    if not ready_after or source_after != source_before:
        checks[0] = _result("repository-state", spec.base_sha, "HOLD", after_code if not ready_after else "source_mutation", [])
        verdict = "HOLD"
    return QaGateOutcome(OK if verdict == "PASS" else HELD, _bundle(spec.canonical, spec.base_sha, checks, verdict))
