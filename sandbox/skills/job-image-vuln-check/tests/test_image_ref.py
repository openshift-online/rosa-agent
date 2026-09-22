"""Unit tests for job_image_vuln_check.image_ref (written before the implementation)."""

from __future__ import annotations

import unittest

from job_image_vuln_check.image_ref import ImageRefError, parse_image_ref


class ParseImageRefTests(unittest.TestCase):
    def test_tag_ref_nested_path(self):
        ref = parse_image_ref(
            "quay.io/redhat-services-prod/rosa-tenant/rosa-agent/rosa-agent:latest"
        )
        self.assertEqual(ref.registry, "quay.io")
        self.assertEqual(ref.namespace, "redhat-services-prod")
        self.assertEqual(ref.repo_path, "rosa-tenant/rosa-agent/rosa-agent")
        self.assertEqual(ref.tag, "latest")
        self.assertIsNone(ref.digest)
        self.assertEqual(ref.full_repository, "redhat-services-prod/rosa-tenant/rosa-agent/rosa-agent")

    def test_tag_ref_simple_path(self):
        ref = parse_image_ref("quay.io/openshift-examples/sample-operator:v1.2.3")
        self.assertEqual(ref.namespace, "openshift-examples")
        self.assertEqual(ref.repo_path, "sample-operator")
        self.assertEqual(ref.tag, "v1.2.3")

    def test_ref_without_tag_leaves_tag_none(self):
        ref = parse_image_ref("quay.io/openshift-examples/sample-operator")
        self.assertIsNone(ref.tag)
        self.assertIsNone(ref.digest)

    def test_digest_ref(self):
        digest = "sha256:" + "a" * 64
        ref = parse_image_ref(f"quay.io/openshift-examples/sample-operator@{digest}")
        self.assertEqual(ref.digest, digest)
        self.assertIsNone(ref.tag)
        self.assertEqual(ref.repo_path, "sample-operator")

    def test_non_quay_registry_raises(self):
        with self.assertRaises(ImageRefError):
            parse_image_ref("docker.io/library/nginx:latest")

    def test_registry_with_port_is_still_quay(self):
        ref = parse_image_ref("quay.io:443/openshift-examples/sample-operator:latest")
        self.assertEqual(ref.registry, "quay.io:443")
        self.assertEqual(ref.namespace, "openshift-examples")
        self.assertEqual(ref.repo_path, "sample-operator")
        self.assertEqual(ref.tag, "latest")

    def test_missing_repo_path_raises(self):
        with self.assertRaises(ImageRefError):
            parse_image_ref("quay.io/onlynamespace")

    def test_empty_ref_raises(self):
        with self.assertRaises(ImageRefError):
            parse_image_ref("")
        with self.assertRaises(ImageRefError):
            parse_image_ref("   ")

    def test_bad_digest_prefix_raises(self):
        with self.assertRaises(ImageRefError):
            parse_image_ref("quay.io/openshift-examples/sample-operator@deadbeef")

    def test_pull_spec_roundtrip_tag(self):
        ref = parse_image_ref("quay.io/openshift-examples/sample-operator:v1")
        self.assertEqual(ref.pull_spec, "quay.io/openshift-examples/sample-operator:v1")

    def test_pull_spec_roundtrip_digest(self):
        digest = "sha256:" + "b" * 64
        ref = parse_image_ref(f"quay.io/openshift-examples/sample-operator@{digest}")
        self.assertEqual(ref.pull_spec, f"quay.io/openshift-examples/sample-operator@{digest}")

    def test_pull_spec_without_tag_or_digest(self):
        ref = parse_image_ref("quay.io/openshift-examples/sample-operator")
        self.assertEqual(ref.pull_spec, "quay.io/openshift-examples/sample-operator")


if __name__ == "__main__":
    unittest.main()
