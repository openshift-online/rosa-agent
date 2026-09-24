"""Unit tests for ops_sop_pr_review.github_client.

All process execution is mocked at the subprocess.run boundary - no real gh
or network calls happen here, matching this package's own hermetic-test
requirement.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from ops_sop_pr_review.github_client import GhApiError, GithubClient, parse_paginated_json


def _completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


class GithubClientGetJsonTests(unittest.TestCase):
    def setUp(self):
        self.client = GithubClient()

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_get_json_returns_parsed_object(self, mock_run):
        mock_run.return_value = _completed('{"number": 42}')
        result = self.client.get_json("repos/o/r/pulls/42")
        self.assertEqual(result, {"number": 42})

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_get_json_builds_query_string(self, mock_run):
        mock_run.return_value = _completed("{}")
        self.client.get_json("repos/o/r/pulls", params={"state": "open", "per_page": "100"})
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("repos/o/r/pulls?state=open&per_page=100", called_cmd)

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_nonzero_exit_raises_with_stderr(self, mock_run):
        mock_run.return_value = _completed("", returncode=1, stderr="HTTP 404: Not Found")
        with self.assertRaises(GhApiError) as ctx:
            self.client.get_json("repos/o/r/pulls/999")
        self.assertIn("404", str(ctx.exception))
        self.assertEqual(ctx.exception.exit_code, 1)

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_invalid_json_raises(self, mock_run):
        mock_run.return_value = _completed("not json")
        with self.assertRaises(GhApiError):
            self.client.get_json("repos/o/r/pulls/1")

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_os_error_running_gh_raises(self, mock_run):
        mock_run.side_effect = OSError("no such file")
        with self.assertRaises(GhApiError):
            self.client.get_json("repos/o/r/pulls/1")

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gh"], timeout=60)
        with self.assertRaises(GhApiError):
            self.client.get_json("repos/o/r/pulls/1")


class GithubClientGetPaginatedTests(unittest.TestCase):
    def setUp(self):
        self.client = GithubClient()

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_single_page_array(self, mock_run):
        mock_run.return_value = _completed('[{"number": 1}, {"number": 2}]')
        result = self.client.get_paginated("repos/o/r/pulls")
        self.assertEqual(result, [{"number": 1}, {"number": 2}])

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_passes_paginate_flag(self, mock_run):
        mock_run.return_value = _completed("[]")
        self.client.get_paginated("repos/o/r/pulls")
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("--paginate", called_cmd)

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_multiple_concatenated_pages_are_flattened(self, mock_run):
        mock_run.return_value = _completed('[{"number": 1}][{"number": 2}, {"number": 3}]')
        result = self.client.get_paginated("repos/o/r/pulls")
        self.assertEqual(result, [{"number": 1}, {"number": 2}, {"number": 3}])

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_empty_output_returns_empty_list(self, mock_run):
        mock_run.return_value = _completed("")
        result = self.client.get_paginated("repos/o/r/pulls")
        self.assertEqual(result, [])

    @patch("ops_sop_pr_review.github_client.subprocess.run")
    def test_nonzero_exit_raises(self, mock_run):
        mock_run.return_value = _completed("", returncode=1, stderr="policy_denied")
        with self.assertRaises(GhApiError):
            self.client.get_paginated("repos/o/r/pulls")


class ParsePaginatedJsonTests(unittest.TestCase):
    def test_single_object_pages_are_appended_not_flattened(self):
        # Non-list pages (e.g. a single-object endpoint paginated by mistake)
        # are appended as-is rather than iterated key-by-key.
        result = parse_paginated_json('{"a": 1}{"b": 2}', "source")
        self.assertEqual(result, [{"a": 1}, {"b": 2}])

    def test_malformed_json_raises(self):
        with self.assertRaises(GhApiError):
            parse_paginated_json("[1, 2", "source")


if __name__ == "__main__":
    unittest.main()
