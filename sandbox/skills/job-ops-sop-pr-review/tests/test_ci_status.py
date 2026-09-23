"""Unit tests for ops_sop_pr_review.ci_status."""

from __future__ import annotations

import unittest

from ops_sop_pr_review.ci_status import failing_contexts, failing_check_runs, failing_legacy_statuses


class FailingCheckRunsTests(unittest.TestCase):
    def test_failure_conclusion_included(self):
        runs = [{"name": "unit-tests", "status": "completed", "conclusion": "failure"}]
        self.assertEqual(failing_check_runs(runs), ["unit-tests"])

    def test_success_conclusion_excluded(self):
        runs = [{"name": "unit-tests", "status": "completed", "conclusion": "success"}]
        self.assertEqual(failing_check_runs(runs), [])

    def test_in_progress_check_excluded(self):
        runs = [{"name": "unit-tests", "status": "in_progress", "conclusion": None}]
        self.assertEqual(failing_check_runs(runs), [])

    def test_tide_check_run_excluded_even_if_failing(self):
        runs = [{"name": "tide", "status": "completed", "conclusion": "failure"}]
        self.assertEqual(failing_check_runs(runs), [])

    def test_tide_prefixed_context_excluded(self):
        runs = [{"name": "Tide/lgtm", "status": "completed", "conclusion": "failure"}]
        self.assertEqual(failing_check_runs(runs), [])

    def test_timed_out_and_cancelled_and_action_required_all_count(self):
        runs = [
            {"name": "a", "status": "completed", "conclusion": "timed_out"},
            {"name": "b", "status": "completed", "conclusion": "cancelled"},
            {"name": "c", "status": "completed", "conclusion": "action_required"},
        ]
        self.assertEqual(failing_check_runs(runs), ["a", "b", "c"])


class FailingLegacyStatusesTests(unittest.TestCase):
    def test_failure_and_error_states_included(self):
        statuses = [
            {"context": "ci/prow/unit", "state": "failure"},
            {"context": "ci/prow/e2e", "state": "error"},
        ]
        self.assertEqual(failing_legacy_statuses(statuses), ["ci/prow/unit", "ci/prow/e2e"])

    def test_success_and_pending_excluded(self):
        statuses = [
            {"context": "ci/prow/unit", "state": "success"},
            {"context": "ci/prow/e2e", "state": "pending"},
        ]
        self.assertEqual(failing_legacy_statuses(statuses), [])

    def test_tide_context_excluded(self):
        statuses = [{"context": "tide", "state": "failure"}]
        self.assertEqual(failing_legacy_statuses(statuses), [])


class FailingContextsTests(unittest.TestCase):
    def test_union_and_dedup(self):
        runs = [{"name": "unit-tests", "status": "completed", "conclusion": "failure"}]
        statuses = [
            {"context": "unit-tests", "state": "failure"},
            {"context": "ci/prow/e2e", "state": "failure"},
        ]
        result = failing_contexts(runs, statuses)
        self.assertEqual(result, ["unit-tests", "ci/prow/e2e"])

    def test_empty_inputs_yield_empty(self):
        self.assertEqual(failing_contexts([], []), [])


if __name__ == "__main__":
    unittest.main()
