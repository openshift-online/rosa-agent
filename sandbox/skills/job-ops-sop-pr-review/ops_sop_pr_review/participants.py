"""Collect distinct human commenters/reviewers, for the stale-PR cc list."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def _is_bot(user: Dict[str, Any]) -> bool:
    if user.get("type") == "Bot":
        return True
    return user.get("login", "").endswith("[bot]")


def distinct_human_participants(
    comments: Iterable[Dict[str, Any]],
    reviews: Iterable[Dict[str, Any]],
    review_comments: Iterable[Dict[str, Any]],
    exclude_logins: Iterable[str],
) -> List[str]:
    """Distinct human (non-bot) logins that commented or reviewed, excluding
    `exclude_logins` (case-insensitive) - typically this job's own account
    and the PR author (who is pinged separately as the author, not cc'd
    again as a "reviewer"). Preserves first-seen order.
    """
    excluded = {login.lower() for login in exclude_logins if login}
    seen: List[str] = []
    for item in list(comments) + list(reviews) + list(review_comments):
        user = item.get("user") or {}
        if _is_bot(user):
            continue
        login = user.get("login")
        if not login or login.lower() in excluded:
            continue
        if login not in seen:
            seen.append(login)
    return seen
