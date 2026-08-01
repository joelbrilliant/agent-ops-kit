"""Versioned public JSON contracts (stdlib dataclasses)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def body_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def signal_digest(signal: "SignalV1") -> str:
    payload = "|".join(
        [
            signal.repository,
            str(signal.pr_number),
            signal.thread_node_id,
            signal.latest_comment_node_id,
            signal.observed_head_sha,
            signal.body_digest,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SignalV1:
    repository: str
    pr_number: int
    thread_node_id: str
    latest_comment_node_id: str
    trusted_author_login: str
    path: str
    line: Optional[int]
    observed_head_sha: str
    body_digest: str
    untrusted: bool = True
    # Ephemeral fields for orchestration only; never persist raw body to receipts.
    base_repository: str = ""
    head_repository: str = ""
    head_ref: str = ""
    head_clone_url: str = ""
    is_fork: bool = False
    pr_url: str = ""
    # Raw body kept in memory for classifier request file only.
    _raw_body: str = field(default="", repr=False, compare=False)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "schema": "SignalV1",
            "repository": self.repository,
            "pr_number": self.pr_number,
            "thread_node_id": self.thread_node_id,
            "latest_comment_node_id": self.latest_comment_node_id,
            "trusted_author_login": self.trusted_author_login,
            "path": self.path,
            "line": self.line,
            "observed_head_sha": self.observed_head_sha,
            "body_digest": self.body_digest,
            "untrusted": True,
            "base_repository": self.base_repository or self.repository,
            "head_repository": self.head_repository or self.repository,
            "head_ref": self.head_ref,
            "is_fork": self.is_fork,
            "pr_url": self.pr_url,
        }


@dataclass
class DecisionV1:
    verdict: str  # ROUTINE | HOLD
    reason: str
    requested_allowed_paths: List[str] = field(default_factory=list)
    proposed_verification_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "DecisionV1",
            "verdict": self.verdict,
            "reason": self.reason,
            "requested_allowed_paths": list(self.requested_allowed_paths),
            "proposed_verification_ids": list(self.proposed_verification_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionV1":
        verdict = str(data.get("verdict", "HOLD")).upper()
        if verdict not in ("ROUTINE", "HOLD"):
            verdict = "HOLD"
        return cls(
            verdict=verdict,
            reason=str(data.get("reason", "invalid decision")),
            requested_allowed_paths=[str(p) for p in data.get("requested_allowed_paths", [])],
            proposed_verification_ids=[str(v) for v in data.get("proposed_verification_ids", [])],
        )


@dataclass
class TaskSpecV1:
    goal: str
    base_sha: str
    permitted_paths: List[str]
    permitted_actions: List[str]
    acceptance_criteria: List[str]
    non_goals: List[str]
    approval_class: str
    repository: str = ""
    pr_number: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "TaskSpecV1",
            "goal": self.goal,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "base_sha": self.base_sha,
            "permitted_paths": list(self.permitted_paths),
            "permitted_actions": list(self.permitted_actions),
            "acceptance_criteria": list(self.acceptance_criteria),
            "non_goals": list(self.non_goals),
            "approval_class": self.approval_class,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskSpecV1":
        return cls(
            goal=str(data.get("goal", "")),
            base_sha=str(data.get("base_sha", "")),
            permitted_paths=[str(p) for p in data.get("permitted_paths", [])],
            permitted_actions=[str(a) for a in data.get("permitted_actions", [])],
            acceptance_criteria=[str(c) for c in data.get("acceptance_criteria", [])],
            non_goals=[str(n) for n in data.get("non_goals", [])],
            approval_class=str(data.get("approval_class", "routine")),
            repository=str(data.get("repository", "")),
            pr_number=int(data.get("pr_number", 0) or 0),
        )


@dataclass
class CheckResultV1:
    check_id: str
    subject_ref: str
    status: str  # PASS | HOLD | SKIP
    summary: str
    evidence_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "CheckResultV1",
            "check_id": self.check_id,
            "subject_ref": self.subject_ref,
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass
class ActionReceiptV1:
    signal_digest: str
    base_sha: str
    resulting_sha: str
    named_checks: List[str]
    reply_node_id: Optional[str]
    outcome: str
    redaction_record: Dict[str, Any] = field(default_factory=dict)
    repository: str = ""
    pr_number: int = 0
    thread_node_id: str = ""
    hold_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "ActionReceiptV1",
            "signal_digest": self.signal_digest,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "thread_node_id": self.thread_node_id,
            "base_sha": self.base_sha,
            "resulting_sha": self.resulting_sha,
            "named_checks": list(self.named_checks),
            "reply_node_id": self.reply_node_id,
            "outcome": self.outcome,
            "hold_reason": self.hold_reason,
            "redaction_record": dict(self.redaction_record),
        }


@dataclass
class EvidenceBundleV1:
    run_id: str
    schema_version: str
    base_sha: str
    resulting_sha: str
    checks: List[CheckResultV1]
    verdict: str
    redaction_record: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "EvidenceBundleV1",
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "base_sha": self.base_sha,
            "resulting_sha": self.resulting_sha,
            "checks": [c.to_dict() for c in self.checks],
            "verdict": self.verdict,
            "redaction_record": dict(self.redaction_record),
        }


@dataclass
class NotifyTriageDecisionV1:
    """Front-door decision for one GitHub notification.

    NO_ACTION: dismiss silently (Joel paste workflow: "no action required").
    ACTION_FIX: hand to the existing PR fix loop when an actionable signal exists.
    NEEDS_JOEL: ping Joel in the configured exception channel only.
    """

    decision: str
    reason: str
    joel_summary: str = ""
    mark_read: bool = True
    related_repository: str = ""
    related_pr_number: int = 0
    related_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "NotifyTriageDecisionV1",
            "decision": self.decision,
            "reason": self.reason,
            "joel_summary": self.joel_summary,
            "mark_read": self.mark_read,
            "related_repository": self.related_repository,
            "related_pr_number": self.related_pr_number,
            "related_url": self.related_url,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NotifyTriageDecisionV1":
        decision = str(data.get("decision", "NEEDS_JOEL")).upper()
        if decision not in {"NO_ACTION", "ACTION_FIX", "NEEDS_JOEL"}:
            decision = "NEEDS_JOEL"
        return cls(
            decision=decision,
            reason=str(data.get("reason", "invalid triage decision")),
            joel_summary=str(data.get("joel_summary", "") or ""),
            mark_read=bool(data.get("mark_read", True)),
            related_repository=str(data.get("related_repository", "") or ""),
            related_pr_number=int(data.get("related_pr_number", 0) or 0),
            related_url=str(data.get("related_url", "") or ""),
        )


def dumps_json(obj: Any) -> str:
    if hasattr(obj, "to_dict"):
        payload = obj.to_dict()
    elif hasattr(obj, "to_public_dict"):
        payload = obj.to_public_dict()
    elif isinstance(obj, dict):
        payload = obj
    else:
        payload = asdict(obj)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
