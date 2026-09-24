"""Deterministic templating for the two kinds of ping comment this job posts.

Per explicit task requirements: ALL author-directed pings (needs-rebase,
stale/>3 months, failing-CI) are bundled into ONE comment so the author is
never notification-stormed by several separate pings in one run. The
do-not-merge/hold ping is different in kind - it targets whoever applied
that label (who may not be the author) - and always stays its own, separate
comment.
"""

from __future__ import annotations

from typing import List, Optional

from .recomment_guard import AUTHOR_PING_MARKER, HOLD_PING_MARKER

# Each footer ends with a hidden HTML-comment marker (invisible when
# rendered) identifying this exact comment as "this job's author ping" /
# "this job's hold ping" - see recomment_guard - so a later run can find its
# own most recent ping of each kind and compare it against the freshly
# computed one instead of blindly reposting every run.
_FOOTER = (
    "\n\n---\n*Automated ping from the scheduled `job-ops-sop-pr-review` job "
    "(all applicable author notices are consolidated into this single "
    "comment to avoid notification-storming).*\n" + AUTHOR_PING_MARKER
)

_HOLD_FOOTER = (
    "\n\n---\n*Automated ping from the scheduled `job-ops-sop-pr-review` job.*\n"
    + HOLD_PING_MARKER
)


def build_consolidated_author_ping(
    author_login: str,
    needs_rebase: bool,
    mergeable_state: Optional[str],
    stale: bool,
    stale_reviewer_logins: List[str],
    failing_checks: List[str],
) -> Optional[str]:
    """Return one bundled comment body covering whichever conditions apply,
    or None if none apply (in which case no comment should be posted)."""
    bullets = []

    if needs_rebase:
        state_note = f" (`mergeable_state` is currently `{mergeable_state}`)" if mergeable_state else ""
        bullets.append(
            f"- This PR is labeled `needs-rebase`{state_note} - could you please "
            "rebase it onto the latest default branch when you get a chance?"
        )

    if failing_checks:
        checks_list = ", ".join(f"`{name}`" for name in failing_checks)
        bullets.append(
            f"- CI is currently failing (excluding tide's lgtm/approve status): "
            f"{checks_list}. Could you take a look?"
        )

    if stale:
        bullets.append(
            "- This PR is over 3 months old and may be stale. If there's no "
            "action within a week, it may be closed. Please update it or "
            "confirm it's still needed."
        )

    if not bullets:
        return None

    mentions = [f"@{author_login}"]
    if stale:
        mentions.extend(f"@{login}" for login in stale_reviewer_logins if login != author_login)

    return " ".join(mentions) + "\n\n" + "\n".join(bullets) + _FOOTER


def build_hold_ping(label_adder_login: str) -> str:
    return (
        f"@{label_adder_login} this PR is on hold (`do-not-merge/hold`) - "
        "could you re-review whether the hold should stay in place?" + _HOLD_FOOTER
    )
