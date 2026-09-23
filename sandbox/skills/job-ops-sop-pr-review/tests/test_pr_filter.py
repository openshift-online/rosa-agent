"""Unit tests for ops_sop_pr_review.pr_filter."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ops_sop_pr_review.pr_filter import (
    DO_NOT_MERGE_HOLD_LABEL,
    NEEDS_REBASE_LABEL,
    REASON_ELIGIBLE,
    REASON_SELF_AUTHORED,
    REASON_TOO_NEW,
    REASON_WIP_HOLD,
    WORK_IN_PROGRESS_HOLD_LABEL,
    age_days,
    check_eligibility,
    has_label,
    is_stale,
)

NOW = datetime(2026, 9, 23, 0, 0, 0, tzinfo=timezone.utc)


def make_pr(created_at="2026-08-01T00:00:00Z", user="alice", labels=None):
    return {
        "number": 1,
        "created_at": created_at,
        "user": {"login": user},
        "labels": [{"name": name} for name in (labels or [])],
    }


class AgeDaysTests(unittest.TestCase):
    def test_computes_fractional_days(self):
        pr_created = "2026-09-13T00:00:00Z"
        self.assertAlmostEqual(age_days(pr_created, NOW), 10.0, places=6)

    def test_zero_for_just_created(self):
        self.assertAlmostEqual(age_days("2026-09-23T00:00:00Z", NOW), 0.0, places=6)


class HasLabelTests(unittest.TestCase):
    def test_present(self):
        pr = make_pr(labels=[NEEDS_REBASE_LABEL])
        self.assertTrue(has_label(pr, NEEDS_REBASE_LABEL))

    def test_absent(self):
        pr = make_pr(labels=[])
        self.assertFalse(has_label(pr, NEEDS_REBASE_LABEL))

    def test_missing_labels_key(self):
        pr = {"created_at": "2026-08-01T00:00:00Z", "user": {"login": "alice"}}
        self.assertFalse(has_label(pr, NEEDS_REBASE_LABEL))


class CheckEligibilityTests(unittest.TestCase):
    def test_eligible_when_old_enough_and_unlabeled(self):
        pr = make_pr(created_at="2026-08-01T00:00:00Z")
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason, REASON_ELIGIBLE)

    def test_self_authored_excluded_regardless_of_age_or_labels(self):
        pr = make_pr(created_at="2020-01-01T00:00:00Z", user="rosa-agent")
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, REASON_SELF_AUTHORED)

    def test_self_authored_check_is_case_insensitive(self):
        pr = make_pr(created_at="2020-01-01T00:00:00Z", user="Rosa-Agent")
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertEqual(result.reason, REASON_SELF_AUTHORED)

    def test_work_in_progress_hold_excluded(self):
        pr = make_pr(created_at="2020-01-01T00:00:00Z", labels=[WORK_IN_PROGRESS_HOLD_LABEL])
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, REASON_WIP_HOLD)

    def test_too_new_excluded(self):
        pr = make_pr(created_at="2026-09-20T00:00:00Z")  # 3 days old
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, REASON_TOO_NEW)

    def test_exactly_14_days_is_not_yet_eligible(self):
        pr = make_pr(created_at="2026-09-09T00:00:00Z")  # exactly 14 days
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertFalse(result.eligible)

    def test_just_over_14_days_is_eligible(self):
        pr = make_pr(created_at="2026-09-08T23:00:00Z")  # 14 days + 1 hour
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertTrue(result.eligible)

    def test_do_not_merge_hold_alone_does_not_block_eligibility(self):
        pr = make_pr(created_at="2026-08-01T00:00:00Z", labels=[DO_NOT_MERGE_HOLD_LABEL])
        result = check_eligibility(pr, "rosa-agent", NOW)
        self.assertTrue(result.eligible)


class IsStaleTests(unittest.TestCase):
    def test_stale_over_90_days(self):
        pr = make_pr(created_at="2026-01-01T00:00:00Z")
        self.assertTrue(is_stale(pr, NOW))

    def test_not_stale_under_90_days(self):
        pr = make_pr(created_at="2026-08-01T00:00:00Z")
        self.assertFalse(is_stale(pr, NOW))


if __name__ == "__main__":
    unittest.main()
