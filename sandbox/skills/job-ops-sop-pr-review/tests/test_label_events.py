"""Unit tests for ops_sop_pr_review.label_events."""

from __future__ import annotations

import unittest

from ops_sop_pr_review.label_events import find_label_adder


class FindLabelAdderTests(unittest.TestCase):
    def test_finds_single_labeling_event(self):
        events = [
            {"event": "commented", "actor": {"login": "someone"}},
            {"event": "labeled", "label": {"name": "do-not-merge/hold"}, "actor": {"login": "maintainer1"}},
        ]
        self.assertEqual(find_label_adder(events, "do-not-merge/hold"), "maintainer1")

    def test_ignores_other_labels(self):
        events = [
            {"event": "labeled", "label": {"name": "needs-rebase"}, "actor": {"login": "bot"}},
        ]
        self.assertIsNone(find_label_adder(events, "do-not-merge/hold"))

    def test_most_recent_relabel_wins(self):
        events = [
            {"event": "labeled", "label": {"name": "do-not-merge/hold"}, "actor": {"login": "maintainer1"}},
            {"event": "unlabeled", "label": {"name": "do-not-merge/hold"}, "actor": {"login": "maintainer1"}},
            {"event": "labeled", "label": {"name": "do-not-merge/hold"}, "actor": {"login": "maintainer2"}},
        ]
        self.assertEqual(find_label_adder(events, "do-not-merge/hold"), "maintainer2")

    def test_no_events_returns_none(self):
        self.assertIsNone(find_label_adder([], "do-not-merge/hold"))

    def test_missing_actor_login_is_skipped(self):
        events = [
            {"event": "labeled", "label": {"name": "do-not-merge/hold"}, "actor": {}},
        ]
        self.assertIsNone(find_label_adder(events, "do-not-merge/hold"))


if __name__ == "__main__":
    unittest.main()
