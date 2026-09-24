"""Deterministic PR filtering/eligibility logic.

Age arithmetic, label membership, and self-authorship are all judgment-free,
so they live here as tested functions rather than being decided by agent
inference on every run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

WORK_IN_PROGRESS_HOLD_LABEL = "work-in-progress/hold"
DO_NOT_MERGE_HOLD_LABEL = "do-not-merge/hold"
NEEDS_REBASE_LABEL = "needs-rebase"

MIN_AGE_DAYS_FOR_REVIEW = 14
STALE_AGE_DAYS = 90

REASON_ELIGIBLE = "eligible"
REASON_TOO_NEW = "too-new"
REASON_SELF_AUTHORED = "self-authored"
REASON_WIP_HOLD = "work-in-progress-hold"


def parse_github_timestamp(value: str) -> datetime:
    """Parse a GitHub API timestamp, e.g. '2026-01-02T03:04:05Z'."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def age_days(created_at: str, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    created = parse_github_timestamp(created_at)
    return (now - created).total_seconds() / 86400.0


def label_names(pr: Dict[str, Any]) -> set:
    return {label.get("name", "") for label in pr.get("labels", []) or []}


def has_label(pr: Dict[str, Any], label: str) -> bool:
    return label in label_names(pr)


def pr_author(pr: Dict[str, Any]) -> str:
    return (pr.get("user") or {}).get("login", "")


@dataclass
class Eligibility:
    eligible: bool
    reason: str


def check_eligibility(pr: Dict[str, Any], self_login: str, now: Optional[datetime] = None) -> Eligibility:
    """A PR is eligible for review only if it is NOT self-authored, NOT
    labeled work-in-progress/hold, and older than MIN_AGE_DAYS_FOR_REVIEW.
    Self-authored and work-in-progress/hold are full skips (checked before
    age, since age doesn't matter for either)."""
    author = pr_author(pr)
    if author and self_login and author.lower() == self_login.lower():
        return Eligibility(False, REASON_SELF_AUTHORED)
    if has_label(pr, WORK_IN_PROGRESS_HOLD_LABEL):
        return Eligibility(False, REASON_WIP_HOLD)
    if age_days(pr["created_at"], now) <= MIN_AGE_DAYS_FOR_REVIEW:
        return Eligibility(False, REASON_TOO_NEW)
    return Eligibility(True, REASON_ELIGIBLE)


def is_stale(pr: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    return age_days(pr["created_at"], now) > STALE_AGE_DAYS
