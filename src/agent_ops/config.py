"""Local configuration loader (JSON only, stdlib)."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


class ConfigError(ValueError):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class RepoPolicy:
    name: str
    permitted_paths: List[str] = field(default_factory=list)
    verification_commands: Dict[str, List[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    operator_logins: List[str]
    owned_namespaces: List[str]
    excluded_repositories: List[str]
    trusted_reviewer_logins: List[str]
    workspace_root: Path
    state_dir: Path
    protected_path_patterns: List[str]
    classifier_command: List[str]
    builder_command: List[str]
    reviewer_command: List[str]
    notification_mode: str
    default_verification_commands: Dict[str, List[str]]
    repository_policies: Dict[str, RepoPolicy]
    pause_file_name: str = "PAUSED"
    reclaim_after_seconds: int = 6 * 60 * 60
    runner_timeout_seconds: int = 3600
    gh_command: str = "gh"
    git_command: str = "git"

    def is_excluded(self, repository: str) -> bool:
        repo = repository.lower()
        return any(repo == ex.lower() for ex in self.excluded_repositories)

    def policy_for(self, repository: str) -> Optional[RepoPolicy]:
        return self.repository_policies.get(repository) or self.repository_policies.get(
            repository.lower()
        )


def _require_str_list(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> List[str]:
    if key not in data:
        raise ConfigError(f"missing required config key: {key}")
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{key} must be an array of strings")
    if not value and not allow_empty:
        raise ConfigError(f"{key} must not be empty")
    return list(value)


def _require_cmd(data: Mapping[str, Any], key: str) -> List[str]:
    if key not in data:
        raise ConfigError(f"missing required config key: {key}")
    value = data[key]
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{key} must be a non-empty array of strings (argv)")
    return list(value)


def _parse_verification_map(raw: Any, label: str) -> Dict[str, List[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be an object mapping id -> argv array")
    out: Dict[str, List[str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, list) or not v or not all(
            isinstance(x, str) for x in v
        ):
            raise ConfigError(f"{label}.{k} must be a non-empty argv array")
        out[k] = list(v)
    return out


def load_config(path: str | Path) -> Config:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be an object")

    operator_logins = [x.lower() for x in _require_str_list(data, "operator_logins")]
    owned_namespaces = [x.lower() for x in _require_str_list(data, "owned_namespaces")]
    trusted = [x.lower() for x in _require_str_list(data, "trusted_reviewer_logins")]
    excluded = [x.lower() for x in _require_str_list(data, "excluded_repositories", allow_empty=True)]
    if "excluded_repositories" not in data:
        excluded = []

    workspace_root = Path(str(data.get("workspace_root", ""))).expanduser()
    if not str(data.get("workspace_root", "")).strip():
        raise ConfigError("workspace_root is required")
    state_dir = Path(str(data.get("state_dir", ""))).expanduser()
    if not str(data.get("state_dir", "")).strip():
        raise ConfigError("state_dir is required")

    protected = _require_str_list(data, "protected_path_patterns", allow_empty=True)
    if "protected_path_patterns" not in data:
        protected = [".github/workflows/*", "**/credentials*", "**/.env*"]

    classifier = _require_cmd(data, "classifier_command")
    builder = _require_cmd(data, "builder_command")
    reviewer = _require_cmd(data, "reviewer_command")

    notification_mode = str(data.get("notification_mode", "quiet"))
    if notification_mode not in ("quiet", "concise", "verbose"):
        raise ConfigError("notification_mode must be quiet|concise|verbose")

    default_verification = _parse_verification_map(
        data.get("default_verification_commands", {}), "default_verification_commands"
    )

    repo_policies: Dict[str, RepoPolicy] = {}
    raw_policies = data.get("repository_policies", {}) or {}
    if not isinstance(raw_policies, dict):
        raise ConfigError("repository_policies must be an object")
    for name, pol in raw_policies.items():
        if not isinstance(pol, dict):
            raise ConfigError(f"repository_policies.{name} must be an object")
        repo_policies[str(name)] = RepoPolicy(
            name=str(name),
            permitted_paths=[str(p) for p in pol.get("permitted_paths", [])],
            verification_commands=_parse_verification_map(
                pol.get("verification_commands", {}), f"repository_policies.{name}.verification_commands"
            ),
        )

    reclaim = int(data.get("reclaim_after_seconds", 6 * 60 * 60))
    if reclaim < 60:
        raise ConfigError("reclaim_after_seconds must be >= 60")
    timeout = int(data.get("runner_timeout_seconds", 3600))
    if timeout < 1:
        raise ConfigError("runner_timeout_seconds must be >= 1")

    return Config(
        operator_logins=operator_logins,
        owned_namespaces=owned_namespaces,
        excluded_repositories=excluded,
        trusted_reviewer_logins=trusted,
        workspace_root=workspace_root,
        state_dir=state_dir,
        protected_path_patterns=protected,
        classifier_command=classifier,
        builder_command=builder,
        reviewer_command=reviewer,
        notification_mode=notification_mode,
        default_verification_commands=default_verification,
        repository_policies=repo_policies,
        pause_file_name=str(data.get("pause_file_name", "PAUSED")),
        reclaim_after_seconds=reclaim,
        runner_timeout_seconds=timeout,
        gh_command=str(data.get("gh_command", "gh")),
        git_command=str(data.get("git_command", "git")),
    )


def ensure_state_dirs(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    requests = config.state_dir / "requests"
    receipts = config.state_dir / "receipts"
    requests.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    # Best-effort owner-only permissions on state dir (posix).
    try:
        os.chmod(config.state_dir, stat.S_IRWXU)
        os.chmod(requests, stat.S_IRWXU)
        os.chmod(receipts, stat.S_IRWXU)
    except OSError:
        pass


def expand_runner_argv(
    template: Sequence[str],
    *,
    request_path: Path,
    response_path: Path,
    worktree_path: Optional[Path] = None,
) -> List[str]:
    """Replace documented fixed placeholders only. Never expand untrusted text."""
    mapping = {
        "{request_path}": str(request_path),
        "{response_path}": str(response_path),
        "{worktree_path}": str(worktree_path) if worktree_path is not None else "",
    }
    out: List[str] = []
    for part in template:
        replaced = part
        for key, value in mapping.items():
            replaced = replaced.replace(key, value)
        out.append(replaced)
    return out
