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
class IssueAutomationConfig:
    enabled: bool
    enabled_repositories: Dict[str, str]
    require_labels: List[str]
    ignore_labels: List[str]
    branch_prefix: str
    draft_pr_title_template: str
    issue_reply_template: str
    max_paths_per_issue: int
    max_changed_files: int
    max_diff_lines: int
    classifier_command: List[str]
    builder_command: List[str]
    reviewer_command: List[str]
    build_runner_identity: RunnerIdentityPolicy
    review_runner_identity: RunnerIdentityPolicy

    @property
    def trigger_label(self) -> str:
        """Backward-compatible single-label accessor for the first require label."""
        return self.require_labels[0] if self.require_labels else ""


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
    issue_automation: Optional[IssueAutomationConfig] = None

    def is_excluded(self, repository: str) -> bool:
        repo = repository.lower()
        return any(repo == ex.lower() for ex in self.excluded_repositories)

    def policy_for(self, repository: str) -> Optional[RepoPolicy]:
        exact = self.repository_policies.get(repository.lower())
        if exact is not None:
            return exact
        return self.repository_policies.get("*")

    def exact_policy_for(self, repository: str) -> Optional[RepoPolicy]:
        return self.repository_policies.get(repository.lower())


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


def _runner_identity_policy(raw: Any, *, label: str = "required_runner_identity") -> RunnerIdentityPolicy:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be an object")
    expected = {"profile", "provider", "model", "reasoning_effort", "service_tier"}
    if set(raw) != expected:
        raise ConfigError(
            f"{label} must contain exactly profile, provider, model, "
            "reasoning_effort, service_tier"
        )
    values = {key: str(raw[key]).strip() for key in expected}
    if not all(values.values()):
        raise ConfigError(f"{label} values must not be empty")
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


_SAFE_BRANCH_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _parse_issue_command(
    raw: Mapping[str, Any],
    key: str,
    *,
    required_placeholders: Sequence[str],
) -> List[str]:
    if key not in raw:
        raise ConfigError(f"issue_automation.{key} is required")
    value = raw[key]
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"issue_automation.{key} must be a non-empty array of strings (argv)")
    command = list(value)
    joined = "\n".join(command)
    missing = [placeholder for placeholder in required_placeholders if placeholder not in joined]
    if missing:
        raise ConfigError(
            f"issue_automation.{key} is missing placeholders: {','.join(missing)}"
        )
    if any("\x00" in part or "\n" in part or "\r" in part for part in command):
        raise ConfigError(f"issue_automation.{key} contains a control character")
    return command


