"""Isolated, local-only repository QA release gate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Union

from agent_ops.config import Config
from agent_ops.contracts import CheckResultV1, EvidenceBundleV1
from agent_ops.exit_codes import HELD, OK, USAGE_OR_TOOLING
from agent_ops.qa.repository import (
    PinnedRepository,
    RepositorySafetyError,
    RepositoryState,
    canonical_identity,
    operation_in_progress,
    repository_state,
    run_git,
)
from agent_ops.qa.spec import QaGateUsageError, parse_spec, public_identifiers_safe, safe_argv


@dataclass(frozen=True)
class QaGateOutcome:
    exit_code: int
    bundle: EvidenceBundleV1

    def render(self) -> str:
        return json.dumps(self.bundle.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def _bounded_path() -> str:
    entries = []
    for item in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if item and os.path.isabs(item) and item not in entries:
            candidate = os.pathsep.join([*entries, item])
            if len(candidate) > 8192:
                break
            entries.append(item)
        if len(entries) == 64:
            break
    return os.pathsep.join(entries) if entries else os.defpath


def _check_env(root: Path) -> Dict[str, str]:
    locale = os.environ.get("LC_ALL") or os.environ.get("LANG") or "C"
    if len(locale) > 128 or any(ord(character) < 32 for character in locale):
        locale = "C"
    env = {
        "PATH": _bounded_path(),
        "LANG": locale,
        "LC_ALL": locale,
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "GH_CONFIG_DIR": str(root / "gh"),
        "XDG_CONFIG_HOME": str(root / "xdg"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": os.devnull,
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CEILING_DIRECTORIES": str(root),
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_OFFLINE": "1",
        "CARGO_NET_OFFLINE": "true",
        "npm_config_offline": "true",
        "npm_config_update_notifier": "false",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }
    return env


def _result(check_id: str, sha: str, status: str, summary: str, refs: List[str]) -> CheckResultV1:
    return CheckResultV1(check_id=check_id, subject_ref=sha, status=status, summary=summary, evidence_refs=refs)


def _bundle(spec_text: str, sha: str, checks: List[CheckResultV1], verdict: str) -> EvidenceBundleV1:
    records = [item.to_dict() for item in checks]
    digest = hashlib.sha256((spec_text + "\n" + sha + "\n" + json.dumps(records, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
    return EvidenceBundleV1(run_id="sha256:" + digest, schema_version=1, base_sha=sha, resulting_sha=sha,
                            checks=checks, verdict=verdict,
                            redaction_record={"raw_output_excluded": True, "commands_excluded": True,
                                              "paths_excluded": True, "credentials_excluded": True,
                                              "repository_contents_excluded": True,
                                              "relative_path_source_isolation": True,
                                              "operating_system_sandbox": False,
                                              "absolute_path_confinement": False,
                                              "raw_socket_confinement": False})


def _source_ready(
    git: str,
    source: PinnedRepository,
    repository: str,
    base_sha: str,
    env: Mapping[str, str],
) -> tuple[bool, str, RepositoryState | None]:
    if not source.still_bound():
        return False, "source_path_changed", None
    try:
        inside = run_git(
            git,
            source.command_path,
            "rev-parse",
            "--is-inside-work-tree",
            env=env,
            pass_fds=source.pass_fds,
        ).strip()
        if inside != b"true":
            return False, "source_not_git_worktree", None
        if canonical_identity(git, source.command_path, env, source.pass_fds) != repository.casefold():
            return False, "repository_identity_mismatch", None
        head = run_git(
            git, source.command_path, "rev-parse", "HEAD", env=env, pass_fds=source.pass_fds
        ).decode("ascii", "strict").strip()
        if head != base_sha:
            return False, "base_sha_mismatch", None
        status = run_git(
            git,
            source.command_path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            env=env,
            pass_fds=source.pass_fds,
        )
        if status:
            return False, "source_not_clean", None
        if operation_in_progress(git, source.command_path, env, source.pass_fds):
            return False, "git_operation_in_progress", None
        current = repository_state(git, source.command_path, env, source.pass_fds)
        if current.head != head or current.status != status:
            return False, "source_changed_during_snapshot", None
        if not source.still_bound():
            return False, "source_path_changed", None
        return True, "", current
    except RepositorySafetyError as exc:
        if exc.code == "tracked_symlink_escapes_clone":
            return False, exc.code, None
        return False, "source_not_git_worktree", None
    except UnicodeError:
        return False, "source_not_git_worktree", None


def _create_workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix="agent-ops-qa-"))


def _set_repository_hold(checks: List[CheckResultV1], sha: str, code: str) -> None:
    existing = checks[0]
    refs = list(existing.evidence_refs)
    marker = "hold_code=" + code
    if marker not in refs:
        refs.append(marker)
    summary = code if existing.status == "PASS" or existing.summary == "source_preflight_failed" else existing.summary
    checks[0] = _result("repository-state", sha, "HOLD", summary, refs)


def _process_refs(criteria: Sequence[str], return_code: int, stdout: bytes, stderr: bytes) -> List[str]:
    return [
        "criteria=" + ",".join(sorted(criteria)),
        "return_code=" + str(return_code),
        "stdout_bytes=" + str(len(stdout)),
        "stderr_bytes=" + str(len(stderr)),
        "output_sha256=" + hashlib.sha256(stdout + stderr).hexdigest(),
    ]


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
    if (
        not public_identifiers_safe(spec.criteria, config.private_markers)
        or any(check not in commands or not safe_argv(commands[check]) for check in check_to_criteria)
    ):
        check = _result("repository-state", spec.base_sha, "HOLD", "invalid_trusted_check", [])
        return QaGateOutcome(USAGE_OR_TOOLING, _bundle(spec.canonical, spec.base_sha, [check], "HOLD"))
    checks: List[CheckResultV1] = [
        _result("repository-state", spec.base_sha, "HOLD", "source_preflight_failed", [])
    ]
    workspace: Path | None = None
    pinned: PinnedRepository | None = None
    source_before: RepositoryState | None = None
    verdict = "PASS"
    try:
        workspace = _create_workspace()
        os.chmod(workspace, 0o700)
        for name in ("home", "tmp", "gh", "xdg", "clone"):
            (workspace / name).mkdir(mode=0o700)
        env = _check_env(workspace)
        try:
            pinned = PinnedRepository.open(source)
        except RepositorySafetyError as exc:
            _set_repository_hold(checks, spec.base_sha, exc.code)
            raise RepositorySafetyError("source_preflight_failed") from exc
        ready, code, source_before = _source_ready(
            config.git_command, pinned, spec.repository, spec.base_sha, env
        )
        if not ready or source_before is None:
            _set_repository_hold(checks, spec.base_sha, code)
            verdict = "HOLD"
            raise RepositorySafetyError("source_preflight_failed")
        checks[0] = _result(
            "repository-state",
            spec.base_sha,
            "PASS",
            "source_bound",
            ["state_digest=sha256:" + source_before.evidence_digest()],
        )
        clone = workspace / "clone" / "repository"

        def enter_pinned_source() -> None:
            os.fchdir(pinned.fd)

        clone_proc = subprocess.run(
            [
                config.git_command,
                "clone",
                "--no-hardlinks",
                "--no-local",
                "--no-checkout",
                "--",
                ".",
                str(clone),
            ],
            capture_output=True,
            check=False,
            shell=False,
            env=env,
            pass_fds=pinned.pass_fds,
            preexec_fn=enter_pinned_source,
        )
        if clone_proc.returncode:
            raise RepositorySafetyError("clone_failed")
        run_git(
            config.git_command,
            clone,
            "remote",
            "set-url",
            "origin",
            "https://github.com/" + spec.repository + ".git",
            env=env,
        )
        if canonical_identity(config.git_command, clone, env) != spec.repository.casefold():
            raise RepositorySafetyError("clone_identity_mismatch")
        run_git(config.git_command, clone, "checkout", "--detach", spec.base_sha, env=env)
        clone_before = repository_state(config.git_command, clone, env)
        if clone_before.head != spec.base_sha or clone_before.status:
            raise RepositorySafetyError("clone_not_clean")
        for check_id in sorted(check_to_criteria):
            argv = commands[check_id]
            precheck_refs = _process_refs(check_to_criteria[check_id], -1, b"", b"")
            try:
                clone_precheck = repository_state(config.git_command, clone, env)
                before_identity = canonical_identity(config.git_command, clone, env)
            except RepositorySafetyError:
                clone_precheck = None
                before_identity = ""
            if (
                clone_precheck is None
                or before_identity != spec.repository.casefold()
                or clone_precheck.mutation_key() != clone_before.mutation_key()
            ):
                checks.append(_result(check_id, spec.base_sha, "HOLD", "clone_mutation", precheck_refs))
                verdict = "HOLD"
                break
            source_precheck_ready, source_precheck_code, source_precheck = _source_ready(
                config.git_command, pinned, spec.repository, spec.base_sha, env
            )
            if not source_precheck_ready or source_precheck != source_before:
                _set_repository_hold(
                    checks,
                    spec.base_sha,
                    source_precheck_code if not source_precheck_ready else "source_mutation",
                )
                checks.append(_result(check_id, spec.base_sha, "HOLD", "source_mutation", precheck_refs))
                verdict = "HOLD"
                break
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(clone),
                    capture_output=True,
                    check=False,
                    shell=False,
                    env=env,
                    timeout=config.runner_timeout_seconds,
                )
                return_code = proc.returncode
                stdout = proc.stdout
                stderr = proc.stderr
                summary = "exit=" + str(return_code)
            except subprocess.TimeoutExpired as exc:
                return_code = -1
                stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
                stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
                summary = "check_timeout"
            except OSError:
                return_code = -1
                stdout = b""
                stderr = b""
                summary = "check_start_failure"
            refs = _process_refs(check_to_criteria[check_id], return_code, stdout, stderr)
            try:
                clone_after = repository_state(config.git_command, clone, env)
                clone_identity = canonical_identity(config.git_command, clone, env)
                clone_mutated = (
                    clone_identity != spec.repository.casefold()
                    or clone_after.mutation_key() != clone_before.mutation_key()
                )
            except RepositorySafetyError:
                clone_mutated = True
            source_ready, source_code, source_after = _source_ready(
                config.git_command, pinned, spec.repository, spec.base_sha, env
            )
            source_mutated = not source_ready or source_after != source_before
            if source_mutated:
                _set_repository_hold(
                    checks,
                    spec.base_sha,
                    source_code if not source_ready else "source_mutation",
                )
            if clone_mutated:
                checks.append(_result(check_id, spec.base_sha, "HOLD", "clone_mutation", refs))
                verdict = "HOLD"
                break
            if source_mutated:
                checks.append(_result(check_id, spec.base_sha, "HOLD", "source_mutation", refs))
                verdict = "HOLD"
                break
            if return_code:
                checks.append(_result(check_id, spec.base_sha, "HOLD", summary, refs))
                verdict = "HOLD"
                break
            checks.append(_result(check_id, spec.base_sha, "PASS", "exit=0", refs))
        if verdict == "PASS" and len(checks) != len(check_to_criteria) + 1:
            verdict = "HOLD"
        if pinned is not None and source_before is not None:
            ready_after, after_code, source_after = _source_ready(
                config.git_command, pinned, spec.repository, spec.base_sha, env
            )
            if not ready_after or source_after != source_before:
                _set_repository_hold(
                    checks,
                    spec.base_sha,
                    after_code if not ready_after else "source_mutation",
                )
                verdict = "HOLD"
    except (OSError, RepositorySafetyError, subprocess.SubprocessError) as exc:
        if isinstance(exc, RepositorySafetyError) and exc.code == "source_preflight_failed":
            pass
        else:
            _set_repository_hold(checks, spec.base_sha, "clone_or_git_failure")
        verdict = "HOLD"
    finally:
        if pinned is not None:
            pinned.close()
        cleanup_failed = False
        if workspace is not None:
            try:
                shutil.rmtree(workspace)
            except OSError:
                cleanup_failed = True
            if os.path.lexists(workspace):
                cleanup_failed = True
        if cleanup_failed:
            _set_repository_hold(checks, spec.base_sha, "cleanup_failed")
            verdict = "HOLD"
    return QaGateOutcome(OK if verdict == "PASS" else HELD, _bundle(spec.canonical, spec.base_sha, checks, verdict))
