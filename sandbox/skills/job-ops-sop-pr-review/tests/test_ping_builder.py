"""Unit tests for ops_sop_pr_review.ping_builder."""

from __future__ import annotations

import unittest

from ops_sop_pr_review.ping_builder import build_consolidated_author_ping, build_hold_ping


class BuildConsolidatedAuthorPingTests(unittest.TestCase):
    def test_none_when_nothing_applies(self):
        result = build_consolidated_author_ping(
            author_login="alice",
            needs_rebase=False,
            mergeable_state=None,
            stale=False,
            stale_reviewer_logins=[],
            failing_checks=[],
        )
        self.assertIsNone(result)

    def test_rebase_only(self):
        result = build_consolidated_author_ping(
            author_login="alice",
            needs_rebase=True,
            mergeable_state="dirty",
            stale=False,
            stale_reviewer_logins=[],
            failing_checks=[],
        )
        self.assertIn("@alice", result)
        self.assertIn("needs-rebase", result)
        self.assertIn("dirty", result)
        self.assertNotIn("stale", result.lower())
        self.assertNotIn("CI is currently failing", result)

    def test_failing_checks_only(self):
        result = build_consolidated_author_ping(
            author_login="alice",
            needs_rebase=False,
            mergeable_state=None,
            stale=False,
            stale_reviewer_logins=[],
            failing_checks=["unit-tests", "lint"],
        )
        self.assertIn("`unit-tests`", result)
        self.assertIn("`lint`", result)
        self.assertIn("tide", result.lower())

    def test_stale_only_cc_s_reviewers(self):
        result = build_consolidated_author_ping(
            author_login="alice",
            needs_rebase=False,
            mergeable_state=None,
            stale=True,
            stale_reviewer_logins=["bob", "carol"],
            failing_checks=[],
        )
        self.assertIn("@alice", result)
        self.assertIn("@bob", result)
        self.assertIn("@carol", result)
        self.assertIn("stale", result.lower())

    def test_stale_reviewers_excludes_author_duplicate(self):
        result = build_consolidated_author_ping(
            author_login="alice",
            needs_rebase=False,
            mergeable_state=None,
            stale=True,
            stale_reviewer_logins=["alice", "bob"],
            failing_checks=[],
        )
        self.assertEqual(result.count("@alice"), 1)

    def test_all_three_bundle_into_one_comment_single_mention_line(self):
        result = build_consolidated_author_ping(
            author_login="alice",
            needs_rebase=True,
            mergeable_state="dirty",
            stale=True,
            stale_reviewer_logins=["bob"],
            failing_checks=["unit-tests"],
        )
        # Bundled into a single string, not multiple - the caller posts this
        # exactly once.
        self.assertIn("needs-rebase", result)
        self.assertIn("`unit-tests`", result)
        self.assertIn("stale", result.lower())
        self.assertEqual(result.count("@alice"), 1)


class BuildHoldPingTests(unittest.TestCase):
    def test_mentions_label_adder(self):
        result = build_hold_ping("maintainer1")
        self.assertIn("@maintainer1", result)
        self.assertIn("do-not-merge/hold", result)


if __name__ == "__main__":
    unittest.main()
