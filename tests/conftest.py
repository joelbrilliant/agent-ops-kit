"""Shared test helpers and synthetic fixtures."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ops.config import Config, RepoPolicy
from agent_ops.github.client import FakeGitHub


def write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def sys_executable() -> str:
    return sys.executable


def make_config(tmp: Path, **overrides: Any) -> Config:
    state = tmp / "state"
    work = tmp / "workspace"
    state.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    scripts = tmp / "scripts"
    scripts.mkdir(exist_ok=True)
    classifier = scripts / "classify.py"
    builder = scripts / "build.py"
    reviewer = scripts / "review.py"

    if not classifier.exists():
        write_executable(
            classifier,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, sys
                from pathlib import Path
                req = Path(sys.argv[sys.argv.index('--request')+1] if '--request' in sys.argv else sys.argv[1])
                resp = None
                for i, a in enumerate(sys.argv):
                    if a == '--response':
                        resp = Path(sys.argv[i+1])
                if resp is None:
                    resp = Path(sys.argv[2])
                data = json.loads(req.read_text())
                body = (data.get('untrusted_body') or '').lower()
                path = data.get('path') or ''
                hold = any(
                    x in body
                    for x in [
                        'credential',
                        'force push',
                        'ignore previous',
                        'exfiltrat',
                        'drop table',
                        'product direction',
                    ]
                )
                out = {
                    'schema': 'DecisionV1',
                    'verdict': 'HOLD' if hold else 'ROUTINE',
                    'reason': 'hold_marker' if hold else 'routine',
                    'requested_allowed_paths': [path] if path and not hold else [],
                    'proposed_verification_ids': ['unit'],
                }
                resp.write_text(json.dumps(out))
                """
            ),
        )

    if not builder.exists():
        write_executable(
            builder,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, sys
                from pathlib import Path
                args = sys.argv[1:]
                def get(flag):
                    i = args.index(flag)
                    return Path(args[i+1])
                req = get('--request')
                resp = get('--response')
                wt = get('--worktree')
                data = json.loads(req.read_text())
                path = (data.get('signal') or {}).get('path') or 'demo.txt'
                target = wt / path
                target.parent.mkdir(parents=True, exist_ok=True)
                text = target.read_text() if target.exists() else 'broken=1\\n'
                new = text.replace('broken=1', 'broken=0').replace('BUG', 'FIXED')
                if new == text:
                    new = text.rstrip() + '\\n# fixed\\n'
                target.write_text(new)
                resp.write_text(json.dumps({'ok': True, 'changed': [path]}))
                """
            ),
        )

    if not reviewer.exists():
        write_executable(
            reviewer,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, sys
                from pathlib import Path
                args = sys.argv[1:]
                def get(flag):
                    i = args.index(flag)
                    return Path(args[i+1])
                resp = get('--response')
                resp.write_text(json.dumps({'ok': True, 'findings': [], 'fixed': False}))
                """
            ),
        )

    unit = scripts / "unit.sh"
    if not unit.exists():
        write_executable(unit, "#!/bin/sh\nexit 0\n")

    cfg = Config(
        operator_logins=["operator"],
        owned_namespaces=["operator"],
        excluded_repositories=[],
        trusted_reviewer_logins=["trusted-bot", "reviewer"],
        workspace_root=work,
        state_dir=state,
        protected_path_patterns=[".github/workflows/*", "secrets/*", "**/secrets*"],
        classifier_command=[
            sys_executable(),
            str(classifier),
            "--request",
            "{request_path}",
            "--response",
            "{response_path}",
        ],
        builder_command=[
            sys_executable(),
            str(builder),
            "--request",
            "{request_path}",
            "--response",
            "{response_path}",
            "--worktree",
            "{worktree_path}",
        ],
        reviewer_command=[
            sys_executable(),
            str(reviewer),
            "--request",
            "{request_path}",
            "--response",
            "{response_path}",
            "--worktree",
            "{worktree_path}",
        ],
        notification_mode="concise",
        default_verification_commands={"unit": [str(unit)]},
        repository_policies={
            "operator/demo": RepoPolicy(
                name="operator/demo",
                permitted_paths=["src/*", "demo.txt", "pkg/*"],
                verification_commands={},
            )
        },
        reclaim_after_seconds=3600,
        runner_timeout_seconds=60,
        gh_command="gh",
        git_command="git",
    )
    if overrides:
        data = cfg.__dict__.copy()
        data.update(overrides)
        cfg = Config(**data)
    return cfg