def _parse_issue_automation(raw: Any) -> IssueAutomationConfig:
    if not isinstance(raw, dict):
        raise ConfigError("issue_automation must be an object")
    allowed = {
        "enabled",
        "enabled_repositories",
        "repositories",
        "require_labels",
        "ignore_labels",
        "trigger_label",
        "branch_prefix",
        "draft_pr_title_template",
        "issue_reply_template",
        "max_paths_per_issue",
        "max_changed_files",
        "max_diff_lines",
        "classifier_command",
        "builder_command",
        "reviewer_command",
        "build_runner_identity",
        "review_runner_identity",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError("unknown issue_automation keys: " + ",".join(unknown))

    if "enabled" not in raw or not isinstance(raw.get("enabled"), bool):
        raise ConfigError("issue_automation.enabled must be a boolean")
    enabled = bool(raw["enabled"])

    enabled_raw = raw.get("enabled_repositories")
    if enabled_raw is None and isinstance(raw.get("repositories"), list):
        # Packet shape: repositories allowlist; default branch resolved at runtime.
        repos_list = raw.get("repositories") or []
        if not all(isinstance(item, str) for item in repos_list):
            raise ConfigError("issue_automation.repositories must be an array of strings")
        enabled_raw = {str(item): "main" for item in repos_list}
    if not isinstance(enabled_raw, dict) or not enabled_raw:
        raise ConfigError(
            "issue_automation.enabled_repositories must be a non-empty object of owner/name -> base branch"
        )
    enabled_repositories: Dict[str, str] = {}
    for repo, base in enabled_raw.items():
        if not isinstance(repo, str) or not isinstance(base, str):
            raise ConfigError(
                "issue_automation.enabled_repositories keys and values must be strings"
            )
        repo_key = repo.strip()
        base_ref = base.strip().strip("/")
        if not _SAFE_REPO_RE.fullmatch(repo_key) or ".." in repo_key:
            raise ConfigError(
                f"issue_automation.enabled_repositories key is not exact owner/name: {repo}"
            )
        if not base_ref or not _SAFE_BRANCH_PREFIX_RE.match(base_ref) or ".." in base_ref:
            raise ConfigError(
                f"issue_automation.enabled_repositories base branch is unsafe for {repo_key}"
            )
        lowered = repo_key.lower()
        if lowered in {k.lower() for k in enabled_repositories}:
            raise ConfigError(f"duplicate enabled repository: {repo_key}")
        enabled_repositories[repo_key] = base_ref

    require_labels: List[str] = []
    if "require_labels" in raw:
        value = raw.get("require_labels")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError("issue_automation.require_labels must be an array of strings")
        require_labels = [item.strip() for item in value if str(item).strip()]
    elif "trigger_label" in raw:
        trigger_label = str(raw.get("trigger_label", "")).strip()
        if trigger_label:
            require_labels = [trigger_label]
    if not require_labels:
        raise ConfigError("issue_automation.require_labels (or trigger_label) must not be empty")
    for label in require_labels:
        if any(ch in label for ch in ("\n", "\r", "\x00")):
            raise ConfigError("issue_automation.require_labels contains a control character")

    ignore_labels: List[str] = []
    if "ignore_labels" in raw:
        value = raw.get("ignore_labels")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError("issue_automation.ignore_labels must be an array of strings")
        ignore_labels = [item.strip() for item in value if str(item).strip()]
        for label in ignore_labels:
            if any(ch in label for ch in ("\n", "\r", "\x00")):
                raise ConfigError("issue_automation.ignore_labels contains a control character")

    branch_prefix = str(raw.get("branch_prefix", "agent-ops/issue")).strip().strip("/")
    if not branch_prefix or not _SAFE_BRANCH_PREFIX_RE.match(branch_prefix):
        raise ConfigError("issue_automation.branch_prefix is unsafe or missing")
    if ".." in branch_prefix or branch_prefix.startswith("-") or branch_prefix.endswith(".lock"):
        raise ConfigError("issue_automation.branch_prefix is unsafe")

    title_tpl = str(raw.get("draft_pr_title_template", "agent-ops: issue #{issue_number}")).strip()
    reply_tpl = str(
        raw.get(
            "issue_reply_template",
            "Draft PR ready: {pr_url}\nSHA: {resulting_sha}\nChecks: {named_checks}",
        )
    ).strip()
    if not title_tpl or not reply_tpl:
        raise ConfigError("issue_automation title/reply templates are required")
    if any(ch in title_tpl for ch in ("\x00", "\r")):
        raise ConfigError("issue_automation.draft_pr_title_template contains a control character")
    if "\x00" in reply_tpl:
        raise ConfigError("issue_automation.issue_reply_template contains a control character")

    def _positive_int(key: str, default: int) -> int:
        if key not in raw:
            return default
        try:
            value = int(raw[key])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"issue_automation.{key} must be an integer") from exc
        if value < 1:
            raise ConfigError(f"issue_automation.{key} must be >= 1")
        return value

    max_paths = _positive_int("max_paths_per_issue", 8)
    max_files = _positive_int("max_changed_files", 20)
    max_diff = _positive_int("max_diff_lines", 400)

    classifier_command = _parse_issue_command(
        raw,
        "classifier_command",
        required_placeholders=("{request_path}", "{response_path}"),
    )
    builder_command = _parse_issue_command(
        raw,
        "builder_command",
        required_placeholders=("{request_path}", "{response_path}", "{worktree_path}"),
    )
    reviewer_command = _parse_issue_command(
        raw,
        "reviewer_command",
        required_placeholders=("{request_path}", "{response_path}", "{worktree_path}"),
    )
    build_identity = _runner_identity_policy(
        raw.get("build_runner_identity"),
        label="issue_automation.build_runner_identity",
    )
    review_identity = _runner_identity_policy(
        raw.get("review_runner_identity"),
        label="issue_automation.review_runner_identity",
    )
    if build_identity == review_identity:
        raise ConfigError(
            "issue_automation.build_runner_identity and review_runner_identity must be distinct"
        )

    return IssueAutomationConfig(
        enabled=enabled,
        enabled_repositories=enabled_repositories,
        require_labels=require_labels,
        ignore_labels=ignore_labels,
        branch_prefix=branch_prefix,
        draft_pr_title_template=title_tpl,
        issue_reply_template=reply_tpl,
        max_paths_per_issue=max_paths,
        max_changed_files=max_files,
        max_diff_lines=max_diff,
        classifier_command=classifier_command,
        builder_command=builder_command,
        reviewer_command=reviewer_command,
        build_runner_identity=build_identity,
        review_runner_identity=review_identity,
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
        "default_verification_commands",
        "repository_policies",
        "pause_file_name",
        "reclaim_after_seconds",
        "runner_timeout_seconds",
        "gh_command",
        "git_command",
        "issue_automation",
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

    issue_automation = None
    if "issue_automation" in data and data.get("issue_automation") is not None:
        issue_automation = _parse_issue_automation(data.get("issue_automation"))
        for repository in issue_automation.enabled_repositories:
            if repository.lower() not in repo_policies or repository.lower() == "*":
                raise ConfigError(
                    f"issue_automation repository {repository} requires an exact repository_policies entry"
                )
            policy = repo_policies[repository.lower()]
            if not policy.permitted_paths:
                raise ConfigError(
                    f"issue_automation repository {repository} exact policy must define permitted_paths"
                )

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
        issue_automation=issue_automation,
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
