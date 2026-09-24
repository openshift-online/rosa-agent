"""Command-line interface for job-ops-sop-pr-review's deterministic worklist
builder. Emits JSON to stdout; does not post anything and does not judge PR
content - it only reads via `gh api` and computes flags/comment templates.
The invoking skill (an agent) is responsible for everything genuinely
non-deterministic: Section-5 source verification against upstream code,
crediting/assessing prior review discussion, writing the review prose, and
posting the comments this tool already drafted.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import List, Optional

from .github_client import DEFAULT_GH_BIN, GhApiError, GithubClient
from .worklist import build_worklist

PROG = "ops-sop-pr-review"

DESCRIPTION = (
    "Compute the deterministic worklist for job-ops-sop-pr-review: which "
    "open PRs on a repo are eligible for review (excluding self-authored "
    "and work-in-progress/hold PRs, and PRs younger than 14 days), plus the "
    "exact pre-templated ping comment body for whichever of needs-rebase / "
    "stale(>90d) / failing-CI conditions apply (bundled into one comment "
    "per PR), and a separate do-not-merge/hold re-review ping naming "
    "whoever applied that label. A 3-week recomment cooldown suppresses "
    "`post_main_review` and any ping this job already posted unchanged "
    "since its last run, unless a new commit or new human discussion "
    "makes it worth repeating."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    parser.add_argument(
        "--repo", required=True, metavar="OWNER/REPO",
        help="Repository to scan, e.g. openshift/ops-sop.",
    )
    parser.add_argument(
        "--self-login", required=True, metavar="LOGIN",
        help="This job's own GitHub login (e.g. from `gh api user --jq .login`), "
        "to exclude self-authored PRs.",
    )
    parser.add_argument(
        "--now", metavar="ISO8601",
        help="Override 'now' for age calculations, e.g. 2026-09-23T00:00:00Z "
        "(mainly for reproducible testing; omit to use the real current time).",
    )
    parser.add_argument(
        "--gh-bin", default=DEFAULT_GH_BIN, metavar="PATH",
        help=f"Path to the gh binary (default: {DEFAULT_GH_BIN}).",
    )
    return parser


def _parse_now(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = GithubClient(gh_bin=args.gh_bin)

    try:
        now = _parse_now(args.now)
    except ValueError as exc:
        parser.error(f"--now: {exc}")
        return 2  # unreachable, parser.error exits

    try:
        items = build_worklist(client, args.repo, args.self_login, now)
    except GhApiError as exc:
        # A failure enumerating PRs at all (not a single-PR failure, which
        # build_worklist already isolates) is the one case this CLI itself
        # cannot "try to work around" - report it and let the invoking
        # skill file the failure Issue.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps([item.to_dict() for item in items], indent=2))
    return 0


def main() -> None:
    sys.exit(run())
