"""Unit tests for ops_sop_pr_review.participants."""

from __future__ import annotations

import unittest

from ops_sop_pr_review.participants import distinct_human_participants


def user(login, is_bot=False):
    return {"user": {"login": login, "type": "Bot" if is_bot else "User"}}


class DistinctHumanParticipantsTests(unittest.TestCase):
    def test_collects_from_all_three_sources(self):
        comments = [user("alice")]
        reviews = [user("bob")]
        review_comments = [user("carol")]
        result = distinct_human_participants(comments, reviews, review_comments, exclude_logins=[])
        self.assertEqual(result, ["alice", "bob", "carol"])

    def test_deduplicates_preserving_first_seen_order(self):
        comments = [user("alice"), user("bob")]
        reviews = [user("alice")]
        result = distinct_human_participants(comments, reviews, [], exclude_logins=[])
        self.assertEqual(result, ["alice", "bob"])

    def test_excludes_bots_by_type(self):
        comments = [user("coderabbitai", is_bot=True), user("alice")]
        result = distinct_human_participants(comments, [], [], exclude_logins=[])
        self.assertEqual(result, ["alice"])

    def test_excludes_bots_by_login_suffix(self):
        comments = [{"user": {"login": "coderabbitai[bot]"}}, user("alice")]
        result = distinct_human_participants(comments, [], [], exclude_logins=[])
        self.assertEqual(result, ["alice"])

    def test_excludes_given_logins_case_insensitively(self):
        comments = [user("Rosa-Agent"), user("alice")]
        result = distinct_human_participants(comments, [], [], exclude_logins=["rosa-agent"])
        self.assertEqual(result, ["alice"])

    def test_missing_user_field_is_skipped(self):
        comments = [{"body": "no user field"}, user("alice")]
        result = distinct_human_participants(comments, [], [], exclude_logins=[])
        self.assertEqual(result, ["alice"])


if __name__ == "__main__":
    unittest.main()
