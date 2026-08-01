"""Local configuration loader (JSON only, stdlib)."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Union


class ConfigError(ValueError):
    """Invalid or missing configuration."""


ALLOWED_TRUSTED_REVIEWER_ASSOCIATIONS = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)


@dataclass(frozen=True)
class RepoPolicy:
    name: str
    permitted_paths: List[str] = field(default_factory=list)
    verification_commands: Dict[str, List[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerIdentityPolicy:
    profile: str
    provider: str
    model: str
    reasoning_effort: str
    service_tier: str


@dataclass(frozen=True)
class NotificationTriagePolicy:
    """Front-door GitHub notification triage (paste-workflow automation)."""

    enabled: bool = True
    participating_only: bool = False
    include_read: bool = False
    mark_read_on_no_action: bool = True
    mark_read_on_action: bool = True
    mark_read_on_needs_joel: bool = False
    max_per_run: int = 40
    run_fix_sweep_on_action: bool = True
    action_worker_command: List[str] = field(default_factory=list)
    max_action_attempts: int = 2
    needs_joel_command: List[str] = field(default_factory=list)


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
    required_runner_identity: RunnerIdentityPolicy
    runner_environment_allowlist: List[str]
    private_markers: List[str]
    require_github_isolation: bool
    pause_file_name: str = "PAUSED"
    reclaim_after_seconds: int = 6 * 60 * 60
    runner_timeout_seconds: int = 3600
    gh_command: str = "gh"
    git_command: str = "git"
    trusted_reviewer_associations: List[str] = field(default_factory=list)
    notification_triage: NotificationTriagePolicy = field(default_factory=NotificationTriagePolicy)

    def is_excluded(self, repository: str) -> bool:
        repo = repository.lower()
        return any(repo == ex.lower() for ex in self.excluded_repositories)

    def policy_for(self, repository: str) -> Optional[RepoPolicy]:
        exact = self.repository_policies.get(repository.lower())
        if exact is not None:
            return exact
        return self.repository_policies.get("*")


def _require_str_list(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> List[str]:
    if key not in data:
        raise ConfigError(f"missing required config key: {key}")
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{key} must be an array of strings")
    if not value and not allow_empty:
        raise ConfigError(f"{key} must not be empty")
    return list(value)


def _require_cmd(
    data: Mapping[str, Any],
    key: str,
    *,
    required_placeholders: Sequence[str],
    forbidden_placeholders: Sequence[str] = (),
) -> List[str]:
    if key not in data:
        raise ConfigError(f"missing required config key: {key}")
    value = data[key]
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{key} must be a non-empty array of strings (argv)")
    command = list(value)
    joined = "\n".join(command)
    missing = [placeholder for placeholder in required_placeholders if placeholder not in joined]
    forbidden = [placeholder for placeholder in forbidden_placeholders if placeholder in joined]
    if missing:
        raise ConfigError(f"{key} is missing placeholders: {','.join(missing)}")
    if forbidden:
        raise ConfigError(f"{key} contains forbidden placeholders: {','.join(forbidden)}")
    if any("\x00" in part or "\n" in part or "\r" in part for part in command):
        raise ConfigError(f"{key} contains a control character")
    return command


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


def _runner_identity_policy(raw: Any) -> RunnerIdentityPolicy:
    if not isinstance(raw, dict):
        raise ConfigError("required_runner_identity must be an object")
    expected = {"profile", "provider", "model", "reasoning_effort", "service_tier"}
    if set(raw) != expected:
        raise ConfigError(
            "required_runner_identity must contain exactly profile, provider, model, "
            "reasoning_effort, service_tier"
        )
    values = {key: str(raw[key]).strip() for key in expected}
    if not all(values.values()):
        raise ConfigError("required_runner_identity values must not be empty")
    return RunnerIdentityPolicy(**values)


_CREDENTIAL_ENV_NAMES: Set[str] = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_PAT",
    "SSH_AUTH_SOCK",
}


def _environment_allowlist(raw: Any) -> List[str]:
    if raw is None:
        return ["PATH", "TMPDIR", "LANG", "LC_ALL"]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError("capability_isolation.environment_allowlist must be an array of strings")
    names = [item.strip() for item in raw if item.strip()]
    secret_name = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH|API_KEY|PRIVATE_KEY)")
    isolation_name = re.compile(r"^(?:HOME|HERMES_HOME|GH_CONFIG_DIR|XDG_CONFIG_HOME|GIT_CONFIG.*)$")
    denied = sorted(
        name
        for name in names
        if name.upper() in _CREDENTIAL_ENV_NAMES
        or secret_name.search(name.upper())
        or isolation_name.match(name.upper())
    )
    if denied:
        raise ConfigError(
            "capability_isolation.environment_allowlist contains credential variables: "
            + ",".join(denied)
        )
    return names



def _parse_notification_triage(raw: Any) -> NotificationTriagePolicy:
    if raw is None:
        return NotificationTriagePolicy()
    if not isinstance(raw, dict):
        raise ConfigError("notification_triage must be an object")
    allowed = {
        "enabled",
        "participating_only",
        "include_read",
        "mark_read_on_no_action",
        "mark_read_on_action",
        "mark_read_on_needs_joel",
        "max_per_run",
        "run_fix_sweep_on_action",
        "action_worker_command",
        "max_action_attempts",
        "needs_joel_command",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError("unknown notification_triage keys: " + ",".join(unknown))

    def _bool(key: str, default: bool) -> bool:
        if key not in raw:
            return default
        value = raw[key]
        if not isinstance(value, bool):
            raise ConfigError(f"notification_triage.{key} must be a boolean")
        return value

    max_per_run = int(raw.get("max_per_run", 40))
    if max_per_run < 1 or max_per_run > 200:
        raise ConfigError("notification_triage.max_per_run must be 1..200")

    max_action_attempts = int(raw.get("max_action_attempts", 2))
    if max_action_attempts < 1 or max_action_attempts > 5:
        raise ConfigError("notification_triage.max_action_attempts must be 1..5")

    action_worker_command: List[str] = []
    if "action_worker_command" in raw and raw.get("action_worker_command") is not None:
        value = raw["action_worker_command"]
        if not isinstance(value, list) or not value or not all(
            isinstance(item, str) for item in value
        ):
            raise ConfigError(
                "notification_triage.action_worker_command must be a non-empty argv array"
            )
        action_worker_command = list(value)
        joined = "\n".join(action_worker_command)
        missing = [
            placeholder
            for placeholder in ("{request_path}", "{response_path}", "{worktree_path}")
            if placeholder not in joined
        ]
        if missing:
            raise ConfigError(
                "notification_triage.action_worker_command is missing placeholders: "
                + ",".join(missing)
            )
        if any("\x00" in item or "\n" in item or "\r" in item for item in action_worker_command):
            raise ConfigError(
                "notification_triage.action_worker_command contains a control character"
            )

    command: List[str] = []
    if "needs_joel_command" in raw and raw.get("needs_joel_command") is not None:
        value = raw["needs_joel_command"]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError("notification_triage.needs_joel_command must be an array of strings")
        command = list(value)
        if command and "{message}" not in "\n".join(command):
            raise ConfigError(
                "notification_triage.needs_joel_command must include {message} placeholder"
            )

    return NotificationTriagePolicy(
        enabled=_bool("enabled", True),
        participating_only=_bool("participating_only", False),
        include_read=_bool("include_read", False),
        mark_read_on_no_action=_bool("mark_read_on_no_action", True),
        mark_read_on_action=_bool("mark_read_on_action", True),
        mark_read_on_needs_joel=_bool("mark_read_on_needs_joel", False),
        max_per_run=max_per_run,
        run_fix_sweep_on_action=_bool("run_fix_sweep_on_action", True),
        action_worker_command=action_worker_command,
        max_action_attempts=max_action_attempts,
        needs_joel_command=command,
    )


def load_config(path: Union[str, Path]) -> Config:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be an object")

    allowed_keys = {
        "operator_logins",
        "owned_namespaces",
        "excluded_repositories",
        "trusted_reviewer_logins",
        "trusted_reviewer_associations",
        "workspace_root",
        "state_dir",
        "protected_path_patterns",
        "classifier_command",
        "builder_command",
        "reviewer_command",
        "required_runner_identity",
        "capability_isolation",
        "notification_mode",
        "notification_triage",
        "default_verification_commands",
        "repository_policies",
        "pause_file_name",
        "reclaim_after_seconds",
        "runner_timeout_seconds",
        "gh_command",
        "git_command",
    }
    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise ConfigError("unknown config keys: " + ",".join(unknown_keys))

    operator_logins = [x.lower() for x in _require_str_list(data, "operator_logins")]
    owned_namespaces = [x.lower() for x in _require_str_list(data, "owned_namespaces")]
    trusted = [x.lower() for x in _require_str_list(data, "trusted_reviewer_logins")]
    associations = (
        [
            item.upper()
            for item in _require_str_list(
                data, "trusted_reviewer_associations", allow_empty=True
            )
        ]
        if "trusted_reviewer_associations" in data
        else []
    )
    unsupported_associations = sorted(
        set(associations) - ALLOWED_TRUSTED_REVIEWER_ASSOCIATIONS
    )
    if unsupported_associations:
        raise ConfigError(
            "trusted_reviewer_associations contains unsupported values: "
            + ",".join(unsupported_associations)
        )
    excluded = (
        [x.lower() for x in _require_str_list(data, "excluded_repositories", allow_empty=True)]
        if "excluded_repositories" in data
        else []
    )

    workspace_root = Path(str(data.get("workspace_root", ""))).expanduser()
    if not str(data.get("workspace_root", "")).strip():
        raise ConfigError("workspace_root is required")
    state_dir = Path(str(data.get("state_dir", ""))).expanduser()
    if not str(data.get("state_dir", "")).strip():
        raise ConfigError("state_dir is required")
    if not workspace_root.is_absolute() or not state_dir.is_absolute():
        raise ConfigError("workspace_root and state_dir must be absolute paths")
    if workspace_root.resolve() == state_dir.resolve():
        raise ConfigError("workspace_root and state_dir must be different paths")
    common_root = Path(os.path.commonpath([workspace_root.resolve(), state_dir.resolve()]))
    if common_root in (workspace_root.resolve(), state_dir.resolve()):
        raise ConfigError("workspace_root and state_dir must not contain one another")

    protected = (
        _require_str_list(data, "protected_path_patterns", allow_empty=True)
        if "protected_path_patterns" in data
        else [".github/workflows/*", "**/credentials*", "**/.env*"]
    )

    classifier = _require_cmd(
        data,
        "classifier_command",
        required_placeholders=("{request_path}", "{response_path}"),
        forbidden_placeholders=("{worktree_path}",),
    )
    builder = _require_cmd(
        data,
        "builder_command",
        required_placeholders=("{request_path}", "{response_path}", "{worktree_path}"),
    )
    reviewer = _require_cmd(
        data,
        "reviewer_command",
        required_placeholders=("{request_path}", "{response_path}", "{worktree_path}"),
    )

    required_runner_identity = _runner_identity_policy(data.get("required_runner_identity"))
    isolation = data.get("capability_isolation")
    if not isinstance(isolation, dict) or isolation.get("enabled") is not True:
        raise ConfigError("capability_isolation.enabled must be true")
    allowed_isolation_keys = {"enabled", "environment_allowlist", "private_markers"}
    unknown_isolation_keys = sorted(set(isolation) - allowed_isolation_keys)
    if unknown_isolation_keys:
        raise ConfigError(
            "unknown capability_isolation keys: " + ",".join(unknown_isolation_keys)
        )
    runner_environment_allowlist = _environment_allowlist(
        isolation.get("environment_allowlist")
    )
    private_markers = isolation.get("private_markers", [])
    if not isinstance(private_markers, list) or not all(
        isinstance(marker, str) for marker in private_markers
    ):
        raise ConfigError("capability_isolation.private_markers must be an array of strings")

    notification_mode = str(data.get("notification_mode", "quiet"))
    if notification_mode not in ("quiet", "concise", "verbose"):
        raise ConfigError("notification_mode must be quiet|concise|verbose")

    notification_triage = _parse_notification_triage(data.get("notification_triage"))

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
        unknown_policy_keys = sorted(set(pol) - {"permitted_paths", "verification_commands"})
        if unknown_policy_keys:
            raise ConfigError(
                f"unknown repository_policies.{name} keys: " + ",".join(unknown_policy_keys)
            )
        permitted = pol.get("permitted_paths", [])
        if not isinstance(permitted, list) or not all(isinstance(item, str) for item in permitted):
            raise ConfigError(f"repository_policies.{name}.permitted_paths must be an array of strings")
        normalized_name = str(name).lower()
        repo_policies[normalized_name] = RepoPolicy(
            name=str(name),
            permitted_paths=list(permitted),
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

    pause_file_name = str(data.get("pause_file_name", "PAUSED"))
    if not pause_file_name or Path(pause_file_name).name != pause_file_name:
        raise ConfigError("pause_file_name must be a simple file name")

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
        required_runner_identity=required_runner_identity,
        runner_environment_allowlist=runner_environment_allowlist,
        private_markers=[str(marker) for marker in private_markers if str(marker)],
        require_github_isolation=True,
        pause_file_name=pause_file_name,
        reclaim_after_seconds=reclaim,
        runner_timeout_seconds=timeout,
        gh_command=str(data.get("gh_command", "gh")),
        git_command=str(data.get("git_command", "git")),
        trusted_reviewer_associations=associations,
        notification_triage=notification_triage,
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
