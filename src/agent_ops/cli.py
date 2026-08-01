"""agent-ops CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from typing import List, Optional

from agent_ops import __version__
from agent_ops.config import ConfigError, load_config
from agent_ops.exit_codes import HELD, OK, USAGE_OR_TOOLING
from agent_ops.maintenance.ledger import Ledger
from agent_ops.maintenance.orchestrator import (
    inspect_work,
    is_paused,
    set_paused,
    status_report,
    sweep,
)


def _print_json(data: object) -> None:
    sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _require_tools(config_gh: str = "gh", config_git: str = "git") -> Optional[str]:
    if shutil.which(config_gh) is None and config_gh == "gh":
        # allow fake/test absolute paths that exist
        pass
    if config_gh == "gh" and shutil.which("gh") is None:
        return "gh CLI not found on PATH"
    if config_git == "git" and shutil.which("git") is None:
        return "git not found on PATH"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-ops",
        description="Evidence-gated operations for local AI agent stacks",
    )
    parser.add_argument("--version", action="version", version=f"agent-ops {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("pr", help="Pull request maintenance loop")
    pr_sub = pr.add_subparsers(dest="pr_command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", required=True, help="Path to local JSON config")

    p_sweep = pr_sub.add_parser("sweep", help="One complete sweep then exit")
    add_config(p_sweep)

    p_inspect = pr_sub.add_parser("inspect", help="Report discoverable work without claiming")
    add_config(p_inspect)

    p_status = pr_sub.add_parser("status", help="Latest redacted state and blockers")
    add_config(p_status)

    p_pause = pr_sub.add_parser("pause", help="Globally pause the loop (inspect-only until resume)")
    add_config(p_pause)

    p_resume = pr_sub.add_parser("resume", help="Clear global pause")
    add_config(p_resume)

    p_clear = pr_sub.add_parser(
        "clear-circuit",
        help="Clear global circuit breaker after diagnosis (does not unpause)",
    )
    add_config(p_clear)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "pr":
        parser.error("unknown command")
        return USAGE_OR_TOOLING

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write(f"config error: {exc}\n")
        return USAGE_OR_TOOLING

    tool_err = _require_tools(config.gh_command, config.git_command)
    if tool_err and args.pr_command in ("sweep", "inspect"):
        sys.stderr.write(f"tooling error: {tool_err}\n")
        return USAGE_OR_TOOLING

    if args.pr_command == "inspect":
        if is_paused(config):
            _print_json({"paused": True, "signals": [], "inspected_prs": [], "skips": []})
            return OK
        result = inspect_work(config)
        _print_json(
            {
                "paused": False,
                "inspected_prs": result.inspected_prs,
                "signals": [s.to_public_dict() for s in result.signals],
                "skips": [
                    {
                        "repository": s.repository,
                        "pr_number": s.pr_number,
                        "reason": s.reason,
                        "thread_node_id": s.thread_node_id,
                    }
                    for s in result.skips
                ],
            }
        )
        return OK

    if args.pr_command == "status":
        _print_json(status_report(config))
        return OK

    if args.pr_command == "pause":
        path = set_paused(config, True)
        sys.stdout.write(f"paused:{path}\n")
        return OK

    if args.pr_command == "resume":
        path = set_paused(config, False)
        sys.stdout.write("resumed\n")
        return OK

    if args.pr_command == "clear-circuit":
        ledger = Ledger(config.state_dir / "ledger.sqlite3")
        ledger.clear_circuit()
        sys.stdout.write("circuit_cleared\n")
        return OK

    if args.pr_command == "sweep":
        outcome = sweep(config)
        # Operator visibility: silent for no work; concise otherwise
        if outcome.message in ("no_work", "paused", "no_new_claims") or outcome.message.startswith(
            "busy:"
        ):
            if config.notification_mode == "verbose":
                sys.stdout.write(outcome.message + "\n")
            return OK
        sys.stdout.write(outcome.message + "\n")
        return outcome.exit_code

    parser.error("unknown pr subcommand")
    return USAGE_OR_TOOLING


if __name__ == "__main__":
    raise SystemExit(main())
