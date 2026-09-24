"""Recomment-cooldown guard.

Explicit requirement: if this job already commented on a PR within the last
3 weeks, don't comment again unless something materially changed since then
(a new commit, or a substantive comment/review from someone else). Without
this, a PR that stays open week after week would get the exact same review
and ping comments reposted every single run, notification-storming everyone
on the thread for no reason.

This module is pure/deterministic - it only reads timestamps and marker text
out of already-fetched comment/review data - so it lives here, hermetically
tested, rather than being left to per-run agent judgment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

RECOMMENT_COOLDOWN_DAYS = 21

# Every comment this job posts ends with one of these hidden HTML-comment
# markers (invisible in rendered Markdown) so a later run can identify its
# own past comments and, for the review comment, which head commit it
# reviewed. REVIEW_MARKER_PREFIX is a prefix because it's followed by the
# actual sha and a closing "-->".
REVIEW_MARKER_PREFIX = "<!-- job-ops-sop-pr-review:review head_sha="
AUTHOR_PING_MARKER = "<!-- job-ops-sop-pr-review:author-ping -->"
HOLD_PING_MARKER = "<!-- job-ops-sop-pr-review:hold-ping -->"


def _is_bot(user: Dict[str, Any]) -> bool:
    if user.get("type") == "Bot":
        return True
    return user.get("login", "").endswith("[bot]")


def _parse_timestamp(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _item_timestamp(item: Dict[str, Any]) -> Optional[datetime]:
    # Issue comments and review comments use created_at; PR reviews use
    # submitted_at instead (and a pending/dismissed review may have none).
    return _parse_timestamp(item.get("created_at") or item.get("submitted_at") or "")


def find_last_self_comment(
    comments: Sequence[Dict[str, Any]], self_login: str, marker: str
) -> Optional[Dict[str, Any]]:
    """The most recent (by created_at) comment authored by self_login whose
    body contains `marker`, or None if this job never posted one."""
    matches = [
        c for c in comments
        if (c.get("user") or {}).get("login", "").lower() == (self_login or "").lower()
        and marker in (c.get("body") or "")
    ]
    if not matches:
        return None
    return max(matches, key=lambda c: c.get("created_at") or "")


def extract_reviewed_head_sha(comment_body: str) -> Optional[str]:
    idx = comment_body.find(REVIEW_MARKER_PREFIX)
    if idx == -1:
        return None
    start = idx + len(REVIEW_MARKER_PREFIX)
    end = comment_body.find("-->", start)
    if end == -1:
        return None
    return comment_body[start:end].strip()


def has_new_human_activity_since(
    comments: Sequence[Dict[str, Any]],
    reviews: Sequence[Dict[str, Any]],
    review_comments: Sequence[Dict[str, Any]],
    since: datetime,
    self_login: str,
) -> bool:
    """True if anyone other than this job's own account - and other bots -
    commented or reviewed strictly after `since`."""
    for item in list(comments) + list(reviews) + list(review_comments):
        user = item.get("user") or {}
        login = user.get("login", "")
        if login.lower() == (self_login or "").lower():
            continue
        if _is_bot(user):
            continue
        ts = _item_timestamp(item)
        if ts is not None and ts > since:
            return True
    return False


def should_skip_main_review(
    comments: Sequence[Dict[str, Any]],
    reviews: Sequence[Dict[str, Any]],
    review_comments: Sequence[Dict[str, Any]],
    self_login: str,
    current_head_sha: str,
    now: datetime,
    cooldown_days: float = RECOMMENT_COOLDOWN_DAYS,
) -> bool:
    """True -> the main review comment should NOT be posted this run: this
    job already reviewed this exact head commit within the cooldown window
    and nobody else has weighed in since."""
    last = find_last_self_comment(comments, self_login, REVIEW_MARKER_PREFIX)
    if last is None:
        return False
    last_ts = _item_timestamp(last)
    if last_ts is None:
        return False
    if (now - last_ts).total_seconds() / 86400.0 >= cooldown_days:
        return False
    if extract_reviewed_head_sha(last.get("body") or "") != current_head_sha:
        return False  # a new commit landed since that review
    if has_new_human_activity_since(comments, reviews, review_comments, last_ts, self_login):
        return False
    return True


def should_skip_ping(
    comments: Sequence[Dict[str, Any]],
    self_login: str,
    marker: str,
    new_ping_text: Optional[str],
    now: datetime,
    cooldown_days: float = RECOMMENT_COOLDOWN_DAYS,
) -> bool:
    """True -> don't (re)post this ping: this job already posted the exact
    same ping within the cooldown window. A ping whose wording changed (a
    new condition started applying, a different check is now failing, etc.)
    is posted regardless of cooldown - that wording change IS the material
    change the cooldown is meant to let through."""
    if not new_ping_text:
        return True
    last = find_last_self_comment(comments, self_login, marker)
    if last is None:
        return False
    last_ts = _item_timestamp(last)
    if last_ts is None:
        return False
    if (now - last_ts).total_seconds() / 86400.0 >= cooldown_days:
        return False
    return (last.get("body") or "").strip() == new_ping_text.strip()
