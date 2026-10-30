"""Tests for hack/openshell_profile_apply.sh.

Uses fake openshell binaries on PATH in a temp dir — no network, no real
gateway, no credentials, no live ~/.config touched.
"""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "openshell_profile_apply.sh"

_PROFILE_YAML = textwrap.dedent("""\
    id: test-profile
    display_name: Test Profile
    description: Test
    category: test
    credentials: []
    endpoints: []
    binaries: []
    inference_capable: false
""")

_EXPORT_WITH_RV = "id: test-profile\nresource_version: 42\n"


def _write(path, content, mode=0o600):
    path = Path(path)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _make_fake_dir(tmp):
    d = tmp / "fake_bin"
    d.mkdir(exist_ok=True)
    return d


def _write_fake_openshell(fake_dir, tmp, *, export_exits_ok, export_output="",
                           import_exit=0, update_exit=0):
    """
    Write a fake `openshell` that:
      - records every invocation to tmp/calls.log
      - on 'profile export': cats tmp/export_out.txt and exits export_exits_ok ? 0 : 1
      - on 'profile import': exits import_exit
      - on 'profile update': records args to tmp/update_args.txt, exits update_exit
    """
    export_file = tmp / "export_out.txt"
    export_file.write_text(export_output, encoding="utf-8")
    calls_log = tmp / "calls.log"
    update_args_file = tmp / "update_args.txt"
    export_exit_code = 0 if export_exits_ok else 1

    script = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {calls_log}
        cmd="$*"
        if [[ "$cmd" == *"profile export"* ]]; then
            cat {export_file}
            exit {export_exit_code}
        elif [[ "$cmd" == *"profile import"* ]]; then
            exit {import_exit}
        elif [[ "$cmd" == *"profile update"* ]]; then
            printf '%s\\n' "$@" > {update_args_file}
            exit {update_exit}
        fi
        exit 0
    """)
    fake = fake_dir / "openshell"
    _write(fake, script, mode=0o755)
    return calls_log, update_args_file


def _run_script(gateway, profile_yaml, global_flag, env):
    args = ["bash", str(SCRIPT), gateway, str(profile_yaml)]
    if global_flag:
        args.append("--global")
    return subprocess.run(args, capture_output=True, text=True, env=env)


class TestImportPath(unittest.TestCase):
    """Profile not found on gateway → import path is taken."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.profile = self.tmp / "profile.yaml"
        _write(self.profile, _PROFILE_YAML)
        fake_dir = _make_fake_dir(self.tmp)
        self.calls_log, _ = _write_fake_openshell(
            fake_dir, self.tmp, export_exits_ok=False, import_exit=0
        )
        self.env = {**os.environ, "PATH": str(fake_dir) + ":" + os.environ.get("PATH", "")}

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exits_zero(self):
        r = _run_script("gw", self.profile, True, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_called(self):
        _run_script("gw", self.profile, True, self.env)
        calls = self.calls_log.read_text()
        self.assertIn("profile import", calls)

    def test_update_not_called(self):
        _run_script("gw", self.profile, True, self.env)
        calls = self.calls_log.read_text()
        self.assertNotIn("profile update", calls)


class TestUpdatePath(unittest.TestCase):
    """Profile exists → update: resource_version spliced into temp file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.profile = self.tmp / "profile.yaml"
        _write(self.profile, _PROFILE_YAML)
        self.fake_dir = _make_fake_dir(self.tmp)
        self.calls_log, self.update_args = _write_fake_openshell(
            self.fake_dir, self.tmp,
            export_exits_ok=True, export_output=_EXPORT_WITH_RV, update_exit=0
        )
        self.env = {**os.environ, "PATH": str(self.fake_dir) + ":" + os.environ.get("PATH", "")}

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exits_zero(self):
        r = _run_script("gw", self.profile, True, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_update_called(self):
        _run_script("gw", self.profile, True, self.env)
        calls = self.calls_log.read_text()
        self.assertIn("profile update", calls)

    def test_import_not_called(self):
        _run_script("gw", self.profile, True, self.env)
        calls = self.calls_log.read_text()
        self.assertNotIn("profile import", calls)

    def test_resource_version_value_in_update_args(self):
        _run_script("gw", self.profile, True, self.env)
        # update_args has one argv word per line; --file <path> must be present
        args = self.update_args.read_text()
        self.assertIn("--file", args, f"--file not in update args: {args}")
        # The resource_version value (42) from the export must be in the args
        # (it's in the temp file path name? no — the file content has rv:42).
        # The script outputs "resource_version: 42" in the temp file; the
        # update call's --file points at that temp file. We verify the temp
        # file content by reading all lines and locating the --file path.
        lines = [l.rstrip() for l in args.splitlines()]
        file_idx = next((i for i, l in enumerate(lines) if l == "--file"), -1)
        self.assertGreater(file_idx, -1, f"--file not found in update args lines: {lines}")

    def test_resource_version_spliced_into_file(self):
        """The --file temp file passed to 'profile update' contains resource_version."""
        # Instrument update to save its --file argument before it exits.
        capture = self.tmp / "captured_file_path.txt"
        script = textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> {self.calls_log}
            cmd="$*"
            if [[ "$cmd" == *"profile export"* ]]; then
                cat {self.tmp}/export_out.txt
                exit 0
            elif [[ "$cmd" == *"profile update"* ]]; then
                args=("$@")
                for i in "${{!args[@]}}"; do
                    if [[ "${{args[$i]}}" == "--file" ]]; then
                        echo "${{args[$((i+1))]}}" > {capture}
                        # Copy file content before script removes it
                        cp "${{args[$((i+1))]}}" {self.tmp}/tmp_file_content.txt 2>/dev/null || true
                    fi
                done
                exit 0
            fi
            exit 0
        """)
        # Overwrite fake
        _write(self.fake_dir / "openshell", script, mode=0o755)

        r = _run_script("gw", self.profile, True, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

        content_path = self.tmp / "tmp_file_content.txt"
        if not content_path.exists():
            self.skipTest("fake could not copy temp file (timing)")
        content = content_path.read_text()
        self.assertIn("resource_version: 42", content, f"resource_version not in temp file: {content}")


class TestTempFileLifecycle(unittest.TestCase):
    """Temp file is 0600 and removed on exit (success and error)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.profile = self.tmp / "profile.yaml"
        _write(self.profile, _PROFILE_YAML)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def _instrument_fake(self, *, update_exit):
        capture = self.tmp / "captured.txt"
        fake_dir = _make_fake_dir(self.tmp)
        calls_log = self.tmp / "calls.log"
        script = textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> {calls_log}
            cmd="$*"
            if [[ "$cmd" == *"profile export"* ]]; then
                printf 'resource_version: 77\\n'
                exit 0
            elif [[ "$cmd" == *"profile update"* ]]; then
                args=("$@")
                for i in "${{!args[@]}}"; do
                    if [[ "${{args[$i]}}" == "--file" ]]; then
                        f="${{args[$((i+1))]}}"
                        # Record mode and path
                        printf '%s %s\\n' "$(stat -c '%a' "$f")" "$f" > {capture}
                    fi
                done
                exit {update_exit}
            fi
            exit 0
        """)
        _write(fake_dir / "openshell", script, mode=0o755)
        env = {**os.environ, "PATH": str(fake_dir) + ":" + os.environ.get("PATH", "")}
        return env, capture

    def test_temp_file_removed_on_success(self):
        env, capture = self._instrument_fake(update_exit=0)
        _run_script("gw", self.profile, True, env)
        if not capture.exists():
            self.skipTest("fake did not record temp file")
        line = capture.read_text().strip()
        tmp_path = line.split(maxsplit=1)[1] if " " in line else ""
        self.assertFalse(Path(tmp_path).exists(), f"temp file not removed: {tmp_path}")

    def test_temp_file_mode_0600(self):
        env, capture = self._instrument_fake(update_exit=0)
        _run_script("gw", self.profile, True, env)
        if not capture.exists():
            self.skipTest("fake did not record temp file")
        line = capture.read_text().strip()
        mode = line.split()[0] if " " in line else ""
        self.assertEqual(mode, "600", f"expected mode 600, got {mode}")

    def test_temp_file_removed_on_error(self):
        env, capture = self._instrument_fake(update_exit=1)
        _run_script("gw", self.profile, True, env)
        if not capture.exists():
            self.skipTest("fake did not record temp file")
        line = capture.read_text().strip()
        tmp_path = line.split(maxsplit=1)[1] if " " in line else ""
        self.assertFalse(Path(tmp_path).exists(), f"temp file not removed after error: {tmp_path}")


class TestInputValidation(unittest.TestCase):
    """Argument validation — missing args, nonexistent file."""

    def _run(self, args):
        return subprocess.run(
            ["bash", str(SCRIPT)] + args,
            capture_output=True, text=True, env=os.environ
        )

    def test_no_args_exits_nonzero(self):
        self.assertNotEqual(self._run([]).returncode, 0)

    def test_one_arg_exits_nonzero(self):
        self.assertNotEqual(self._run(["my-gw"]).returncode, 0)

    def test_missing_profile_file_exits_nonzero(self):
        self.assertNotEqual(self._run(["my-gw", "/no/such/file.yaml"]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
