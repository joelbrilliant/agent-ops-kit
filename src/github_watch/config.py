"""Configuration parsing for the small GitHub Watch runtime."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CHANNEL = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_KEYS = {
    "github_login",
    "allowed_namespaces",
    "state_dir",
    "worktree_root",
    "oscar_command",
    "oscar_timeout_seconds",
    "buzz_channel",
    "buzz_executable",
    "batch_limit",
    "max_notification_age_hours",
}


class ConfigError(ValueError):
    """Raised when a local configuration is outside the fixed contract."""


@dataclass(frozen=True)
class Config:
    github_login: str
    allowed_namespaces: tuple[str, ...]
    state_dir: Path
    worktree_root: Path
    oscar_command: str
    oscar_timeout_seconds: int
    buzz_channel: str
    buzz_executable: str
    batch_limit: int
    max_notification_age_hours: int


def load_config(path: str | Path) -> Config:
    """Load the only accepted local JSON configuration shape."""
    location = Path(path).expanduser()
    try:
        value = json.loads(location.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"cannot read configuration: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"invalid JSON configuration: {error.msg}") from error
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise ConfigError("configuration must contain exactly the documented keys")
    return Config(
        github_login=_name(value, "github_login"),
        allowed_namespaces=_names(value.get("allowed_namespaces")),
        state_dir=_path(value, "state_dir"),
        worktree_root=_path(value, "worktree_root"),
        oscar_command=_executable(value, "oscar_command"),
        oscar_timeout_seconds=_positive_int(value, "oscar_timeout_seconds", 3600),
        buzz_channel=_channel(value),
        buzz_executable=_executable(value, "buzz_executable"),
        batch_limit=_positive_int(value, "batch_limit", 100),
        max_notification_age_hours=_positive_int(
            value,
            "max_notification_age_hours",
            24 * 90,
        ),
    )


def _name(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not _NAME.fullmatch(result):
        raise ConfigError(f"{key} must be a GitHub name")
    return result


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("allowed_namespaces must be a non-empty list")
    names = tuple(value)
    if len(set(names)) != len(names) or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in names):
        raise ConfigError("allowed_namespaces must contain unique GitHub names")
    return names


def _path(value: dict[str, Any], key: str) -> Path:
    result = value.get(key)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise ConfigError(f"{key} must be a path")
    return Path(result).expanduser()


def _executable(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result or "\x00" in result or "\n" in result:
        raise ConfigError(f"{key} must be one executable path or name")
    if result == "/usr/bin/open" or ".app/Contents/MacOS/" in result:
        raise ConfigError(f"{key} must be headless")
    return result


def _positive_int(value: dict[str, Any], key: str, maximum: int) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or not 1 <= result <= maximum:
        raise ConfigError(f"{key} must be an integer between 1 and {maximum}")
    return result


def _text(value: dict[str, Any], key: str, maximum: int) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result or len(result) > maximum or any(c in result for c in "\x00\r\n"):
        raise ConfigError(f"{key} must be a short single-line value")
    return result


def _channel(value: dict[str, Any]) -> str:
    result = _text(value, "buzz_channel", 36)
    if not _CHANNEL.fullmatch(result):
        raise ConfigError("buzz_channel must be a UUID")
    return result
