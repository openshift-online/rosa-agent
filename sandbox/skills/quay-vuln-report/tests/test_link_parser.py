"""Unit tests for quay_vuln_report.link_parser."""

from __future__ import annotations

import unittest

from quay_vuln_report.link_parser import QuayLinkError, parse_quay_link


class ParseQuayLinkTests(unittest.TestCase):
    def test_simple_repository_link(self):
        parsed = parse_quay_link("https://quay.io/repository/openshift-examples/sample-operator")
        self.assertEqual(parsed.namespace, "openshift-examples")
        self.assertEqual(parsed.repo_path, "sample-operator")
        self.assertIsNone(parsed.digest)
        self.assertIsNone(parsed.tag)
        self.assertEqual(parsed.full_repository, "openshift-examples/sample-operator")

    def test_nested_repository_path(self):
        parsed = parse_quay_link(
            "https://quay.io/repository/redhat-services-prod/rosa-tenant/rosa-agent/rosa-agent"
        )
        self.assertEqual(parsed.namespace, "redhat-services-prod")
        self.assertEqual(parsed.repo_path, "rosa-tenant/rosa-agent/rosa-agent")

    def test_manifest_link_with_digest(self):
        digest = "sha256:" + "a" * 64
        parsed = parse_quay_link(
            f"https://quay.io/repository/openshift-examples/sample-operator/manifest/{digest}"
        )
        self.assertEqual(parsed.namespace, "openshift-examples")
        self.assertEqual(parsed.repo_path, "sample-operator")
        self.assertEqual(parsed.digest, digest)

    def test_nested_manifest_link_with_digest(self):
        digest = "sha256:" + "b" * 64
        parsed = parse_quay_link(
            "https://quay.io/repository/redhat-services-prod/rosa-tenant/rosa-agent/"
            f"rosa-agent/manifest/{digest}?tab=vulnerabilities&fixable=true"
        )
        self.assertEqual(parsed.namespace, "redhat-services-prod")
        self.assertEqual(parsed.repo_path, "rosa-tenant/rosa-agent/rosa-agent")
        self.assertEqual(parsed.digest, digest)

    def test_tag_query_param(self):
        parsed = parse_quay_link("https://quay.io/repository/openshift-examples/sample-operator?tab=tags")
        self.assertIsNone(parsed.tag)
        parsed = parse_quay_link("https://quay.io/repository/openshift-examples/sample-operator?tag=v1.2.3")
        self.assertEqual(parsed.tag, "v1.2.3")

    def test_empty_link_raises(self):
        with self.assertRaises(QuayLinkError):
            parse_quay_link("")
        with self.assertRaises(QuayLinkError):
            parse_quay_link("   ")

    def test_missing_scheme_raises(self):
        with self.assertRaises(QuayLinkError):
            parse_quay_link("quay.io/repository/openshift-examples/sample-operator")

    def test_wrong_host_raises(self):
        with self.assertRaises(QuayLinkError):
            parse_quay_link("https://hub.docker.com/repository/openshift-examples/sample-operator")

    def test_host_with_port_is_still_quay(self):
        parsed = parse_quay_link("https://quay.io:443/repository/openshift-examples/sample-operator")
        self.assertEqual(parsed.namespace, "openshift-examples")

    def test_not_a_repository_link_raises(self):
        with self.assertRaises(QuayLinkError):
            parse_quay_link("https://quay.io/organization/openshift-examples")

    def test_missing_repo_path_raises(self):
        with self.assertRaises(QuayLinkError):
            parse_quay_link("https://quay.io/repository/openshift-examples")

    def test_manifest_segment_without_digest_raises(self):
        with self.assertRaises(QuayLinkError):
            parse_quay_link("https://quay.io/repository/openshift-examples/sample-operator/manifest/")

    def test_digest_missing_sha256_prefix_raises(self):
        with self.assertRaises(QuayLinkError):
            parse_quay_link(
                "https://quay.io/repository/openshift-examples/sample-operator/manifest/deadbeef"
            )


if __name__ == "__main__":
    unittest.main()
