"""Unit tests for job_image_vuln_check.cli.

Fully hermetic: build_targets and run_quay_vuln_report are always mocked,
and --output goes through the injected `write_text` callable - nothing here
touches a real file, process, or network call.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from job_image_vuln_check import cli
from job_image_vuln_check.quay_report import QuayVulnReportError
from job_image_vuln_check.targets import Target


def _report(count=0):
    return {"vulnerability_count": count, "vulnerabilities": [{"cve": f"CVE-{i}"} for i in range(count)]}


class RunTests(unittest.TestCase):
    @patch("job_image_vuln_check.cli.run_quay_vuln_report")
    @patch("job_image_vuln_check.cli.build_targets")
    def test_single_target_prints_json_array_with_source_repository(self, mock_build_targets, mock_run):
        mock_build_targets.return_value = [Target(image_ref="quay.io/a/b:latest", repository="org/b-src")]
        mock_run.return_value = _report(2)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = cli.run(["--image", "quay.io/a/b:latest", "--repository", "org/b-src"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["source_repository"], "org/b-src")
        self.assertEqual(payload[0]["vulnerability_count"], 2)

    @patch("job_image_vuln_check.cli.run_quay_vuln_report")
    @patch("job_image_vuln_check.cli.build_targets")
    def test_unresolved_repository_adds_a_note(self, mock_build_targets, mock_run):
        mock_build_targets.return_value = [Target(image_ref="quay.io/a/b:latest", repository=None)]
        mock_run.return_value = _report(0)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.run(["--image", "quay.io/a/b:latest"])
        payload = json.loads(buf.getvalue())
        self.assertIsNone(payload[0]["source_repository"])
        self.assertIn("note", payload[0])

    @patch("job_image_vuln_check.cli.run_quay_vuln_report")
    @patch("job_image_vuln_check.cli.build_targets")
    def test_unresolved_repository_note_includes_resolution_error(self, mock_build_targets, mock_run):
        mock_build_targets.return_value = [
            Target(image_ref="quay.io/a/b:latest", repository=None, resolution_error="skopeo inspect exited 1: Forbidden")
        ]
        mock_run.return_value = _report(0)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.run(["--image", "quay.io/a/b:latest"])
        payload = json.loads(buf.getvalue())
        self.assertIn("Forbidden", payload[0]["note"])

    @patch("job_image_vuln_check.cli.run_quay_vuln_report")
    @patch("job_image_vuln_check.cli.build_targets")
    def test_multiple_targets_all_included(self, mock_build_targets, mock_run):
        mock_build_targets.return_value = [
            Target(image_ref="quay.io/a/b:latest", repository="org/b-src"),
            Target(image_ref="quay.io/c/d:latest", repository="org/d-src"),
        ]
        mock_run.side_effect = [_report(1), _report(0)]

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.run(["--targets", '{"quay.io/a/b:latest": "org/b-src", "quay.io/c/d:latest": "org/d-src"}'])
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload), 2)

    @patch("job_image_vuln_check.cli.run_quay_vuln_report")
    @patch("job_image_vuln_check.cli.build_targets")
    def test_one_bad_target_does_not_discard_others(self, mock_build_targets, mock_run):
        # The core batch-robustness guarantee: a nightly run over several
        # images must not lose every other target's data because one entry
        # was bad.
        mock_build_targets.return_value = [
            Target(image_ref="quay.io/a/b:latest", repository="org/b-src"),
            Target(image_ref="quay.io/bad/c:latest", repository="org/c-src"),
            Target(image_ref="quay.io/a/d:latest", repository="org/d-src"),
        ]
        mock_run.side_effect = [_report(1), QuayVulnReportError("boom"), _report(0)]

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err):
            exit_code = cli.run(["--targets", "{}"])
        self.assertEqual(exit_code, 1)
        payload = json.loads(buf_out.getvalue())
        self.assertEqual(len(payload), 3)
        self.assertEqual(payload[0]["vulnerability_count"], 1)
        self.assertEqual(payload[1]["error"], "boom")
        self.assertEqual(payload[1]["image_ref"], "quay.io/bad/c:latest")
        self.assertNotIn("vulnerability_count", payload[1])
        self.assertEqual(payload[2]["vulnerability_count"], 0)
        self.assertIn("boom", buf_err.getvalue())

    @patch("job_image_vuln_check.cli.run_quay_vuln_report")
    @patch("job_image_vuln_check.cli.build_targets")
    def test_output_file_uses_injected_writer_not_real_disk(self, mock_build_targets, mock_run):
        mock_build_targets.return_value = [Target(image_ref="quay.io/a/b:latest", repository="org/b-src")]
        mock_run.return_value = _report(0)

        written = {}

        def fake_write_text(path, content):
            written["path"] = path
            written["content"] = content

        exit_code = cli.run(
            ["--image", "quay.io/a/b:latest", "--repository", "org/b-src", "--output", "ignored-path.json"],
            write_text=fake_write_text,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(written["path"], "ignored-path.json")
        payload = json.loads(written["content"])
        self.assertEqual(len(payload), 1)

    @patch("job_image_vuln_check.cli.build_targets")
    def test_bad_targets_input_reports_error_and_returns_1(self, mock_build_targets):
        mock_build_targets.side_effect = ValueError("one of --image, --targets, or --targets-file is required")
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            exit_code = cli.run([])
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", buf.getvalue())

    @patch("job_image_vuln_check.cli.run_quay_vuln_report")
    @patch("job_image_vuln_check.cli.build_targets")
    def test_quay_vuln_report_error_reports_and_returns_1(self, mock_build_targets, mock_run):
        mock_build_targets.return_value = [Target(image_ref="quay.io/a/b:latest", repository="org/b-src")]
        mock_run.side_effect = QuayVulnReportError("boom")
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err):
            exit_code = cli.run(["--image", "quay.io/a/b:latest", "--repository", "org/b-src"])
        self.assertEqual(exit_code, 1)
        self.assertIn("boom", buf_err.getvalue())


if __name__ == "__main__":
    unittest.main()