def sample_pr(
    *,
    number: int = 1,
    head_sha: str = "a" * 40,
    head_ref: str = "feat/fix",
    base_repo: str = "operator/demo",
    head_repo: str = "operator/demo",
    is_fork: bool = False,
    threads: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if threads is None:
        threads = [
            {
                "id": "thread-1",
                "isResolved": False,
                "isOutdated": False,
                "path": "demo.txt",
                "line": 1,
                "comments": {
                    "nodes": [
                        {
                            "id": "comment-1",
                            "databaseId": 1,
                            "author": {"login": "reviewer"},
                            "body": "Please set broken=0",
                            "createdAt": "2026-08-01T00:00:00Z",
                            "viewerDidAuthor": False,
                        }
                    ]
                },
            }
        ]
    return {
        "number": number,
        "url": f"https://github.com/{base_repo}/pull/{number}",
        "isDraft": True,
        "headRefName": head_ref,
        "headRefOid": head_sha,
        "headRepository": {
            "nameWithOwner": head_repo,
            "url": f"https://github.com/{head_repo}",
            "isFork": is_fork,
        },
        "baseRepository": {"nameWithOwner": base_repo},
        "author": {"login": "operator"},
        "reviewThreads": {"nodes": threads},
    }


def wire_fake_for_pr(
    fake: FakeGitHub,
    *,
    base_repo: str = "operator/demo",
    pr: Optional[Dict[str, Any]] = None,
    search_items: Optional[List[Dict[str, Any]]] = None,
    pushable: bool = True,
) -> Dict[str, Any]:
    pr = pr or sample_pr()
    num = int(pr["number"])
    item = {
        "id": 100,
        "number": num,
        "html_url": f"https://github.com/{base_repo}/pull/{num}",
        "repository_url": f"https://api.github.com/repos/{base_repo}",
        "pull_request": {"url": f"https://api.github.com/repos/{base_repo}/pulls/{num}"},
    }
    items = search_items if search_items is not None else [item]
    fake.search_pages["is:pr is:open author:operator"] = [items]
    fake.search_pages["is:pr is:open user:operator"] = [items]

    def gql(query: str, variables: Dict[str, Any]):
        if "addPullRequestReviewThreadReply" in query:
            cid = f"reply-{len(fake.replies)+1}"
            fake.replies.append(
                {
                    "id": cid,
                    "body": variables.get("body"),
                    "thread": variables.get("threadId"),
                }
            )
            tid = variables.get("threadId")
            for t in pr["reviewThreads"]["nodes"]:
                if t["id"] == tid:
                    t["comments"]["nodes"].append(
                        {
                            "id": cid,
                            "author": {"login": "operator"},
                            "body": variables.get("body"),
                            "createdAt": "2026-08-01T01:00:00Z",
                        }
                    )
            return {
                "data": {
                    "addPullRequestReviewThreadReply": {
                        "comment": {
                            "id": cid,
                            "body": variables.get("body"),
                            "url": "https://example/c",
                        }
                    }
                }
            }
        if "node(id:" in query or ("$id" in query and "PullRequestReviewThread" in query):
            tid = variables.get("id")
            for t in pr["reviewThreads"]["nodes"]:
                if t["id"] == tid:
                    return {"data": {"node": t}}
            return {"data": {"node": None}}
        if "pullRequest" in query:
            return {"data": {"repository": {"pullRequest": pr}}}
        return None

    fake.graphql_handlers.append(gql)
    head = pr["headRepository"]["nameWithOwner"]
    fake.rest_handlers[f"repos/{head}"] = {
        "permissions": {"push": pushable, "admin": False, "maintain": False},
        "full_name": head,
    }
    fake.rest_handlers[f"repos/{base_repo}"] = {
        "permissions": {"push": pushable, "admin": False, "maintain": False},
        "full_name": base_repo,
    }
    return pr
