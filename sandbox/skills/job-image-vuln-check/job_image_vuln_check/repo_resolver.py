"""Best-effort GitHub source-repo resolution for a quay.io image.

Shells out to ``skopeo inspect`` (an allow-listed binary alongside curl
under this sandbox's ``quay_registry`` policy) to read the image config's
OCI labels, rather than pulling the image. A missing/unrecognized label is
a normal "couldn't determine it" outcome (returns ``None``) - only an
actual execution failure raises.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable, Optional

from .image_ref import ImageRef

DEFAULT_SKOPEO_BIN = "/usr/bin/skopeo"
DEFAULT_TIMEOUT = 30

# Checked in order - the first present, GitHub-hosted label wins.
_SOURCE_LABEL_KEYS = (
    "org.opencontainers.image.source",
    "org.opencontainers.image.url",
)

_GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

Runner = Callable[..., subprocess.CompletedProcess]


class RepoResolutionError(RuntimeError):
    """Raised when skopeo itself fails to run - never for "no label present"."""


def _extract_github_repo(url: str) -> Optional[str]:
    match = _GITHUB_REPO_RE.match(url.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def resolve_source_repo(
    image: ImageRef,
    *,
    skopeo_bin: str = DEFAULT_SKOPEO_BIN,
    timeout: int = DEFAULT_TIMEOUT,
    runner: Runner = subprocess.run,
) -> Optional[str]:
    """Return the ``owner/repo`` this image was built from, or ``None``.

    ``None`` means skopeo ran fine but the image simply has no recognizable
    source label - the caller (or the agent, per SKILL.md) decides what to
    do next. A ``RepoResolutionError`` means skopeo itself couldn't run.
    """
    cmd = [skopeo_bin, "inspect", f"docker://{image.pull_spec}"]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as exc:
        raise RepoResolutionError(f"failed to run {skopeo_bin}: {exc}") from exc

    if proc.returncode != 0:
        raise RepoResolutionError(
            f"skopeo inspect exited {proc.returncode} for {image.pull_spec}: "
            f"{(proc.stderr or '').strip() or '(no stderr output)'} - note that quay.io "
            "itself may be reachable while this still fails: reading labels requires "
            "fetching the image config blob, which quay.io redirects to its backing "
            "storage (observed: s3.us-east-1.amazonaws.com) - a host separate from "
            "quay.io that may not be allow-listed even when quay.io:443 is"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RepoResolutionError(
            f"skopeo inspect for {image.pull_spec} did not return valid JSON: {exc}"
        ) from exc

    labels = data.get("Labels") or {}
    for key in _SOURCE_LABEL_KEYS:
        value = labels.get(key)
        if not value:
            continue
        repo = _extract_github_repo(value)
        if repo:
            return repo
    return None
