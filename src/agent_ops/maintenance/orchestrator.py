"""Stable CLI facade for the packet-v2 reviewer-fix orchestrator."""

from __future__ import annotations

from pathlib import Path

from agent_ops.config import Config, ensure_state_dirs
from agent_ops.maintenance.review_fix import (
    InspectOutcome,
    SweepOutcome,
    inspect_work,
    status as status_report,
    sweep,
)


def is_paused(config: Config) -> bool:
    return (config.state_dir / config.pause_file_name).exists()


def set_paused(config: Config, paused: bool) -> Path:
    ensure_state_dirs(config)
    path = config.state_dir / config.pause_file_name
    if paused:
        path.write_text("paused\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
    return path


__all__ = [
    "InspectOutcome",
    "SweepOutcome",
    "inspect_work",
    "is_paused",
    "set_paused",
    "status_report",
    "sweep",
]
