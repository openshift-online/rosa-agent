"""Orchestrates github_client + the deterministic modules into a per-PR
worklist: everything computable without judgment, precomputed once so the
invoking skill (an agent) only has to act on it - or, for the main review
comment, layer genuinely non-deterministic Section-5 source verification and
prior-discussion synthesis on top of what's already been fetched here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .ci_status import failing_contexts
from .github_client import GhApiError, GithubClient
from .label_events import find_label_adder
from .participants import distinct_human_participants
from .ping_builder import build_consolidated_author_ping, build_hold_ping
from .pr_filter import (
    DO_NOT_MERGE_HOLD_LABEL,
    NEEDS_REBASE_LABEL,
    REASON_ELIGIBLE,
    age_days,
    check_eligibility,
    has_label,
    is_stale,
    pr_author,
)
from .recomment_guard import AUTHOR_PING_MARKER, HOLD_PING_MARKER, should_skip_main_review, should_skip_ping


@dataclass
class PRWorkItem:
    number: int
    title: str
    html_url: str
    author: str
    age_days: float
    eligible: bool
    skip_reason: str
    needs_rebase: bool
    stale: bool
    stale_reviewer_logins: List[str]
    failing_checks: List[str]
    hold_label_adder: Optional[str]
    consolidated_author_ping: Optional[str]
    hold_ping: Optional[str]
    head_sha: str = ""
    post_main_review: bool = True
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "html_url": self.html_url,
            "author": self.author,
            "age_days": round(self.age_days, 2),
            "eligible": self.eligible,
            "skip_reason": self.skip_reason,
            "needs_rebase": self.needs_rebase,
            "stale": self.stale,
            "stale_reviewer_logins": self.stale_reviewer_logins,
            "failing_checks": self.failing_checks,
            "hold_label_adder": self.hold_label_adder,
            "consolidated_author_ping": self.consolidated_author_ping,
            "hold_ping": self.hold_ping,
            "head_sha": self.head_sha,
            "post_main_review": self.post_main_review,
            "error": self.error,
        }


def _skipped_item(pr: Dict[str, Any], reason: str, now: Optional[datetime]) -> PRWorkItem:
    return PRWorkItem(
        number=pr["number"],
        title=pr.get("title", ""),
        html_url=pr.get("html_url", ""),
        author=pr_author(pr),
        age_days=age_days(pr["created_at"], now),
        eligible=False,
        skip_reason=reason,
        needs_rebase=False,
        stale=False,
        stale_reviewer_logins=[],
        failing_checks=[],
        hold_label_adder=None,
        consolidated_author_ping=None,
        hold_ping=None,
        head_sha="",
        post_main_review=False,
    )


def _error_item(pr: Dict[str, Any], error: str, now: Optional[datetime]) -> PRWorkItem:
    return PRWorkItem(
        number=pr["number"],
        title=pr.get("title", ""),
        html_url=pr.get("html_url", ""),
        author=pr_author(pr),
        age_days=age_days(pr["created_at"], now),
        eligible=False,
        skip_reason="error",
        needs_rebase=False,
        stale=False,
        stale_reviewer_logins=[],
        failing_checks=[],
        hold_label_adder=None,
        consolidated_author_ping=None,
        hold_ping=None,
        head_sha="",
        post_main_review=False,
        error=error,
    )


def build_work_item(
    client: GithubClient,
    repo: str,
    pr_summary: Dict[str, Any],
    self_login: str,
    now: Optional[datetime] = None,
) -> PRWorkItem:
    """`pr_summary` is one entry from the PR list endpoint - enough to decide
    eligibility (author/labels/created_at are all present there) without an
    extra API call for PRs we're about to skip anyway."""
    eligibility = check_eligibility(pr_summary, self_login, now)
    if not eligibility.eligible:
        return _skipped_item(pr_summary, eligibility.reason, now)

    resolved_now = now or datetime.now(timezone.utc)
    number = pr_summary["number"]
    # The list endpoint omits mergeable_state/head detail; fetch the full
    # resource now that we know this PR is actually going to be reviewed.
    pr = client.get_json(f"repos/{repo}/pulls/{number}")
    author = pr_author(pr)

    needs_rebase = has_label(pr, NEEDS_REBASE_LABEL)
    stale = is_stale(pr, now)

    issue_comments = client.get_paginated(f"repos/{repo}/issues/{number}/comments")
    reviews = client.get_paginated(f"repos/{repo}/pulls/{number}/reviews")
    review_comments = client.get_paginated(f"repos/{repo}/pulls/{number}/comments")

    stale_reviewers: List[str] = []
    if stale:
        stale_reviewers = distinct_human_participants(
            issue_comments, reviews, review_comments,
            exclude_logins=[self_login, author],
        )

    ref = (pr.get("head") or {}).get("sha", "")
    check_runs = client.get_paginated(f"repos/{repo}/commits/{ref}/check-runs") if ref else []
    statuses = client.get_paginated(f"repos/{repo}/commits/{ref}/statuses") if ref else []
    failing = failing_contexts(check_runs, statuses)

    hold_adder = None
    if has_label(pr, DO_NOT_MERGE_HOLD_LABEL):
        events = client.get_paginated(f"repos/{repo}/issues/{number}/events")
        hold_adder = find_label_adder(events, DO_NOT_MERGE_HOLD_LABEL)

    consolidated = build_consolidated_author_ping(
        author_login=author,
        needs_rebase=needs_rebase,
        mergeable_state=pr.get("mergeable_state"),
        stale=stale,
        stale_reviewer_logins=stale_reviewers,
        failing_checks=failing,
    )
    hold_ping = build_hold_ping(hold_adder) if hold_adder else None

    # Recomment cooldown: don't repeat a comment this job already posted
    # within the last 3 weeks unless something material changed since then
    # (a new commit for the review, or different wording for a ping).
    if consolidated is not None and should_skip_ping(
        issue_comments, self_login, AUTHOR_PING_MARKER, consolidated, resolved_now
    ):
        consolidated = None
    if hold_ping is not None and should_skip_ping(
        issue_comments, self_login, HOLD_PING_MARKER, hold_ping, resolved_now
    ):
        hold_ping = None
    post_main_review = not should_skip_main_review(
        issue_comments, reviews, review_comments, self_login, ref, resolved_now
    )

    return PRWorkItem(
        number=number,
        title=pr.get("title", ""),
        html_url=pr.get("html_url", ""),
        author=author,
        age_days=age_days(pr["created_at"], now),
        eligible=True,
        skip_reason=REASON_ELIGIBLE,
        needs_rebase=needs_rebase,
        stale=stale,
        stale_reviewer_logins=stale_reviewers,
        failing_checks=failing,
        hold_label_adder=hold_adder,
        consolidated_author_ping=consolidated,
        hold_ping=hold_ping,
        head_sha=ref,
        post_main_review=post_main_review,
    )


def build_worklist(
    client: GithubClient,
    repo: str,
    self_login: str,
    now: Optional[datetime] = None,
) -> List[PRWorkItem]:
    """Build the full worklist, one item per open PR. A `GhApiError` while
    processing a single PR does not abort the run - per this job's
    "always try to accomplish as much as possible" requirement, that PR gets
    an `error`-flagged item (for the invoking skill to report/file an Issue
    about) and the rest of the sweep continues.
    """
    prs = client.get_paginated(f"repos/{repo}/pulls", params={"state": "open", "per_page": "100"})
    items = []
    for pr in prs:
        try:
            items.append(build_work_item(client, repo, pr, self_login, now))
        except GhApiError as exc:
            items.append(_error_item(pr, str(exc), now))
    return items
