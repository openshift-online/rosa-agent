"""Unit tests for job_image_vuln_check.repo_resolver (written before the implementation).

No real skopeo/subprocess/network calls - the runner is always injected.
"""

from __future__ import annotations

import subprocess
import unittest

from job_image_vuln_check.image_ref import parse_image_ref
from job_image_vuln_check.repo_resolver import RepoResolutionError, resolve_source_repo

IMAGE = parse_image_ref("quay.io/openshift-examples/sample-operator:latest")


def _completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["skopeo"], returncode=returncode, stdout=stdout, stderr=stderr)


class ResolveSourceRepoTests(unittest.TestCase):
    def test_resolves_from_source_label(self):
        stdout = '{"Labels": {"org.opencontainers.image.source": "https://github.com/openshift-online/rosa-agent"}}'
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            return _completed(stdout)

        repo = resolve_source_repo(IMAGE, runner=fake_runner)
        self.assertEqual(repo, "openshift-online/rosa-agent")
        self.assertIn("docker://quay.io/openshift-examples/sample-operator:latest", calls[0])

    def test_falls_back_to_url_label(self):
        stdout = '{"Labels": {"org.opencontainers.image.url": "https://github.com/foo/bar"}}'
        repo = resolve_source_repo(IMAGE, runner=lambda cmd, **kw: _completed(stdout))
        self.assertEqual(repo, "foo/bar")

    def test_source_label_takes_precedence_over_url_label(self):
        stdout = (
            '{"Labels": {'
            '"org.opencontainers.image.source": "https://github.com/a/b",'
            '"org.opencontainers.image.url": "https://github.com/c/d"'
            "}}"
        )
        repo = resolve_source_repo(IMAGE, runner=lambda cmd, **kw: _completed(stdout))
        self.assertEqual(repo, "a/b")

    def test_no_labels_returns_none(self):
        repo = resolve_source_repo(IMAGE, runner=lambda cmd, **kw: _completed('{"Labels": {}}'))
        self.assertIsNone(repo)

    def test_missing_labels_key_returns_none(self):
        repo = resolve_source_repo(IMAGE, runner=lambda cmd, **kw: _completed("{}"))
        self.assertIsNone(repo)

    def test_non_github_source_label_returns_none(self):
        stdout = '{"Labels": {"org.opencontainers.image.source": "https://gitlab.com/a/b"}}'
        repo = resolve_source_repo(IMAGE, runner=lambda cmd, **kw: _completed(stdout))
        self.assertIsNone(repo)

    def test_source_label_is_case_insensitive_and_tolerates_www(self):
        stdout = '{"Labels": {"org.opencontainers.image.source": "https://WWW.GitHub.com/Openshift-Online/Rosa-Agent"}}'
        repo = resolve_source_repo(IMAGE, runner=lambda cmd, **kw: _completed(stdout))
        self.assertEqual(repo, "Openshift-Online/Rosa-Agent")

    def test_skopeo_nonzero_exit_raises(self):
        def fake_runner(cmd, **kwargs):
            return _completed("", returncode=1, stderr="policy denied")

        with self.assertRaises(RepoResolutionError):
            resolve_source_repo(IMAGE, runner=fake_runner)

    def test_skopeo_missing_binary_raises(self):
        def fake_runner(cmd, **kwargs):
            raise OSError("no such file")

        with self.assertRaises(RepoResolutionError):
            resolve_source_repo(IMAGE, runner=fake_runner)

    def test_invalid_json_raises(self):
        with self.assertRaises(RepoResolutionError):
            resolve_source_repo(IMAGE, runner=lambda cmd, **kw: _completed("not json"))


if __name__ == "__main__":
    unittest.main()
