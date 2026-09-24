"""Unit tests for ops_sop_pr_review.recomment_guard."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ops_sop_pr_review.recomment_guard import (
    AUTHOR_PING_MARKER,
    REVIEW_MARKER_PREFIX,
    extract_reviewed_head_sha,
    find_last_self_comment,
    has_new_human_activity_since,
    should_skip_main_review,
    should_skip_ping,
)

NOW = datetime(2026, 9, 23, 0, 0, 0, tzinfo=timezone.utc)


class FindLastSelfCommentTests(unittest.TestCase):
    def test_ignores_other_authors_and_missing_marker(self):
        comments = [
            {"user": {"login": "bob"}, "created_at": "2026-09-01T00:00:00Z", "body": AUTHOR_PING_MARKER},
            {"user": {"login": "rosa-agent"}, "created_at": "2026-09-02T00:00:00Z", "body": "no marker here"},
        ]
        self.assertIsNone(find_last_self_comment(comments, "rosa-agent", AUTHOR_PING_MARKER))

    def test_picks_most_recent_match(self):
        comments = [
            {"user": {"login": "rosa-agent"}, "created_at": "2026-09-01T00:00:00Z", "body": "old " + AUTHOR_PING_MARKER},
            {"user": {"login": "rosa-agent"}, "created_at": "2026-09-10T00:00:00Z", "body": "new " + AUTHOR_PING_MARKER},
        ]
        last = find_last_self_comment(comments, "rosa-agent", AUTHOR_PING_MARKER)
        self.assertEqual(last["body"], "new " + AUTHOR_PING_MARKER)

    def test_case_insensitive_login_match(self):
        comments = [{"user": {"login": "Rosa-Agent"}, "created_at": "2026-09-01T00:00:00Z", "body": AUTHOR_PING_MARKER}]
        self.assertIsNotNone(find_last_self_comment(comments, "rosa-agent", AUTHOR_PING_MARKER))


class ExtractReviewedHeadShaTests(unittest.TestCase):
    def test_extracts_sha(self):
        body = f"Approve.\n\n{REVIEW_MARKER_PREFIX}abc123 -->"
        self.assertEqual(extract_reviewed_head_sha(body), "abc123")

    def test_none_when_marker_absent(self):
        self.assertIsNone(extract_reviewed_head_sha("no marker here"))


class HasNewHumanActivitySinceTests(unittest.TestCase):
    SINCE = datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc)

    def test_true_for_human_comment_after_since(self):
        comments = [{"user": {"login": "bob"}, "created_at": "2026-09-15T00:00:00Z"}]
        self.assertTrue(has_new_human_activity_since(comments, [], [], self.SINCE, "rosa-agent"))

    def test_false_for_comment_before_since(self):
        comments = [{"user": {"login": "bob"}, "created_at": "2026-09-05T00:00:00Z"}]
        self.assertFalse(has_new_human_activity_since(comments, [], [], self.SINCE, "rosa-agent"))

    def test_false_for_self_comment_after_since(self):
        comments = [{"user": {"login": "rosa-agent"}, "created_at": "2026-09-15T00:00:00Z"}]
        self.assertFalse(has_new_human_activity_since(comments, [], [], self.SINCE, "rosa-agent"))

    def test_false_for_bot_comment_after_since(self):
        comments = [{"user": {"login": "coderabbitai[bot]"}, "created_at": "2026-09-15T00:00:00Z"}]
        self.assertFalse(has_new_human_activity_since(comments, [], [], self.SINCE, "rosa-agent"))

    def test_reviews_use_submitted_at(self):
        reviews = [{"user": {"login": "bob"}, "submitted_at": "2026-09-15T00:00:00Z"}]
        self.assertTrue(has_new_human_activity_since([], reviews, [], self.SINCE, "rosa-agent"))


class ShouldSkipMainReviewTests(unittest.TestCase):
    def test_no_prior_review_never_skips(self):
        self.assertFalse(should_skip_main_review([], [], [], "rosa-agent", "abc123", NOW))

    def test_recent_matching_sha_no_new_activity_skips(self):
        comments = [{
            "user": {"login": "rosa-agent"},
            "created_at": "2026-09-10T00:00:00Z",
            "body": f"Approve.\n\n{REVIEW_MARKER_PREFIX}abc123 -->",
        }]
        self.assertTrue(should_skip_main_review(comments, [], [], "rosa-agent", "abc123", NOW))

    def test_sha_changed_does_not_skip(self):
        comments = [{
            "user": {"login": "rosa-agent"},
            "created_at": "2026-09-10T00:00:00Z",
            "body": f"Approve.\n\n{REVIEW_MARKER_PREFIX}abc123 -->",
        }]
        self.assertFalse(should_skip_main_review(comments, [], [], "rosa-agent", "def456", NOW))

    def test_new_human_comment_does_not_skip(self):
        comments = [
            {
                "user": {"login": "rosa-agent"},
                "created_at": "2026-09-10T00:00:00Z",
                "body": f"Approve.\n\n{REVIEW_MARKER_PREFIX}abc123 -->",
            },
            {"user": {"login": "bob"}, "created_at": "2026-09-15T00:00:00Z", "body": "wait, reconsider"},
        ]
        self.assertFalse(should_skip_main_review(comments, [], [], "rosa-agent", "abc123", NOW))

    def test_cooldown_expired_does_not_skip(self):
        comments = [{
            "user": {"login": "rosa-agent"},
            "created_at": "2026-08-01T00:00:00Z",  # more than 21 days before NOW
            "body": f"Approve.\n\n{REVIEW_MARKER_PREFIX}abc123 -->",
        }]
        self.assertFalse(should_skip_main_review(comments, [], [], "rosa-agent", "abc123", NOW))


class ShouldSkipPingTests(unittest.TestCase):
    def test_no_new_ping_text_skips(self):
        self.assertTrue(should_skip_ping([], "rosa-agent", AUTHOR_PING_MARKER, None, NOW))

    def test_no_prior_ping_never_skips(self):
        self.assertFalse(should_skip_ping([], "rosa-agent", AUTHOR_PING_MARKER, "@alice ping", NOW))

    def test_recent_identical_ping_skips(self):
        comments = [{
            "user": {"login": "rosa-agent"},
            "created_at": "2026-09-10T00:00:00Z",
            "body": "@alice ping " + AUTHOR_PING_MARKER,
        }]
        self.assertTrue(should_skip_ping(comments, "rosa-agent", AUTHOR_PING_MARKER, "@alice ping " + AUTHOR_PING_MARKER, NOW))

    def test_recent_but_different_ping_does_not_skip(self):
        comments = [{
            "user": {"login": "rosa-agent"},
            "created_at": "2026-09-10T00:00:00Z",
            "body": "@alice old ping " + AUTHOR_PING_MARKER,
        }]
        self.assertFalse(should_skip_ping(comments, "rosa-agent", AUTHOR_PING_MARKER, "@alice new ping " + AUTHOR_PING_MARKER, NOW))

    def test_cooldown_expired_does_not_skip(self):
        comments = [{
            "user": {"login": "rosa-agent"},
            "created_at": "2026-08-01T00:00:00Z",
            "body": "@alice ping " + AUTHOR_PING_MARKER,
        }]
        self.assertFalse(should_skip_ping(comments, "rosa-agent", AUTHOR_PING_MARKER, "@alice ping " + AUTHOR_PING_MARKER, NOW))


if __name__ == "__main__":
    unittest.main()
