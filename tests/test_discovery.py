"""Discovery: search pagination, dedupe, thread filters."""

from __future__ import annotations

from agent_ops.github.client import FakeGitHub
from agent_ops.github.discovery import (
    discover_actionable_signals,
    extract_signals_from_pr,
    search_open_prs,
)
from tests.conftest import sample_pr, wire_fake_for_pr


def test_paginated_search_and_dedupe_author_owner():
    fake = FakeGitHub()
    page1 = [
        {
            "id": 1,
            "number": 1,
            "html_url": "https://github.com/operator/demo/pull/1",
            "repository_url": "https://api.github.com/repos/operator/demo",
            "pull_request": {"url": "https://api.github.com/repos/operator/demo/pulls/1"},
        }
    ]
    page2 = [
        {
            "id": 2,
            "number": 2,
            "html_url": "https://github.com/operator/demo/pull/2",
            "repository_url": "https://api.github.com/repos/operator/demo",
            "pull_request": {"url": "https://api.github.com/repos/operator/demo/pulls/2"},
        }
    ]
    # Author search paginated
    fake.search_pages["is:pr is:open author:operator"] = [page1, page2]
    # Owner search overlaps PR 1
    fake.search_pages["is:pr is:open user:operator"] = [page1]

    items = search_open_prs(
        fake,
        operator_logins=["operator"],
        owned_namespaces=["operator"],
        max_pages=5,
        per_page=1,
    )
    numbers = sorted(i["number"] for i in items)
    assert numbers == [1, 2]


def test_extract_skips_resolved_outdated_untrusted():
    pr = sample_pr(
        threads=[
            {
                "id": "t-resolved",
                "isResolved": True,
                "isOutdated": False,
                "path": "a.py",
                "line": 1,
                "comments": {
                    "nodes": [
                        {
                            "id": "c1",
                            "author": {"login": "reviewer"},
                            "body": "x",
                            "createdAt": "t",
                            "viewerDidAuthor": False,
                        }
                    ]
                },
            },
            {
                "id": "t-outdated",
                "isResolved": False,
                "isOutdated": True,
                "path": "a.py",
                "line": 1,
                "comments": {
                    "nodes": [
                        {
                            "id": "c2",
                            "author": {"login": "reviewer"},
                            "body": "x",
                            "createdAt": "t",
                            "viewerDidAuthor": False,
                        }
                    ]
                },
            },
            {
                "id": "t-untrusted",
                "isResolved": False,
                "isOutdated": False,
                "path": "a.py",
                "line": 1,
                "comments": {
                    "nodes": [
                        {
                            "id": "c3",
                            "author": {"login": "random-user"},
                            "body": "x",
                            "createdAt": "t",
                            "viewerDidAuthor": False,
                        }
                    ]
                },
            },
            {
                "id": "t-ok",
                "isResolved": False,
                "isOutdated": False,
                "path": "demo.txt",
                "line": 2,
                "comments": {
                    "nodes": [
                        {
                            "id": "c4",
                            "author": {"login": "reviewer"},
                            "body": "fix please",
                            "createdAt": "t",
                            "viewerDidAuthor": False,
                        }
                    ]
                },
            },
        ]
    )
    signals, skips = extract_signals_from_pr(
        pr,
        base_repository="operator/demo",
        trusted_reviewer_logins=["reviewer"],
        operator_logins=["operator"],
    )
    reasons = {s.reason for s in skips}
    assert "thread_resolved" in reasons
    assert "thread_outdated" in reasons
    assert "untrusted_author" in reasons
    assert len(signals) == 1
    assert signals[0].thread_node_id == "t-ok"
    assert signals[0].untrusted is True
    assert signals[0]._raw_body == "fix please"


def test_discover_excludes_repo():
    fake = FakeGitHub()
    pr = sample_pr()
    wire_fake_for_pr(fake, pr=pr)
    result = discover_actionable_signals(
        fake,
        operator_logins=["operator"],
        owned_namespaces=["operator"],
        trusted_reviewer_logins=["reviewer"],
        excluded_repositories=["operator/demo"],
    )
    assert result.signals == []
    assert any(s.reason == "repository_excluded" for s in result.skips)
