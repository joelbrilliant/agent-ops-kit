"""Resolve GitHub Actions notification shapes and prove current-head checks."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import quote


_FAILED_WORKFLOW = re.compile(r"^CI workflow run failed for (.+) branch$")
Getter = Callable[[str], Any]


def resolve_failed_workflow(
    repository: str, title: str, notification_updated_at: str, get: Getter
) -> tuple[int | None, str | None]:
    """Map GitHub's null-URL CheckSuite notification to one exact pull and source head."""
    match = _FAILED_WORKFLOW.fullmatch(title)
    if not match or len(match.group(1)) > 255 or any(character in match.group(1) for character in "\x00\r\n"):
        return None, None
    branch = match.group(1)
    encoded_branch = quote(branch, safe="")
    payload = get(
        f"/repos/{repository}/actions/runs?branch={encoded_branch}&event=pull_request&status=failure&per_page=100"
    )
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        return None, None
    candidates = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_branch") == branch
        and isinstance(run.get("updated_at"), str)
        and run["updated_at"] <= notification_updated_at
    ]
    if not candidates:
        return None, None
    run = max(candidates, key=lambda candidate: candidate["updated_at"])
    head_repository = run.get("head_repository")
    owner = head_repository.get("owner") if isinstance(head_repository, dict) else None
    login = owner.get("login") if isinstance(owner, dict) else None
    source_head = run.get("head_sha")
    if not isinstance(login, str) or not login or not isinstance(source_head, str) or not source_head:
        return None, None
    encoded_head = quote(f"{login}:{branch}", safe="")
    pulls = get(f"/repos/{repository}/pulls?state=all&head={encoded_head}&per_page=100")
    numbers = {
        pull["number"]
        for pull in pulls
        if isinstance(pull, dict) and isinstance(pull.get("number"), int) and pull["number"] > 0
    } if isinstance(pulls, list) else set()
    return (numbers.pop(), source_head) if len(numbers) == 1 else (None, None)


def current_head_is_green(repository: str, head_sha: str, get: Getter) -> bool:
    """Require every check run and every legacy status on the exact current head to be green."""
    payload = get(f"/repos/{repository}/commits/{head_sha}/check-runs?per_page=100")
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if not isinstance(runs, list) or not runs or isinstance(total, int) and total != len(runs):
        return False
    checks_green = all(
        isinstance(run, dict)
        and run.get("status") == "completed"
        and run.get("conclusion") in {"success", "neutral", "skipped"}
        for run in runs
    )
    if not checks_green:
        return False
    status = get(f"/repos/{repository}/commits/{head_sha}/status")
    statuses = status.get("statuses") if isinstance(status, dict) else None
    status_total = status.get("total_count") if isinstance(status, dict) else None
    if not isinstance(statuses, list) or isinstance(status_total, int) and status_total != len(statuses):
        return False
    return not statuses or status.get("state") == "success"
