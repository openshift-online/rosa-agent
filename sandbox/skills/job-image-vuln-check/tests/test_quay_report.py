"""Unit tests for job_image_vuln_check.quay_report.

Fully hermetic: the skill-dir existence check goes through the injected
`skill_dir_exists` callable and the subprocess call through the injected
`runner` - nothing here creates a real file/directory or spawns a real
process.
"""

from __future__ import annotations

import json
import subprocess
import unittest

from job_image_vuln_check.image_ref import parse_image_ref
from job_image_vuln_check.quay_report import QuayVulnReportError, run_quay_vuln_report

FAKE_SKILL_DIR = "/fake/quay-vuln-report"  # never touched - skill_dir_exists is faked below


def _completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["python3"], returncode=returncode, stdout=stdout, stderr=stderr)


def _empty_report_runner(captured=None):
    def fake_runner(cmd, **kwargs):
        if captured is not None:
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
        return _completed(json.dumps({"vulnerability_count": 0, "vulnerabilities": []}))

    return fake_runner


class RunQuayVulnReportTests(unittest.TestCase):
    def test_tag_ref_builds_repository_image_tag_argv(self):
        image = parse_image_ref("quay.io/redhat-services-prod/rosa-tenant/rosa-agent/rosa-agent:latest")
        captured = {}

        run_quay_vuln_report(
            image, skill_dir=FAKE_SKILL_DIR, runner=_empty_report_runner(captured), skill_dir_exists=lambda d: True
        )
        cmd = captured["cmd"]
        self.assertIn("quay_vuln_report", cmd)
        self.assertIn("--repository", cmd)
        self.assertIn("redhat-services-prod", cmd)
        self.assertIn("--image", cmd)
        self.assertIn("rosa-tenant/rosa-agent/rosa-agent", cmd)
        self.assertIn("--tag", cmd)
        self.assertIn("latest", cmd)
        self.assertEqual(captured["cwd"], FAKE_SKILL_DIR)

    def test_ref_without_tag_defaults_to_latest(self):
        image = parse_image_ref("quay.io/openshift-examples/sample-operator")
        captured = {}

        run_quay_vuln_report(
            image, skill_dir=FAKE_SKILL_DIR, runner=_empty_report_runner(captured), skill_dir_exists=lambda d: True
        )
        idx = captured["cmd"].index("--tag")
        self.assertEqual(captured["cmd"][idx + 1], "latest")

    def test_digest_ref_uses_link_flag(self):
        digest = "sha256:" + "a" * 64
        image = parse_image_ref(f"quay.io/openshift-examples/sample-operator@{digest}")
        captured = {}

        run_quay_vuln_report(
            image, skill_dir=FAKE_SKILL_DIR, runner=_empty_report_runner(captured), skill_dir_exists=lambda d: True
        )
        self.assertIn("--link", captured["cmd"])
        idx = captured["cmd"].index("--link")
        self.assertIn(digest, captured["cmd"][idx + 1])
        self.assertNotIn("--repository", captured["cmd"])

    def test_pass_through_flags(self):
        image = parse_image_ref("quay.io/openshift-examples/sample-operator:latest")
        captured = {}

        run_quay_vuln_report(
            image,
            skill_dir=FAKE_SKILL_DIR,
            runner=_empty_report_runner(captured),
            skill_dir_exists=lambda d: True,
            include_non_fixable=True,
            sort_by="package",
            timeout=5,
            curl_bin="/opt/bin/curl",
        )
        cmd = captured["cmd"]
        self.assertIn("--include-non-fixable", cmd)
        self.assertIn("--sort-by", cmd)
        self.assertIn("package", cmd)
        self.assertIn("--timeout", cmd)
        self.assertIn("5", cmd)
        self.assertIn("--curl-bin", cmd)
        self.assertIn("/opt/bin/curl", cmd)

    def test_success_returns_parsed_json(self):
        image = parse_image_ref("quay.io/openshift-examples/sample-operator:latest")
        report = {"vulnerability_count": 2, "vulnerabilities": [{"cve": "CVE-1"}, {"cve": "CVE-2"}]}
        result = run_quay_vuln_report(
            image,
            skill_dir=FAKE_SKILL_DIR,
            skill_dir_exists=lambda d: True,
            runner=lambda cmd, **kw: _completed(json.dumps(report)),
        )
        self.assertEqual(result, report)

    def test_nonzero_exit_raises_with_stderr(self):
        image = parse_image_ref("quay.io/openshift-examples/sample-operator:latest")
        with self.assertRaises(QuayVulnReportError) as ctx:
            run_quay_vuln_report(
                image,
                skill_dir=FAKE_SKILL_DIR,
                skill_dir_exists=lambda d: True,
                runner=lambda cmd, **kw: _completed("", returncode=1, stderr="error: policy denied"),
            )
        self.assertIn("policy denied", str(ctx.exception))

    def test_malformed_json_raises(self):
        image = parse_image_ref("quay.io/openshift-examples/sample-operator:latest")
        with self.assertRaises(QuayVulnReportError):
            run_quay_vuln_report(
                image,
                skill_dir=FAKE_SKILL_DIR,
                skill_dir_exists=lambda d: True,
                runner=lambda cmd, **kw: _completed("not json"),
            )

    def test_missing_skill_dir_raises_actionable_error(self):
        image = parse_image_ref("quay.io/openshift-examples/sample-operator:latest")
        with self.assertRaises(QuayVulnReportError) as ctx:
            run_quay_vuln_report(
                image,
                skill_dir=FAKE_SKILL_DIR,
                skill_dir_exists=lambda d: False,
                runner=lambda cmd, **kw: self.fail("runner should not be called when skill_dir doesn't exist"),
            )
        self.assertIn("--quay-vuln-report-dir", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
