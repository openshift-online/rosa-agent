"""Command-line interface for job-image-vuln-check.

Retrieves fixable-CVE data for one or more quay.io images (never a
hardcoded default) by delegating to quay-vuln-report, and attaches each
image's resolved (or unresolved) source GitHub repository so the agent
knows where to open a fix PR. A failure on one target is reported inline
for that target and does not discard results already collected for others
- this runs unattended as a nightly cron job, so one bad entry in a
--targets batch must not silently wipe out the whole run's output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__
from .image_ref import ImageRefError, parse_image_ref
from .quay_report import DEFAULT_QUAY_VULN_REPORT_DIR, QuayVulnReportError, run_quay_vuln_report
from .targets import build_targets

PROG = "job-image-vuln-check"

UNRESOLVED_NOTE = (
    "could not determine the source GitHub repository from this image's "
    "OCI labels; pass --repository (or a non-null value in --targets) "
    "explicitly, or use your own judgment to identify it before acting on "
    "this target"
)

WriteText = Callable[[str, str], None]


def _default_write_text(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Retrieve fixable-CVE data for one or more quay.io images by wrapping "
            "the quay-vuln-report skill, resolving each image's source GitHub repo "
            "when not given explicitly."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    parser.add_argument(
        "--image",
        metavar="REF",
        help="A single quay.io image reference, e.g. quay.io/<namespace>/<path>[:tag]. "
        "Mutually exclusive with --targets/--targets-file.",
    )
    parser.add_argument(
        "--repository",
        metavar="OWNER/REPO",
        help="The GitHub repo that produced --image. Optional - resolved via image labels when omitted.",
    )
    parser.add_argument(
        "--targets",
        metavar="JSON",
        help='A JSON object {"<image-ref>": "<owner/repo-or-null>"} for multiple images in one run.',
    )
    parser.add_argument(
        "--targets-file",
        metavar="PATH",
        help="Path to a JSON file in the same shape as --targets.",
    )

    parser.add_argument(
        "--include-non-fixable",
        action="store_true",
        help="Include vulnerabilities with no fixed version (default: fixable-only).",
    )
    parser.add_argument(
        "--sort-by",
        choices=["severity", "cve", "package", "layer"],
        default="severity",
    )
    parser.add_argument("--timeout", type=int, default=30, metavar="SECONDS")
    parser.add_argument(
        "--curl-bin",
        metavar="PATH",
        help="Passed through to quay-vuln-report's --curl-bin.",
    )
    parser.add_argument(
        "--quay-vuln-report-dir",
        default=DEFAULT_QUAY_VULN_REPORT_DIR,
        metavar="DIR",
        help=f"Installed location of the quay-vuln-report skill (default: {DEFAULT_QUAY_VULN_REPORT_DIR}).",
    )

    parser.add_argument("--output", "-o", metavar="FILE", help="Write JSON to this file instead of stdout.")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of pretty-printed.",
    )
    return parser


def _check_one_target(target, args) -> dict:
    """Return the JSON result for one target - either a report or an error entry.

    Never raises: a per-target failure (bad image ref, quay-vuln-report
    failure) is captured into the returned dict rather than propagated, so
    one bad target in a batch doesn't discard results already collected for
    the others.
    """
    try:
        image = parse_image_ref(target.image_ref)
        report = run_quay_vuln_report(
            image,
            skill_dir=args.quay_vuln_report_dir,
            include_non_fixable=args.include_non_fixable,
            sort_by=args.sort_by,
            timeout=args.timeout,
            curl_bin=args.curl_bin,
        )
    except (ImageRefError, QuayVulnReportError) as exc:
        print(f"error: {target.image_ref}: {exc}", file=sys.stderr)
        return {"image_ref": target.image_ref, "source_repository": target.repository, "error": str(exc)}

    report["source_repository"] = target.repository
    if target.repository is None:
        if target.resolution_error:
            report["note"] = f"could not check the image's source label ({target.resolution_error}); {UNRESOLVED_NOTE}"
        else:
            report["note"] = UNRESOLVED_NOTE
    return report


def run(argv: Optional[List[str]] = None, *, write_text: WriteText = _default_write_text) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        targets = build_targets(
            image=args.image,
            repository=args.repository,
            targets_json=args.targets,
            targets_file=args.targets_file,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    results = [_check_one_target(target, args) for target in targets]
    any_failed = any("error" in result for result in results)

    indent = None if args.compact else 2
    text = json.dumps(results, indent=indent)

    if args.output:
        write_text(args.output, text + "\n")
    else:
        print(text)
    return 1 if any_failed else 0


def main() -> None:
    sys.exit(run())
