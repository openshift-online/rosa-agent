"""Unit tests for ops_sop_pr_review.worklist.

GithubClient itself is replaced with a hand-rolled fake (no subprocess/gh
involved at all), keeping these tests focused on orchestration logic.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ops_sop_pr_review.github_client import GhApiError
from ops_sop_pr_review.ping_builder import build_consolidated_author_ping
from ops_sop_pr_review.pr_filter import REASON_SELF_AUTHORED, REASON_TOO_NEW, REASON_WIP_HOLD
from ops_sop_pr_review.worklist import build_work_item, build_worklist

NOW = datetime(2026, 9, 23, 0, 0, 0, tzinfo=timezone.utc)


def make_pr_summary(number=1, created_at="2026-08-01T00:00:00Z", user="alice", labels=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "created_at": created_at,
        "user": {"login": user},
        "labels": [{"name": name} for name in (labels or [])],
    }


class FakeGithubClient:
    """Minimal stand-in: pre-seeded responses, keyed by exact path."""

    def __init__(self, json_responses=None, paginated_responses=None, raise_on=None):
        self.json_responses = json_responses or {}
        self.paginated_responses = paginated_responses or {}
        self.raise_on = raise_on or set()

    def get_json(self, path, params=None):
        if path in self.raise_on:
            raise GhApiError(f"boom: {path}")
        return self.json_responses[path]

    def get_paginated(self, path, params=None):
        if path in self.raise_on:
            raise GhApiError(f"boom: {path}")
        return self.paginated_responses.get(path, [])


class BuildWorkItemSkipTests(unittest.TestCase):
    def test_self_authored_skips_without_extra_calls(self):
        pr = make_pr_summary(user="rosa-agent")
        client = FakeGithubClient()  # no responses seeded - would KeyError if called
        item = build_work_item(client, "o/r", pr, "rosa-agent", NOW)
        self.assertFalse(item.eligible)
        self.assertEqual(item.skip_reason, REASON_SELF_AUTHORED)

    def test_wip_hold_skips(self):
        pr = make_pr_summary(labels=["work-in-progress/hold"])
        client = FakeGithubClient()
        item = build_work_item(client, "o/r", pr, "rosa-agent", NOW)
        self.assertEqual(item.skip_reason, REASON_WIP_HOLD)

    def test_too_new_skips(self):
        pr = make_pr_summary(created_at="2026-09-20T00:00:00Z")
        client = FakeGithubClient()
        item = build_work_item(client, "o/r", pr, "rosa-agent", NOW)
        self.assertEqual(item.skip_reason, REASON_TOO_NEW)


class BuildWorkItemEligibleTests(unittest.TestCase):
    def test_eligible_pr_fetches_detail_and_computes_flags(self):
        pr_summary = make_pr_summary(number=42, created_at="2026-08-01T00:00:00Z", labels=["needs-rebase"])
        detail = dict(pr_summary)
        detail.update({"mergeable_state": "dirty", "head": {"sha": "abc123"}})
        client = FakeGithubClient(
            json_responses={"repos/o/r/pulls/42": detail},
            paginated_responses={
                "repos/o/r/issues/42/comments": [],
                "repos/o/r/pulls/42/reviews": [],
                "repos/o/r/pulls/42/comments": [],
                "repos/o/r/commits/abc123/check-runs": [
                    {"name": "unit-tests", "status": "completed", "conclusion": "failure"}
                ],
                "repos/o/r/commits/abc123/statuses": [],
            },
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertTrue(item.eligible)
        self.assertTrue(item.needs_rebase)
        self.assertEqual(item.failing_checks, ["unit-tests"])
        self.assertIsNotNone(item.consolidated_author_ping)
        self.assertIn("needs-rebase", item.consolidated_author_ping)
        self.assertIn("unit-tests", item.consolidated_author_ping)
        self.assertIsNone(item.hold_ping)
        self.assertEqual(item.head_sha, "abc123")
        self.assertTrue(item.post_main_review)

    def test_do_not_merge_hold_produces_separate_hold_ping(self):
        pr_summary = make_pr_summary(number=7, created_at="2026-08-01T00:00:00Z", labels=["do-not-merge/hold"])
        detail = dict(pr_summary)
        detail.update({"mergeable_state": "clean", "head": {"sha": "deadbeef"}})
        client = FakeGithubClient(
            json_responses={"repos/o/r/pulls/7": detail},
            paginated_responses={
                "repos/o/r/issues/7/comments": [],
                "repos/o/r/pulls/7/reviews": [],
                "repos/o/r/pulls/7/comments": [],
                "repos/o/r/commits/deadbeef/check-runs": [],
                "repos/o/r/commits/deadbeef/statuses": [],
                "repos/o/r/issues/7/events": [
                    {"event": "labeled", "label": {"name": "do-not-merge/hold"}, "actor": {"login": "maintainer1"}}
                ],
            },
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertIsNone(item.consolidated_author_ping)
        self.assertEqual(
            item.hold_ping,
            "@maintainer1 this PR is on hold (`do-not-merge/hold`) - could you "
            "re-review whether the hold should stay in place?\n\n---\n*Automated "
            "ping from the scheduled `job-ops-sop-pr-review` job.*\n"
            "<!-- job-ops-sop-pr-review:hold-ping -->",
        )

    def test_stale_pr_collects_reviewer_logins_excluding_self_and_author(self):
        pr_summary = make_pr_summary(number=9, created_at="2026-01-01T00:00:00Z", user="alice")
        detail = dict(pr_summary)
        detail.update({"mergeable_state": "clean", "head": {"sha": "cafe"}})
        client = FakeGithubClient(
            json_responses={"repos/o/r/pulls/9": detail},
            paginated_responses={
                "repos/o/r/issues/9/comments": [{"user": {"login": "bob"}}],
                "repos/o/r/pulls/9/reviews": [{"user": {"login": "alice"}}, {"user": {"login": "rosa-agent"}}],
                "repos/o/r/pulls/9/comments": [],
                "repos/o/r/commits/cafe/check-runs": [],
                "repos/o/r/commits/cafe/statuses": [],
            },
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertTrue(item.stale)
        self.assertEqual(item.stale_reviewer_logins, ["bob"])


class RecommentCooldownTests(unittest.TestCase):
    """The job must not re-post a comment it already posted within the last
    3 weeks unless something material changed - a new commit (for the main
    review) or different wording (for a ping), or the cooldown itself has
    lapsed."""

    REVIEW_MARKER = "<!-- job-ops-sop-pr-review:review head_sha=abc123 -->"

    def _client(self, issue_comments, reviews=None, review_comments=None, labels=None, sha="abc123", mergeable_state="dirty"):
        pr_summary = make_pr_summary(number=5, created_at="2026-08-01T00:00:00Z", labels=labels or [])
        detail = dict(pr_summary)
        detail.update({"mergeable_state": mergeable_state, "head": {"sha": sha}})
        return pr_summary, FakeGithubClient(
            json_responses={"repos/o/r/pulls/5": detail},
            paginated_responses={
                "repos/o/r/issues/5/comments": issue_comments,
                "repos/o/r/pulls/5/reviews": reviews or [],
                "repos/o/r/pulls/5/comments": review_comments or [],
                f"repos/o/r/commits/{sha}/check-runs": [],
                f"repos/o/r/commits/{sha}/statuses": [],
            },
        )

    def test_recent_self_review_no_change_skips(self):
        pr_summary, client = self._client(
            issue_comments=[{
                "user": {"login": "rosa-agent"},
                "created_at": "2026-09-10T00:00:00Z",  # 13 days before NOW
                "body": "Approve.\n\n" + self.REVIEW_MARKER,
            }],
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertFalse(item.post_main_review)

    def test_recent_self_review_but_new_commit_reposts(self):
        pr_summary, client = self._client(
            issue_comments=[{
                "user": {"login": "rosa-agent"},
                "created_at": "2026-09-10T00:00:00Z",
                "body": "Approve.\n\n" + self.REVIEW_MARKER,  # sha abc123
            }],
            sha="def456",  # PR has since gained a new commit
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertTrue(item.post_main_review)

    def test_recent_self_review_but_new_human_comment_reposts(self):
        pr_summary, client = self._client(
            issue_comments=[
                {
                    "user": {"login": "rosa-agent"},
                    "created_at": "2026-09-10T00:00:00Z",
                    "body": "Approve.\n\n" + self.REVIEW_MARKER,
                },
                {
                    "user": {"login": "bob"},
                    "created_at": "2026-09-15T00:00:00Z",
                    "body": "Actually, can we reconsider this?",
                },
            ],
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertTrue(item.post_main_review)

    def test_recent_self_review_but_bot_comment_still_skips(self):
        pr_summary, client = self._client(
            issue_comments=[
                {
                    "user": {"login": "rosa-agent"},
                    "created_at": "2026-09-10T00:00:00Z",
                    "body": "Approve.\n\n" + self.REVIEW_MARKER,
                },
                {
                    "user": {"login": "coderabbitai[bot]"},
                    "created_at": "2026-09-15T00:00:00Z",
                    "body": "Automated nit.",
                },
            ],
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertFalse(item.post_main_review)

    def test_old_self_review_reposts_regardless(self):
        pr_summary, client = self._client(
            issue_comments=[{
                "user": {"login": "rosa-agent"},
                "created_at": "2026-08-20T00:00:00Z",  # 34 days before NOW
                "body": "Approve.\n\n" + self.REVIEW_MARKER,
            }],
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertTrue(item.post_main_review)

    def test_recent_identical_ping_suppressed(self):
        expected_ping = build_consolidated_author_ping(
            author_login="alice", needs_rebase=True, mergeable_state="dirty",
            stale=False, stale_reviewer_logins=[], failing_checks=[],
        )
        pr_summary, client = self._client(
            issue_comments=[{
                "user": {"login": "rosa-agent"},
                "created_at": "2026-09-10T00:00:00Z",
                "body": expected_ping,
            }],
            labels=["needs-rebase"],
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertIsNone(item.consolidated_author_ping)

    def test_recent_but_changed_ping_reposts(self):
        stale_wording_ping = (
            "@alice\n\n- an older/different ping body\n\n---\n"
            "*Automated ping from the scheduled `job-ops-sop-pr-review` job "
            "(all applicable author notices are consolidated into this single "
            "comment to avoid notification-storming).*\n"
            "<!-- job-ops-sop-pr-review:author-ping -->"
        )
        pr_summary, client = self._client(
            issue_comments=[{
                "user": {"login": "rosa-agent"},
                "created_at": "2026-09-10T00:00:00Z",
                "body": stale_wording_ping,
            }],
            labels=["needs-rebase"],
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertIsNotNone(item.consolidated_author_ping)

    def test_old_identical_ping_reposts_regardless(self):
        expected_ping = build_consolidated_author_ping(
            author_login="alice", needs_rebase=True, mergeable_state="dirty",
            stale=False, stale_reviewer_logins=[], failing_checks=[],
        )
        pr_summary, client = self._client(
            issue_comments=[{
                "user": {"login": "rosa-agent"},
                "created_at": "2026-08-20T00:00:00Z",  # 34 days before NOW
                "body": expected_ping,
            }],
            labels=["needs-rebase"],
        )
        item = build_work_item(client, "o/r", pr_summary, "rosa-agent", NOW)
        self.assertIsNotNone(item.consolidated_author_ping)


class BuildWorklistTests(unittest.TestCase):
    def test_isolates_per_pr_failure_and_continues(self):
        good_summary = make_pr_summary(number=1, created_at="2026-08-01T00:00:00Z")
        bad_summary = make_pr_summary(number=2, created_at="2026-08-01T00:00:00Z")
        detail = dict(good_summary)
        detail.update({"mergeable_state": "clean", "head": {"sha": "abc"}})
        client = FakeGithubClient(
            json_responses={"repos/o/r/pulls/1": detail},
            paginated_responses={
                "repos/o/r/pulls": [good_summary, bad_summary],
                "repos/o/r/issues/1/comments": [],
                "repos/o/r/pulls/1/reviews": [],
                "repos/o/r/pulls/1/comments": [],
                "repos/o/r/commits/abc/check-runs": [],
                "repos/o/r/commits/abc/statuses": [],
            },
            raise_on={"repos/o/r/pulls/2"},
        )
        items = build_worklist(client, "o/r", "rosa-agent", NOW)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].eligible)
        self.assertIsNone(items[0].error)
        self.assertFalse(items[1].eligible)
        self.assertEqual(items[1].skip_reason, "error")
        self.assertIn("boom", items[1].error)


if __name__ == "__main__":
    unittest.main()
