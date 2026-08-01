"""agent-ops CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from typing import List, Optional

from agent_ops import __version__
from agent_ops.audit.redaction import redact_text
from agent_ops.audit.report import build_audit_report, render_audit_report
from agent_ops.config import ConfigError, load_config
from agent_ops.exit_codes import HELD, OK, USAGE_OR_TOOLING
from agent_ops.github.client import GitHubError
from agent_ops.maintenance.issue_fix import inspect_issue_work, issue_sweep
from agent_ops.maintenance.ledger import Ledger
from agent_ops.maintenance.orchestrator import (
    inspect_work,
    is_paused,
    set_paused,
    status_report,
    sweep,
)
from agent_ops.process import RunnerError


def _print_json(data: object) -> None:
    sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _require_tools(config_gh: str = "gh", config_git: str = "git") -> Optional[str]:
    if shutil.which(config_gh) is None:
        return "configured gh CLI not found"
    if shutil.which(config_git) is None:
        return "configured git executable not found"
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

    issue = sub.add_parser("issue", help="Issue-to-draft-PR automation")
    issue_sub = issue.add_subparsers(dest="issue_command", required=True)

    p_issue_inspect = issue_sub.add_parser(
        "inspect",
        help="Discover eligible labelled issues without claiming or launching models",
    )
    add_config(p_issue_inspect)

    p_issue_sweep = issue_sub.add_parser(
        "sweep",
        help="One serial issue sweep then exit",
    )
    add_config(p_issue_sweep)

    audit = sub.add_parser("audit", help="Read-only local receipt audit")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    p_audit_report = audit_sub.add_parser("report", help="Render a public-safe receipt report")
    add_config(p_audit_report)
    p_audit_report.add_argument("--format", choices=("json", "markdown"), default="json")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command not in ("pr", "issue", "audit"):
        parser.error("unknown command")
        return USAGE_OR_TOOLING

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write(f"config error: {exc}\n")
        return USAGE_OR_TOOLING

    if args.command == "audit":
        report = build_audit_report(config)
        try:
            sys.stdout.write(render_audit_report(report, args.format, config.private_markers))
        except ValueError:
            return HELD
        return OK if report.verdict == "PASS" else HELD

    if args.command == "issue":
        if config.issue_automation is None:
            sys.stderr.write("config error: issue_automation is not configured\n")
            return USAGE_OR_TOOLING
        tool_err = _require_tools(config.gh_command, config.git_command)
        if tool_err:
            sys.stderr.write(f"tooling error: {tool_err}\n")
            return USAGE_OR_TOOLING
        if args.issue_command == "inspect":
            try:
                result = inspect_issue_work(config)
            except (GitHubError, RunnerError, OSError, ValueError) as exc:
                detail = redact_text(str(exc) or exc.__class__.__name__, config.private_markers)
                sys.stderr.write(f"issue inspect error: {detail}\n")
                return HELD
            _print_json(
                {
                    "paused": is_paused(config),
                    "inspected_issues": result.inspected_issues,
                    "signals": [s.to_public_dict() for s in result.signals],
                    "skips": [
                        {
                            "repository": s.repository,
                            "issue_number": s.issue_number,
                            "reason": s.reason,
                            "issue_node_id": s.issue_node_id,
                        }
                        for s in result.skips
                    ],
                }
            )
            return OK
        if args.issue_command == "sweep":
            outcome = issue_sweep(config)
            if outcome.message in (
                "no_actionable_signal",
                "paused_inspect_only",
                "busy_inspect_only",
                "issue_automation_disabled",
            ):
                if config.notification_mode == "verbose":
                    sys.stdout.write(outcome.message + "\n")
                return OK
            sys.stdout.write(outcome.message + "\n")
            return outcome.exit_code
        parser.error("unknown issue subcommand")
        return USAGE_OR_TOOLING

    tool_err = _require_tools(config.gh_command, config.git_command)
    if tool_err and args.pr_command in ("sweep", "inspect"):
        sys.stderr.write(f"tooling error: {tool_err}\n")
        return USAGE_OR_TOOLING

    if args.pr_command == "inspect":
        try:
            result = inspect_work(config)
        except (GitHubError, RunnerError, OSError, ValueError) as exc:
            detail = redact_text(str(exc) or exc.__class__.__name__, config.private_markers)
            sys.stderr.write(f"inspect error: {detail}\n")
            return HELD
        _print_json(
            {
                "paused": is_paused(config),
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
        set_paused(config, True)
        sys.stdout.write("paused\n")
        return OK

    if args.pr_command == "resume":
        set_paused(config, False)
        sys.stdout.write("resumed\n")
        return OK

    if args.pr_command == "clear-circuit":
        ledger = Ledger(config.state_dir / "ledger.sqlite3")
        ledger.clear_circuit()
        sys.stdout.write("circuit_cleared\n")
        return OK

    if args.pr_command == "sweep":
        outcome = sweep(config)
        if outcome.message in (
            "no_actionable_signal",
            "paused_inspect_only",
            "busy_inspect_only",
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
