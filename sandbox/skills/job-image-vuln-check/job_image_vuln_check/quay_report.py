"""Subprocess wrapper around the quay-vuln-report skill's own public CLI.

This never imports quay_vuln_report's internals - it shells out to
``python3 -m quay_vuln_report`` exactly as that skill's own SKILL.md
documents, the same discipline job-sop-improve uses for ``/sop-improve``:
never reimplement or reach into another skill's internals, always invoke it
the way it documents itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from .image_ref import ImageRef

# The installed runtime path (sandbox/skills/ is copied here in the
# Containerfile) - see github-labels/SKILL.md for the same convention.
DEFAULT_QUAY_VULN_REPORT_DIR = "/sandbox/.claude/skills/quay-vuln-report"
DEFAULT_TIMEOUT = 30

Runner = Callable[..., subprocess.CompletedProcess]
DirExists = Callable[[str], bool]


class QuayVulnReportError(RuntimeError):
    """Raised when quay-vuln-report can't be found, fails, or returns bad output."""


def _default_skill_dir_exists(skill_dir: str) -> bool:
    return (Path(skill_dir) / "quay_vuln_report" / "__init__.py").is_file()


def _build_argv(python_bin: str, image: ImageRef, *, include_non_fixable: bool, sort_by: str, timeout: int, curl_bin: Optional[str]):
    cmd = [python_bin, "-m", "quay_vuln_report"]
    if image.digest:
        link = f"https://{image.registry}/repository/{image.full_repository}/manifest/{image.digest}"
        cmd += ["--link", link]
    else:
        cmd += [
            "--repository", image.namespace,
            "--image", image.repo_path,
            "--tag", image.tag or "latest",
        ]
    if include_non_fixable:
        cmd.append("--include-non-fixable")
    cmd += ["--sort-by", sort_by, "--timeout", str(timeout)]
    if curl_bin:
        cmd += ["--curl-bin", curl_bin]
    return cmd


def run_quay_vuln_report(
    image: ImageRef,
    *,
    skill_dir: str = DEFAULT_QUAY_VULN_REPORT_DIR,
    include_non_fixable: bool = False,
    sort_by: str = "severity",
    timeout: int = DEFAULT_TIMEOUT,
    curl_bin: Optional[str] = None,
    python_bin: str = sys.executable,
    runner: Runner = subprocess.run,
    skill_dir_exists: DirExists = _default_skill_dir_exists,
) -> dict:
    """Run quay-vuln-report for ``image`` and return its parsed JSON report."""
    if not skill_dir_exists(skill_dir):
        raise QuayVulnReportError(
            f"quay-vuln-report skill not found at '{skill_dir}' (expected "
            f"'{skill_dir}/quay_vuln_report/__init__.py' to exist) - pass "
            "--quay-vuln-report-dir to point at its installed location"
        )

    cmd = _build_argv(python_bin, image, include_non_fixable=include_non_fixable, sort_by=sort_by, timeout=timeout, curl_bin=curl_bin)

    try:
        proc = runner(cmd, capture_output=True, text=True, cwd=skill_dir, check=False)
    except OSError as exc:
        raise QuayVulnReportError(f"failed to run {cmd[0]}: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "(no output)"
        raise QuayVulnReportError(f"quay-vuln-report exited {proc.returncode}: {detail}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QuayVulnReportError(f"quay-vuln-report did not return valid JSON: {exc}") from exc
