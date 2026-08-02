"""Manual GitHub Watch commands."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Sequence

from .config import load_config
from .github import GitHub
from .loop import WatchLoop
from .state import StateStore
from .worker import OscarWorker


def main(argv: Sequence[str] | None = None, github: GitHub | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github-watch")
    parser.add_argument("--config", required=True)
    parser.add_argument("command", choices=("inspect", "run", "status", "pause", "resume"))
    args = parser.parse_args(argv)
    config = load_config(args.config)
    client = github or GitHub(config)
    if args.command == "inspect":
        notifications = client.list_notifications(config.batch_limit)
        print(json.dumps([_inspection(item) for item in notifications]))
        return 0
    state = StateStore(config.state_dir)
    if args.command == "pause":
        state.pause()
        print(json.dumps({"paused": True}))
        return 0
    if args.command == "resume":
        state.resume()
        print(json.dumps({"paused": False}))
        return 0
    if args.command == "status":
        print(json.dumps({"paused": state.is_paused(), "notifications": [asdict(row) for row in state.rows()]}))
        return 0
    result = WatchLoop(config, client, OscarWorker(config), state).run()
    print(json.dumps(asdict(result)))
    return 0


def _inspection(item) -> dict[str, object]:
    return {
        "id": item.notification_id,
        "repository": item.repository,
        "pull_number": item.pull_number,
        "updated_at": item.updated_at,
        "kind": item.kind,
    }
