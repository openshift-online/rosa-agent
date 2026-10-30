"""Tests for hack/openshell_provider_apply.sh.

Uses a fake openshellctl binary in a temp dir — no network, no real gateway,
no credentials, no live ~/.config touched.

Invariants under test:
  - create path: provider not found → openshellctl provider create
  - update path: provider found    → openshellctl provider update
  - credential is always passed as a bare KEY (not KEY=VALUE)
  - no secret value appears in any captured argv
  - KEY=VALUE form rejected immediately (before any gateway call)
"""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "openshell_provider_apply.sh"


def _write(path, content, mode=0o600):
    path = Path(path)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _make_fake_openshellctl(tmp, *, get_exit):
    """
    Fake openshellctl that:
      - logs every invocation to tmp/calls.log (one line per call, full argv)
      - on 'provider get': exits get_exit
      - on 'provider create': records args to tmp/create_args.txt, exits 0
      - on 'provider update': records args to tmp/update_args.txt, exits 0
    """
    fake_dir = tmp / "fake_bin"
    fake_dir.mkdir(exist_ok=True)
    calls_log = tmp / "calls.log"
    create_args = tmp / "create_args.txt"
    update_args = tmp / "update_args.txt"

    script = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {calls_log}
        cmd="$*"
        if [[ "$cmd" == *"provider get"* ]]; then
            exit {get_exit}
        elif [[ "$cmd" == *"provider create"* ]]; then
            printf '%s\\n' "$@" > {create_args}
            exit 0
        elif [[ "$cmd" == *"provider update"* ]]; then
            printf '%s\\n' "$@" > {update_args}
            exit 0
        fi
        exit 0
    """)
    _write(fake_dir / "openshellctl", script, mode=0o755)
    return fake_dir, calls_log, create_args, update_args


def _run(gateway, provider, profile, cred_key, env):
    return subprocess.run(
        ["bash", str(SCRIPT), gateway, provider, profile, cred_key],
        capture_output=True, text=True, env=env
    )


class TestCreatePath(unittest.TestCase):
    """Provider not found → create."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake_dir, self.calls, self.create_args, _ = _make_fake_openshellctl(
            self.tmp, get_exit=1
        )
        self.env = {**os.environ, "PATH": str(self.fake_dir) + ":" + os.environ.get("PATH", "")}

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exits_zero(self):
        r = _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_create_called(self):
        _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        calls = self.calls.read_text()
        self.assertIn("provider create", calls)

    def test_update_not_called(self):
        _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        calls = self.calls.read_text()
        self.assertNotIn("provider update", calls)

    def test_create_uses_global_profile_flag(self):
        _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        args = self.create_args.read_text()
        self.assertIn("--global-profile", args)

    def test_create_credential_is_bare_key(self):
        """--credential must appear followed by the bare KEY, not KEY=VALUE."""
        _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        args_lines = self.create_args.read_text().splitlines()
        cred_idx = next((i for i, l in enumerate(args_lines) if l == "--credential"), -1)
        self.assertGreater(cred_idx, -1, f"--credential not found in: {args_lines}")
        cred_value = args_lines[cred_idx + 1]
        self.assertEqual(cred_value, "MY_KEY", f"expected bare key 'MY_KEY', got: {cred_value}")
        self.assertNotIn("=", cred_value, "credential must be bare KEY, not KEY=VALUE")

    def test_no_secret_in_argv(self):
        """A planted secret value in env must not appear in the recorded argv."""
        env = {**self.env, "MY_KEY": "super-secret-value"}
        _run("gw", "my-provider", "my-profile", "MY_KEY", env)
        all_calls = self.calls.read_text()
        self.assertNotIn("super-secret-value", all_calls)


class TestUpdatePath(unittest.TestCase):
    """Provider exists → update."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake_dir, self.calls, _, self.update_args = _make_fake_openshellctl(
            self.tmp, get_exit=0
        )
        self.env = {**os.environ, "PATH": str(self.fake_dir) + ":" + os.environ.get("PATH", "")}

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exits_zero(self):
        r = _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_update_called(self):
        _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        calls = self.calls.read_text()
        self.assertIn("provider update", calls)

    def test_create_not_called(self):
        _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        calls = self.calls.read_text()
        self.assertNotIn("provider create", calls)

    def test_update_credential_is_bare_key(self):
        _run("gw", "my-provider", "my-profile", "MY_KEY", self.env)
        args_lines = self.update_args.read_text().splitlines()
        cred_idx = next((i for i, l in enumerate(args_lines) if l == "--credential"), -1)
        self.assertGreater(cred_idx, -1, f"--credential not found in: {args_lines}")
        cred_value = args_lines[cred_idx + 1]
        self.assertEqual(cred_value, "MY_KEY", f"expected bare key 'MY_KEY', got: {cred_value}")
        self.assertNotIn("=", cred_value, "credential must be bare KEY, not KEY=VALUE")

    def test_no_secret_in_argv(self):
        env = {**self.env, "MY_KEY": "another-secret"}
        _run("gw", "my-provider", "my-profile", "MY_KEY", env)
        all_calls = self.calls.read_text()
        self.assertNotIn("another-secret", all_calls)


class TestInputValidation(unittest.TestCase):
    """Argument validation — wrong arg count, KEY=VALUE form rejected."""

    def _run(self, args):
        return subprocess.run(
            ["bash", str(SCRIPT)] + args,
            capture_output=True, text=True, env=os.environ
        )

    def test_no_args_exits_nonzero(self):
        self.assertNotEqual(self._run([]).returncode, 0)

    def test_three_args_exits_nonzero(self):
        self.assertNotEqual(self._run(["gw", "prov", "prof"]).returncode, 0)

    def test_key_equals_value_rejected(self):
        """KEY=VALUE must be rejected before any gateway call."""
        r = self._run(["gw", "prov", "prof", "KEY=value"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bare", r.stderr.lower(), f"expected 'bare' in stderr: {r.stderr}")

    def test_empty_key_rejected(self):
        r = self._run(["gw", "prov", "prof", ""])
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
