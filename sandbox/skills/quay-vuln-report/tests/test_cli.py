"""Unit tests for quay_vuln_report.cli."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import Mock, mock_open, patch

from quay_vuln_report import cli
from quay_vuln_report.api_client import QuayAPIError
from quay_vuln_report.link_parser import QuayLinkError
from quay_vuln_report.report import SecurityReportError


class ValidateArgsTests(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_link_with_repository_is_rejected(self):
        args = self.parser.parse_args(["--link", "https://quay.io/repository/a/b", "--repository", "a"])
        with self.assertRaises(SystemExit):
            cli.validate_args(self.parser, args)

    def test_nothing_given_is_rejected(self):
        args = self.parser.parse_args([])
        with self.assertRaises(SystemExit):
            cli.validate_args(self.parser, args)

    def test_repository_without_image_is_rejected(self):
        args = self.parser.parse_args(["--repository", "ns"])
        with self.assertRaises(SystemExit):
            cli.validate_args(self.parser, args)

    def test_image_without_repository_is_rejected(self):
        args = self.parser.parse_args(["--image", "repo"])
        with self.assertRaises(SystemExit):
            cli.validate_args(self.parser, args)

    def test_link_alone_is_valid(self):
        args = self.parser.parse_args(["--link", "https://quay.io/repository/a/b"])
        cli.validate_args(self.parser, args)  # does not raise

    def test_repository_and_image_is_valid(self):
        args = self.parser.parse_args(["--repository", "ns", "--image", "repo"])
        cli.validate_args(self.parser, args)  # does not raise


class ResolveImageTests(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_via_repository_and_image_resolves_tag_via_client(self):
        args = self.parser.parse_args(["--repository", "ns", "--image", "repo"])
        client = Mock()
        client.resolve_tag_digest.return_value = "sha256:abc"
        image = cli.resolve_image(self.parser, args, client)
        client.resolve_tag_digest.assert_called_once_with("ns/repo", "latest")
        self.assertEqual(image.digest, "sha256:abc")
        self.assertEqual(image.tag, "latest")

    def test_explicit_tag_overrides_default(self):
        args = self.parser.parse_args(["--repository", "ns", "--image", "repo", "--tag", "v1"])
        client = Mock()
        client.resolve_tag_digest.return_value = "sha256:abc"
        cli.resolve_image(self.parser, args, client)
        client.resolve_tag_digest.assert_called_once_with("ns/repo", "v1")

    def test_via_link_with_digest_skips_tag_resolution(self):
        digest = "sha256:" + "a" * 64
        args = self.parser.parse_args(["--link", f"https://quay.io/repository/ns/repo/manifest/{digest}"])
        client = Mock()
        image = cli.resolve_image(self.parser, args, client)
        client.resolve_tag_digest.assert_not_called()
        self.assertEqual(image.digest, digest)
        self.assertIsNone(image.tag)

    def test_via_link_without_digest_resolves_tag(self):
        args = self.parser.parse_args(["--link", "https://quay.io/repository/ns/repo"])
        client = Mock()
        client.resolve_tag_digest.return_value = "sha256:abc"
        cli.resolve_image(self.parser, args, client)
        client.resolve_tag_digest.assert_called_once_with("ns/repo", "latest")

    def test_bad_link_calls_parser_error(self):
        args = self.parser.parse_args(["--link", "not-a-url"])
        client = Mock()
        with self.assertRaises(SystemExit):
            cli.resolve_image(self.parser, args, client)


class RunTests(unittest.TestCase):
    def _fake_raw(self):
        return {
            "status": "scanned",
            "data": {
                "Layer": {
                    "Features": [
                        {
                            "Name": "pkg",
                            "Version": "1.0",
                            "AddedBy": "sha256:layer",
                            "Vulnerabilities": [
                                {"Name": "CVE-1", "Severity": "High", "FixedBy": "1.1", "Link": "", "Description": ""}
                            ],
                        }
                    ]
                }
            },
        }

    @patch("quay_vuln_report.cli.QuayAPIClient")
    def test_success_prints_json_to_stdout(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.resolve_tag_digest.return_value = "sha256:abc"
        client.get_manifest_security.return_value = self._fake_raw()

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            exit_code = cli.run(["--repository", "ns", "--image", "repo"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["vulnerability_count"], 1)

    @patch("quay_vuln_report.cli.QuayAPIClient")
    def test_success_writes_output_file(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.resolve_tag_digest.return_value = "sha256:abc"
        client.get_manifest_security.return_value = self._fake_raw()

        m = mock_open()
        with patch("quay_vuln_report.cli.open", m):
            exit_code = cli.run(["--repository", "ns", "--image", "repo", "--output", "ignored-path.json"])
        self.assertEqual(exit_code, 0)
        m.assert_called_once_with("ignored-path.json", "w", encoding="utf-8")
        written = "".join(call.args[0] for call in m().write.call_args_list)
        payload = json.loads(written)
        self.assertEqual(payload["vulnerability_count"], 1)

    @patch("quay_vuln_report.cli.QuayAPIClient")
    def test_api_error_prints_to_stderr_and_returns_1(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.resolve_tag_digest.side_effect = QuayAPIError("boom")

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            exit_code = cli.run(["--repository", "ns", "--image", "repo"])
        self.assertEqual(exit_code, 1)
        self.assertIn("error: boom", buf.getvalue())

    @patch("quay_vuln_report.cli.QuayAPIClient")
    def test_security_report_error_prints_to_stderr_and_returns_1(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.resolve_tag_digest.return_value = "sha256:abc"
        client.get_manifest_security.return_value = {"status": "queued"}

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            exit_code = cli.run(["--repository", "ns", "--image", "repo"])
        self.assertEqual(exit_code, 1)
        self.assertIn("error:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
