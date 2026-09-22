"""Unit tests for job_image_vuln_check.targets.

Fully hermetic: --targets-file reads go through the injected `read_text`
callable, never a real file; repository resolution goes through the mocked
`resolve_source_repo`, never real skopeo/network. Nothing here touches disk
or the network.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from job_image_vuln_check.repo_resolver import RepoResolutionError
from job_image_vuln_check.targets import Target, build_targets


def _reader(text: str):
    """A fake read_text that returns a fixed string, no disk access."""
    return lambda path: text


def _failing_reader(exc: Exception):
    """A fake read_text that raises, no disk access."""

    def _raise(path):
        raise exc

    return _raise


class BuildTargetsFromSingleImageTests(unittest.TestCase):
    def test_image_with_explicit_repository(self):
        targets = build_targets(
            image="quay.io/openshift-examples/sample-operator:latest",
            repository="openshift-examples/sample-operator-src",
        )
        self.assertEqual(
            targets,
            [
                Target(
                    image_ref="quay.io/openshift-examples/sample-operator:latest",
                    repository="openshift-examples/sample-operator-src",
                )
            ],
        )

    @patch("job_image_vuln_check.targets.resolve_source_repo")
    def test_image_without_repository_resolves_it(self, mock_resolve):
        mock_resolve.return_value = "openshift-online/rosa-agent"
        targets = build_targets(image="quay.io/redhat-services-prod/rosa-tenant/rosa-agent/rosa-agent:latest")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].repository, "openshift-online/rosa-agent")
        mock_resolve.assert_called_once()

    @patch("job_image_vuln_check.targets.resolve_source_repo")
    def test_unresolvable_repository_leaves_it_none(self, mock_resolve):
        mock_resolve.return_value = None
        targets = build_targets(image="quay.io/openshift-examples/sample-operator:latest")
        self.assertEqual(len(targets), 1)
        self.assertIsNone(targets[0].repository)
        self.assertIsNone(targets[0].resolution_error)

    @patch("job_image_vuln_check.targets.resolve_source_repo")
    def test_resolution_error_is_caught_not_raised(self, mock_resolve):
        # A real, observed case: skopeo can reach quay.io but the redirected
        # blob fetch for the image config (needed for Labels) lands on a host
        # this sandbox doesn't allow-list. That must not crash the whole run
        # - it's equivalent to "couldn't determine it", with the reason kept
        # for the caller/agent to see.
        mock_resolve.side_effect = RepoResolutionError("skopeo inspect exited 1: Forbidden")
        targets = build_targets(image="quay.io/openshift-examples/sample-operator:latest")
        self.assertEqual(len(targets), 1)
        self.assertIsNone(targets[0].repository)
        self.assertIn("Forbidden", targets[0].resolution_error)


class BuildTargetsFromJsonMapTests(unittest.TestCase):
    def test_targets_json_string_with_explicit_repos(self):
        payload = json.dumps(
            {
                "quay.io/openshift-examples/a:latest": "org/a-src",
                "quay.io/openshift-examples/b:latest": "org/b-src",
            }
        )
        targets = build_targets(targets_json=payload)
        self.assertEqual(len(targets), 2)
        by_image = {t.image_ref: t.repository for t in targets}
        self.assertEqual(by_image["quay.io/openshift-examples/a:latest"], "org/a-src")
        self.assertEqual(by_image["quay.io/openshift-examples/b:latest"], "org/b-src")

    @patch("job_image_vuln_check.targets.resolve_source_repo")
    def test_null_repo_in_map_triggers_resolution(self, mock_resolve):
        mock_resolve.return_value = "org/a-src"
        payload = json.dumps({"quay.io/openshift-examples/a:latest": None})
        targets = build_targets(targets_json=payload)
        self.assertEqual(targets[0].repository, "org/a-src")

    def test_targets_file_uses_injected_reader_not_real_disk(self):
        payload = {"quay.io/openshift-examples/a:latest": "org/a-src"}
        targets = build_targets(targets_file="ignored-path.json", read_text=_reader(json.dumps(payload)))
        self.assertEqual(
            targets,
            [Target(image_ref="quay.io/openshift-examples/a:latest", repository="org/a-src")],
        )

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            build_targets(targets_json="not json")

    def test_targets_json_must_be_an_object(self):
        with self.assertRaises(ValueError):
            build_targets(targets_json=json.dumps(["quay.io/openshift-examples/a:latest"]))

    def test_unreadable_targets_file_raises_value_error_not_a_traceback(self):
        with self.assertRaises(ValueError):
            build_targets(
                targets_file="ignored-path.json",
                read_text=_failing_reader(FileNotFoundError("no such file")),
            )


class BuildTargetsInputValidationTests(unittest.TestCase):
    def test_no_input_raises(self):
        with self.assertRaises(ValueError):
            build_targets()

    def test_image_and_targets_together_raises(self):
        with self.assertRaises(ValueError):
            build_targets(image="quay.io/openshift-examples/a:latest", targets_json="{}")

    def test_targets_json_and_targets_file_together_raises(self):
        with self.assertRaises(ValueError):
            build_targets(targets_json="{}", targets_file="ignored-path.json")

    def test_repository_with_targets_json_raises(self):
        with self.assertRaises(ValueError):
            build_targets(
                targets_json='{"quay.io/openshift-examples/a:latest": "org/a-src"}',
                repository="org/ignored",
            )

    def test_repository_with_targets_file_raises(self):
        with self.assertRaises(ValueError):
            build_targets(
                targets_file="ignored-path.json",
                repository="org/ignored",
                read_text=_reader(json.dumps({"quay.io/openshift-examples/a:latest": "org/a-src"})),
            )


if __name__ == "__main__":
    unittest.main()
