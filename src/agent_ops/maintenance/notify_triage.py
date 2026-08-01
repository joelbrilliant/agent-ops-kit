"""GitHub notification front door: act, dismiss, or NEEDS_JOEL.

Automates Joel's paste-into-agent workflow for GitHub notifications on his
contribution PRs. Untrusted notification and comment text is evidence only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from agent_ops.audit.redaction import redact_text
from agent_ops.config import Config, ensure_state_dirs
from agent_ops.contracts import NotifyTriageDecisionV1
from agent_ops.exit_codes import HELD, OK
from agent_ops.github.client import GhClient, GitHubClient, GitHubError
from agent_ops.github.discovery import (
    DiscoverSkip,
    extract_signals_from_pr,
    fetch_pr_threads,
)
from agent_ops.maintenance.ledger import Ledger
from agent_ops.maintenance.orchestrator import is_paused
from agent_ops.maintenance.review_fix import sweep
from agent_ops.process import run_argv

DECISION_NO_ACTION = "NO_ACTION"
DECISION_ACTION_FIX = "ACTION_FIX"
DECISION_NEEDS_JOEL = "NEEDS_JOEL"

_SUBJECT_PR_RE = re.compile(r"/repos/([^/]+)/([^/]+)/pulls/(\d+)(?:/|$)", re.I)
_SUBJECT_ISSUE_RE = re.compile(r"/repos/([^/]+)/([^/]+)/issues/(\d+)(?:/|$)", re.I)
_CHECK_BRANCH_RE = re.compile(
    r"\b(?:for|on)\s+(?P<branch>[A-Za-z0-9._/-]+)\s+branch\b",
    re.I,
)
_VALIDATION_ONLY_RE = re.compile(
    r"(?is)\b("
    r"lgtm|looks good|ship it|approved|approve(?:d)?|"
    r"thanks|thank you|nit:?\s*n/?a|no action required|"
    r"already (?:fixed|resolved|done)|superseded|"
    r"acknowledged|ack\b"
    r")\b"
)
_QUESTION_RE = re.compile(
    r"(?is)(\?|\b(?:could you|can you|please (?:clarify|confirm|decide)|"
    r"what do you think|should we|do you want|which option)\b)"
)
_HOLD_RE = re.compile(
    r"(?is)\b("
    r"security|credential|secret|token leak|auth bypass|"
    r"breaking change|design (?:decision|direction)|"
    r"do not merge|hold|blocker for product"
    r")\b"
)


@dataclass
class NotifyRow:
    thread_id: str
    reason: str
    updated_at: str
    repository: str
    subject_type: str
    subject_title: str
    subject_url: str
    latest_comment_url: str
    unread: bool
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NotifyTriageItem:
    notification: NotifyRow
    decision: NotifyTriageDecisionV1
    already_processed: bool = False

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.notification.thread_id,
            "repository": self.notification.repository,
            "subject_type": self.notification.subject_type,
            "subject_title": self.notification.subject_title,
            "reason": self.notification.reason,
            "updated_at": self.notification.updated_at,
            "unread": self.notification.unread,
            "already_processed": self.already_processed,
            "decision": self.decision.to_dict(),
        }


@dataclass
class NotifyTriageOutcome:
    exit_code: int
    message: str
    items: List[NotifyTriageItem] = field(default_factory=list)
    acted_fix: int = 0
    dismissed: int = 0
    needs_joel: int = 0
    sweep_triggered: bool = False
    notified_joel: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "acted_fix": self.acted_fix,
            "dismissed": self.dismissed,
            "needs_joel": self.needs_joel,
            "sweep_triggered": self.sweep_triggered,
            "notified_joel": self.notified_joel,
            "items": [item.to_public_dict() for item in self.items],
        }


def _repo_from_notification(raw: Dict[str, Any]) -> str:
    repo = raw.get("repository") or {}
    if isinstance(repo, dict):
        name = repo.get("full_name") or ""
        if name:
            return str(name)
    return ""


def parse_notification(raw: Dict[str, Any]) -> Optional[NotifyRow]:
    thread_id = str(raw.get("id") or "").strip()
    if not thread_id:
        return None
    subject = raw.get("subject") or {}
    if not isinstance(subject, dict):
        subject = {}
    return NotifyRow(
        thread_id=thread_id,
        reason=str(raw.get("reason") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        repository=_repo_from_notification(raw),
        subject_type=str(subject.get("type") or ""),
        subject_title=str(subject.get("title") or ""),
        subject_url=str(subject.get("url") or ""),
        latest_comment_url=str(subject.get("latest_comment_url") or ""),
        unread=bool(raw.get("unread", True)),
        raw=dict(raw),
    )


def parse_pr_ref(subject_url: str) -> Optional[Tuple[str, int]]:
    if not subject_url:
        return None
    match = _SUBJECT_PR_RE.search(subject_url)
    if match:
        return f"{match.group(1)}/{match.group(2)}", int(match.group(3))
    match = _SUBJECT_ISSUE_RE.search(subject_url)
    if match:
        # Issues API is also used for some PR subjects.
        return f"{match.group(1)}/{match.group(2)}", int(match.group(3))
    return None


def branch_from_check_title(title: str) -> Optional[str]:
    match = _CHECK_BRANCH_RE.search(title or "")
    if not match:
        return None
    branch = match.group("branch").strip().rstrip(".,;")
    return branch or None


def _is_operator_pr(author_login: str, operator_logins: Sequence[str]) -> bool:
    author = (author_login or "").lower()
    return any(author == login.lower() for login in operator_logins)


def _public_url(repository: str, pr_number: int = 0) -> str:
    if repository and pr_number:
        return f"https://github.com/{repository}/pull/{pr_number}"
    if repository:
        return f"https://github.com/{repository}"
    return ""


def _rest_json(client: GitHubClient, path: str) -> Any:
    # Prefer typed client methods when present; fall back to rest_get for objects.
    getter = getattr(client, "rest_get_any", None)
    if callable(getter):
        return getter(path)
    data = client.rest_get(path)
    return data


def _load_pr_rest(client: GitHubClient, repository: str, number: int) -> Dict[str, Any]:
    owner, name = repository.split("/", 1)
    data = _rest_json(client, f"repos/{owner}/{name}/pulls/{number}")
    if not isinstance(data, dict):
        raise GitHubError("pull request payload invalid")
    return data


def _load_issue_comment(client: GitHubClient, url: str) -> Optional[Dict[str, Any]]:
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    if path.startswith("api.github.com/"):
        path = path[len("api.github.com/") :]
    # Accept full API URLs by stripping host.
    if "://" in url:
        # path already from urlparse; if host is api.github.com path is fine
        path = parsed.path.lstrip("/")
    if not path.startswith("repos/"):
        # Sometimes latest_comment_url is absolute API URL with host only in netloc
        if parsed.netloc.endswith("github.com") and parsed.path.startswith("/repos/"):
            path = parsed.path.lstrip("/")
        else:
            return None
    try:
        data = _rest_json(client, path)
    except GitHubError:
        return None
    return data if isinstance(data, dict) else None


def _comment_body_decision(
    body: str,
    *,
    repository: str,
    pr_number: int,
    url: str,
) -> Optional[NotifyTriageDecisionV1]:
    text = body or ""
    if _HOLD_RE.search(text):
        return NotifyTriageDecisionV1(
            decision=DECISION_NEEDS_JOEL,
            reason="comment_flags_hold_or_security",
            joel_summary=(
                f"NEEDS_JOEL: comment on {repository}#{pr_number} may need your call "
                f"(hold/security/product). {url}"
            ),
            mark_read=False,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=url,
        )
    if _QUESTION_RE.search(text) and not _VALIDATION_ONLY_RE.search(text):
        return NotifyTriageDecisionV1(
            decision=DECISION_NEEDS_JOEL,
            reason="comment_asks_operator_question",
            joel_summary=(
                f"NEEDS_JOEL: question on {repository}#{pr_number} needs a human reply. {url}"
            ),
            mark_read=False,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=url,
        )
    if _VALIDATION_ONLY_RE.search(text):
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="validation_or_ack_only",
            mark_read=True,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=url,
        )
    return None


def _signals_for_pr(
    client: GitHubClient,
    config: Config,
    repository: str,
    pr_number: int,
) -> Tuple[List[Any], List[DiscoverSkip], Dict[str, Any]]:
    pr = fetch_pr_threads(client, repository, pr_number)
    if "number" not in pr:
        pr = dict(pr)
        pr["number"] = pr_number
    signals, skips = extract_signals_from_pr(
        pr,
        base_repository=repository,
        trusted_reviewer_logins=config.trusted_reviewer_logins,
        operator_logins=config.operator_logins,
        trusted_reviewer_associations=config.trusted_reviewer_associations,
    )
    return signals, skips, pr


def _find_open_prs_for_branch(
    client: GitHubClient,
    *,
    repository: str,
    branch: str,
    operator_logins: Sequence[str],
) -> List[Dict[str, Any]]:
    owner, name = repository.split("/", 1)
    # Prefer search: open PRs in repo with head branch.
    query = f"is:pr is:open repo:{repository} head:{branch}"
    try:
        page = client.rest_search_issues(query, page=1, per_page=20)
    except GitHubError:
        page = {"items": []}
    items = page.get("items") if isinstance(page, dict) else []
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        num = int(item.get("number") or 0)
        if num <= 0:
            continue
        try:
            pr = _load_pr_rest(client, repository, num)
        except GitHubError:
            continue
        head = pr.get("head") or {}
        head_ref = str(head.get("ref") or "")
        if head_ref != branch and not head_ref.endswith("/" + branch):
            # head may be owner:branch form in search, but REST ref is plain branch
            if head_ref.split(":")[-1] != branch:
                continue
        user = (pr.get("user") or {}).get("login") or ""
        if operator_logins and not _is_operator_pr(str(user), operator_logins):
            # Still include owned-namespace PRs opened by others only if author empty
            # Front door focuses on operator contribution PRs.
            continue
        out.append(pr)
    if out:
        return out
    # Fallback: list open PRs in repo (small repos / API shape issues).
    try:
        data = _rest_json(client, f"repos/{owner}/{name}/pulls?state=open&per_page=30")
    except GitHubError:
        return []
    if not isinstance(data, list):
        return []
    for pr in data:
        if not isinstance(pr, dict):
            continue
        head = pr.get("head") or {}
        head_ref = str(head.get("ref") or "")
        if head_ref.split(":")[-1] != branch:
            continue
        user = (pr.get("user") or {}).get("login") or ""
        if operator_logins and not _is_operator_pr(str(user), operator_logins):
            continue
        out.append(pr)
    return out


def _checks_all_green(rows: Sequence[Dict[str, Any]]) -> bool:
    if not rows:
        return True
    for row in rows:
        bucket = str(row.get("bucket") or "").lower()
        state = str(row.get("state") or "").upper()
        if bucket in {"fail", "pending"}:
            return False
        if state in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "PENDING", "QUEUED", "IN_PROGRESS"}:
            return False
        if bucket and bucket not in {"pass", "skipping", "skip"}:
            # Unknown bucket: not green
            if bucket not in {"pass"}:
                return False
    return True


def classify_notification(
    client: GitHubClient,
    config: Config,
    note: NotifyRow,
) -> NotifyTriageDecisionV1:
    repo = note.repository
    if repo and config.is_excluded(repo):
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="repository_excluded",
            mark_read=True,
            related_repository=repo,
        )

    subject_type = (note.subject_type or "").lower()
    title = note.subject_title or ""

    # Releases / discussions / security advisories: not Oscar PR ops.
    if subject_type in {"release", "repositoryvulnerabilityalert", "discussion"}:
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason=f"subject_type_{subject_type or 'unknown'}_out_of_scope",
            mark_read=True,
            related_repository=repo,
        )

    if subject_type in {"checksuite", "checkrun", "workflowrun", "ciactivity"}:
        return _classify_check_notification(client, config, note)

    pr_ref = parse_pr_ref(note.subject_url)
    if subject_type in {"pullrequest", "issue"} and pr_ref:
        repository, pr_number = pr_ref
        return _classify_pr_notification(
            client, config, note, repository=repository, pr_number=pr_number
        )

    if subject_type == "issue" and pr_ref is None:
        # Plain issue mention: only escalate if it looks operator-directed.
        url = note.subject_url
        return NotifyTriageDecisionV1(
            decision=DECISION_NEEDS_JOEL,
            reason="issue_notification_needs_human_scan",
            joel_summary=(
                f"NEEDS_JOEL: issue notification on {repo or 'unknown'}: {title[:120]}. "
                f"Not auto-actionable as a PR fix."
            ),
            mark_read=False,
            related_repository=repo,
            related_url=url,
        )

    return NotifyTriageDecisionV1(
        decision=DECISION_NEEDS_JOEL,
        reason="unclassified_notification",
        joel_summary=(
            f"NEEDS_JOEL: unclassified GitHub notification "
            f"({subject_type or 'unknown'}) on {repo or 'unknown'}: {title[:120]}"
        ),
        mark_read=False,
        related_repository=repo,
        related_url=note.subject_url,
    )


def _classify_check_notification(
    client: GitHubClient,
    config: Config,
    note: NotifyRow,
) -> NotifyTriageDecisionV1:
    repo = note.repository
    title = note.subject_title or ""
    branch = branch_from_check_title(title)
    if not repo:
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="check_notification_missing_repository",
            mark_read=True,
        )
    if not branch:
        # Without a branch we cannot safely map to an open PR; dismiss noise-like CI spam
        # only when title clearly says success; otherwise escalate once.
        if re.search(r"\b(pass|success|succeeded|completed successfully)\b", title, re.I):
            return NotifyTriageDecisionV1(
                decision=DECISION_NO_ACTION,
                reason="check_success_no_branch",
                mark_read=True,
                related_repository=repo,
            )
        return NotifyTriageDecisionV1(
            decision=DECISION_NEEDS_JOEL,
            reason="check_notification_unmapped_branch",
            joel_summary=(
                f"NEEDS_JOEL: CI notification on {repo} could not be mapped to a branch/PR: "
                f"{title[:140]}"
            ),
            mark_read=False,
            related_repository=repo,
        )

    prs = _find_open_prs_for_branch(
        client,
        repository=repo,
        branch=branch,
        operator_logins=config.operator_logins,
    )
    if not prs:
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="check_branch_has_no_open_operator_pr",
            mark_read=True,
            related_repository=repo,
        )

    # Evaluate each matching open PR's required checks on current head.
    failing: List[Tuple[int, str]] = []
    for pr in prs:
        number = int(pr.get("number") or 0)
        html = str(pr.get("html_url") or _public_url(repo, number))
        try:
            rows = client.required_checks(repo, number)
        except GitHubError:
            failing.append((number, html))
            continue
        if _checks_all_green(rows):
            continue
        failing.append((number, html))

    if not failing:
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="check_failure_superseded_or_current_green",
            mark_read=True,
            related_repository=repo,
            related_pr_number=int(prs[0].get("number") or 0),
            related_url=str(prs[0].get("html_url") or _public_url(repo, int(prs[0].get("number") or 0))),
        )

    # Current open operator PR still red: not silent. Front door does not invent CI repairs.
    number, html = failing[0]
    return NotifyTriageDecisionV1(
        decision=DECISION_NEEDS_JOEL,
        reason="check_failing_on_current_open_pr",
        joel_summary=(
            f"NEEDS_JOEL: required checks still failing on open PR {repo}#{number}. {html}"
        ),
        mark_read=False,
        related_repository=repo,
        related_pr_number=number,
        related_url=html,
    )


def _classify_pr_notification(
    client: GitHubClient,
    config: Config,
    note: NotifyRow,
    *,
    repository: str,
    pr_number: int,
) -> NotifyTriageDecisionV1:
    url = _public_url(repository, pr_number)
    try:
        pr_rest = _load_pr_rest(client, repository, pr_number)
    except GitHubError as exc:
        detail = str(exc).lower()
        if "404" in detail or "not found" in detail:
            return NotifyTriageDecisionV1(
                decision=DECISION_NO_ACTION,
                reason="pr_subject_not_found",
                mark_read=True,
                related_repository=repository,
                related_pr_number=pr_number,
            )
        return NotifyTriageDecisionV1(
            decision=DECISION_NEEDS_JOEL,
            reason="pr_lookup_failed",
            joel_summary=f"NEEDS_JOEL: could not load {repository}#{pr_number}: {exc}",
            mark_read=False,
            related_repository=repository,
            related_pr_number=pr_number,
        )

    state = str(pr_rest.get("state") or "").lower()
    merged = bool(pr_rest.get("merged"))
    author = str((pr_rest.get("user") or {}).get("login") or "")
    html = str(pr_rest.get("html_url") or url)

    if merged or state == "closed":
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="pr_closed_or_merged",
            mark_read=True,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=html,
        )

    if config.operator_logins and not _is_operator_pr(author, config.operator_logins):
        # Owned-namespace PR not authored by Joel: only act if review is on operator work.
        # Conservative: no auto-fix; escalate once if reason is review_requested/mention.
        if note.reason in {"review_requested", "mention", "assign", "author"}:
            return NotifyTriageDecisionV1(
                decision=DECISION_NEEDS_JOEL,
                reason="non_operator_author_needs_scan",
                joel_summary=(
                    f"NEEDS_JOEL: notification on {repository}#{pr_number} "
                    f"(author {author or 'unknown'}), not auto-fixed. {html}"
                ),
                mark_read=False,
                related_repository=repository,
                related_pr_number=pr_number,
                related_url=html,
            )
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="non_operator_pr_noise",
            mark_read=True,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=html,
        )

    # Slice-1 path: trusted unresolved review threads on current head.
    signals = []
    skips = []
    try:
        signals, skips, _pr = _signals_for_pr(client, config, repository, pr_number)
    except GitHubError:
        # Discovery can fail in degraded environments; still try top-level comment triage.
        signals, skips = [], []

    if signals:
        return NotifyTriageDecisionV1(
            decision=DECISION_ACTION_FIX,
            reason="trusted_unresolved_review_thread",
            joel_summary="",
            mark_read=True,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=html,
        )

    # Top-level conversation comment (common email subject path).
    comment = _load_issue_comment(client, note.latest_comment_url)
    if comment is not None:
        body = str(comment.get("body") or "")
        decided = _comment_body_decision(
            body, repository=repository, pr_number=pr_number, url=html
        )
        if decided is not None:
            return decided

    # Skip reasons that clearly mean no operator action.
    skip_reasons = {s.reason for s in skips}
    if skip_reasons and skip_reasons.issubset(
        {
            "thread_resolved",
            "thread_outdated",
            "latest_comment_from_operator",
            "latest_comment_untrusted",
            "no_comments",
            "draft_pr",
        }
    ):
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason="no_actionable_review_signal",
            mark_read=True,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=html,
        )

    # Review requested with nothing actionable yet.
    if note.reason in {"review_requested", "subscribed", "manual", "state_change"}:
        return NotifyTriageDecisionV1(
            decision=DECISION_NO_ACTION,
            reason=f"pr_open_no_actionable_signal_{note.reason or 'unknown'}",
            mark_read=True,
            related_repository=repository,
            related_pr_number=pr_number,
            related_url=html,
        )

    # Default: rare unknown activity on Joel's open PR → exception inbox.
    return NotifyTriageDecisionV1(
        decision=DECISION_NEEDS_JOEL,
        reason="open_pr_activity_needs_scan",
        joel_summary=(
            f"NEEDS_JOEL: activity on open PR {repository}#{pr_number} "
            f"({note.reason or 'unknown reason'}; {note.subject_title[:80]}). {html}"
        ),
        mark_read=False,
        related_repository=repository,
        related_pr_number=pr_number,
        related_url=html,
    )


def collect_notifications(
    client: GitHubClient,
    config: Config,
    *,
    include_read: bool = False,
) -> List[NotifyRow]:
    policy = config.notification_triage
    per_page = 50
    max_items = max(1, policy.max_per_run)
    rows: List[NotifyRow] = []
    seen: Set[str] = set()
    page = 1
    while len(rows) < max_items:
        batch = client.list_notifications(
            all_notifications=include_read or policy.include_read,
            participating=policy.participating_only,
            per_page=per_page,
            page=page,
        )
        if not batch:
            break
        for raw in batch:
            note = parse_notification(raw)
            if note is None or note.thread_id in seen:
                continue
            seen.add(note.thread_id)
            rows.append(note)
            if len(rows) >= max_items:
                break
        if len(batch) < per_page:
            break
        page += 1
        if page > 10:
            break
    return rows


def _notify_joel(config: Config, summary: str) -> bool:
    command = list(config.notification_triage.needs_joel_command or [])
    if not command:
        return False
    text = redact_text(summary, config.private_markers)
    if not text.strip():
        return False
    # Safety: {message} may only appear as a whole argv token. Never interpolate into
    # a larger string (blocks sh -c "…{message}…" injection from untrusted titles).
    argv: List[str] = []
    saw_placeholder = False
    for part in command:
        if part == "{message}":
            argv.append(text)
            saw_placeholder = True
        elif "{message}" in part:
            return False
        else:
            argv.append(part)
    if not saw_placeholder:
        argv = command + [text]
    result = run_argv(argv, timeout=60, check=False)
    return result.ok


def inspect_notifications(
    config: Config,
    client: Optional[GitHubClient] = None,
) -> NotifyTriageOutcome:
    ensure_state_dirs(config)
    github = client or GhClient(config.gh_command)
    ledger = Ledger(config.state_dir / "ledger.sqlite3")
    notes = collect_notifications(github, config, include_read=False)
    items: List[NotifyTriageItem] = []
    for note in notes:
        prior = ledger.get_notification(note.thread_id)
        if prior and prior.get("updated_at") == note.updated_at and prior.get("decision"):
            decision = NotifyTriageDecisionV1.from_dict(
                {
                    "decision": prior.get("decision"),
                    "reason": prior.get("reason") or "already_processed",
                    "joel_summary": prior.get("joel_summary") or "",
                    "mark_read": False,
                    "related_repository": prior.get("related_repository") or note.repository,
                    "related_pr_number": prior.get("related_pr_number") or 0,
                    "related_url": prior.get("related_url") or "",
                }
            )
            items.append(
                NotifyTriageItem(notification=note, decision=decision, already_processed=True)
            )
            continue
        decision = classify_notification(github, config, note)
        items.append(NotifyTriageItem(notification=note, decision=decision))
    return NotifyTriageOutcome(
        exit_code=OK,
        message="notify_inspect",
        items=items,
        acted_fix=sum(1 for i in items if i.decision.decision == DECISION_ACTION_FIX),
        dismissed=sum(1 for i in items if i.decision.decision == DECISION_NO_ACTION),
        needs_joel=sum(1 for i in items if i.decision.decision == DECISION_NEEDS_JOEL),
    )


def triage_notifications(
    config: Config,
    client: Optional[GitHubClient] = None,
    *,
    run_fix_sweep: bool = True,
) -> NotifyTriageOutcome:
    """Classify notifications, dismiss noise, hand ACTION_FIX to PR sweep, ping NEEDS_JOEL."""
    ensure_state_dirs(config)
    if not config.notification_triage.enabled:
        return NotifyTriageOutcome(exit_code=OK, message="notify_triage_disabled")

    github = client or GhClient(config.gh_command)
    ledger = Ledger(config.state_dir / "ledger.sqlite3")

    if is_paused(config):
        # Still classify for operator visibility, but do not mutate or ping.
        outcome = inspect_notifications(config, client=github)
        outcome.message = "paused_notify_inspect_only"
        return outcome

    if ledger.circuit_open():
        outcome = inspect_notifications(config, client=github)
        outcome.message = "circuit_open_notify_inspect_only"
        outcome.exit_code = HELD
        return outcome

    notes = collect_notifications(github, config, include_read=False)
    items: List[NotifyTriageItem] = []
    acted = 0
    dismissed = 0
    needs = 0
    notified = 0
    should_sweep = False
    notified_keys: set[str] = set()

    for note in notes:
        prior = ledger.get_notification(note.thread_id)
        if (
            prior
            and prior.get("updated_at") == note.updated_at
            and prior.get("decision") in {DECISION_NO_ACTION, DECISION_ACTION_FIX, DECISION_NEEDS_JOEL}
            and prior.get("status") == "processed"
        ):
            decision = NotifyTriageDecisionV1.from_dict(prior)
            items.append(
                NotifyTriageItem(notification=note, decision=decision, already_processed=True)
            )
            continue

        decision = classify_notification(github, config, note)
        items.append(NotifyTriageItem(notification=note, decision=decision))

        if decision.decision == DECISION_NO_ACTION:
            dismissed += 1
            ledger.record_notification(
                thread_id=note.thread_id,
                decision=decision.decision,
                reason=decision.reason,
                updated_at=note.updated_at,
                repository=decision.related_repository or note.repository,
                pr_number=decision.related_pr_number,
                related_url=decision.related_url,
                joel_summary="",
                status="processed",
            )
            if decision.mark_read and config.notification_triage.mark_read_on_no_action:
                try:
                    github.mark_notification_read(note.thread_id)
                except GitHubError:
                    pass
            continue

        if decision.decision == DECISION_ACTION_FIX:
            acted += 1
            should_sweep = True
            ledger.record_notification(
                thread_id=note.thread_id,
                decision=decision.decision,
                reason=decision.reason,
                updated_at=note.updated_at,
                repository=decision.related_repository or note.repository,
                pr_number=decision.related_pr_number,
                related_url=decision.related_url,
                joel_summary="",
                status="processed",
            )
            if decision.mark_read and config.notification_triage.mark_read_on_action:
                try:
                    github.mark_notification_read(note.thread_id)
                except GitHubError:
                    pass
            continue

        # NEEDS_JOEL - coalesce pings per PR within one run.
        needs += 1
        summary = decision.joel_summary or (
            f"NEEDS_JOEL: {note.repository} {note.subject_title[:120]}"
        )
        coalesce_key = (
            f"{(decision.related_repository or note.repository).lower()}#"
            f"{int(decision.related_pr_number or 0)}|{decision.reason}"
        )
        pinged = False
        if coalesce_key not in notified_keys:
            pinged = _notify_joel(config, summary)
            if pinged:
                notified += 1
                notified_keys.add(coalesce_key)
        else:
            # Already pinged this PR/reason this run; treat as processed silently.
            pinged = True
        ledger.record_notification(
            thread_id=note.thread_id,
            decision=decision.decision,
            reason=decision.reason,
            updated_at=note.updated_at,
            repository=decision.related_repository or note.repository,
            pr_number=decision.related_pr_number,
            related_url=decision.related_url,
            joel_summary=summary,
            status="processed" if pinged or not config.notification_triage.needs_joel_command else "pending_notify",
        )
        if pinged or not config.notification_triage.needs_joel_command:
            if config.notification_triage.mark_read_on_needs_joel:
                try:
                    github.mark_notification_read(note.thread_id)
                except GitHubError:
                    pass

    sweep_triggered = False
    message = "notify_triage_complete"
    exit_code = OK
    if should_sweep and run_fix_sweep:
        sweep_outcome = sweep(config, client=github)
        sweep_triggered = True
        message = f"notify_triage_complete;sweep:{sweep_outcome.message}"
        exit_code = sweep_outcome.exit_code

    return NotifyTriageOutcome(
        exit_code=exit_code,
        message=message,
        items=items,
        acted_fix=acted,
        dismissed=dismissed,
        needs_joel=needs,
        sweep_triggered=sweep_triggered,
        notified_joel=notified,
    )
