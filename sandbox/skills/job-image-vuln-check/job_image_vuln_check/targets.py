"""Build the list of (image, repository) targets to check in one run.

Accepts exactly one of a single ``--image`` (with optional ``--repository``)
or a ``--targets``/``--targets-file`` JSON map of
``{"<image-ref>": "<owner/repo-or-null>"}``. A missing repository is
resolved via ``repo_resolver.resolve_source_repo``; if that can't determine
one either, the target's ``repository`` stays ``None`` rather than failing
the whole run - SKILL.md tells the agent what to do about that gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .image_ref import parse_image_ref
from .repo_resolver import RepoResolutionError, resolve_source_repo

ReadText = Callable[[str], str]


def _default_read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Target:
    image_ref: str
    repository: Optional[str] = None
    # Set only when resolution was attempted and skopeo itself failed (as
    # opposed to running fine and simply finding no source label) - kept so
    # the caller/agent can see *why*, without the whole run crashing.
    resolution_error: Optional[str] = None


def _resolve_if_missing(image_ref: str, repository: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if repository:
        return repository, None
    try:
        return resolve_source_repo(parse_image_ref(image_ref)), None
    except RepoResolutionError as exc:
        return None, str(exc)


def build_targets(
    *,
    image: Optional[str] = None,
    repository: Optional[str] = None,
    targets_json: Optional[str] = None,
    targets_file: Optional[str] = None,
    read_text: ReadText = _default_read_text,
) -> List[Target]:
    given = [name for name, value in (("image", image), ("targets_json", targets_json), ("targets_file", targets_file)) if value is not None]
    if not given:
        raise ValueError("one of --image, --targets, or --targets-file is required")
    if len(given) > 1:
        raise ValueError(f"--image, --targets, and --targets-file are mutually exclusive (got {', '.join(given)})")
    if repository is not None and image is None:
        raise ValueError(
            "--repository is only valid together with --image - set the repository "
            "per-entry inside --targets/--targets-file's JSON map instead"
        )

    if image is not None:
        raw_map = {image: repository}
    else:
        if targets_file is not None:
            try:
                text = read_text(targets_file)
            except OSError as exc:
                raise ValueError(f"could not read --targets-file '{targets_file}': {exc}") from exc
        else:
            text = targets_json
        try:
            raw_map = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"targets JSON is not valid: {exc}") from exc
        if not isinstance(raw_map, dict):
            raise ValueError(
                "targets JSON must be an object mapping image reference -> repository (or null)"
            )

    targets = []
    for image_ref, repo in raw_map.items():
        resolved_repo, error = _resolve_if_missing(image_ref, repo)
        targets.append(Target(image_ref=image_ref, repository=resolved_repo, resolution_error=error))
    return targets
