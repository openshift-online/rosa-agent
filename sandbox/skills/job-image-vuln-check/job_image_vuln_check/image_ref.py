"""Parsing for podman/docker-pull-style quay.io image references.

Accepts the same shape ``podman pull`` or ``skopeo inspect`` would:
``quay.io/<namespace>/<path...>[:tag]`` or
``quay.io/<namespace>/<path...>[@sha256:<digest>]``. This tool wraps the
quay-vuln-report skill, which is itself quay.io-only, so any other registry
is rejected here rather than silently mishandled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SUPPORTED_REGISTRY = "quay.io"
DIGEST_PREFIX = "sha256:"


class ImageRefError(ValueError):
    """Raised when a string is not a well-formed quay.io image reference."""


@dataclass(frozen=True)
class ImageRef:
    registry: str
    namespace: str
    repo_path: str
    tag: Optional[str] = None
    digest: Optional[str] = None

    @property
    def full_repository(self) -> str:
        return f"{self.namespace}/{self.repo_path}"

    @property
    def pull_spec(self) -> str:
        """Reconstruct a podman/docker-pull-style reference."""
        base = f"{self.registry}/{self.full_repository}"
        if self.digest:
            return f"{base}@{self.digest}"
        if self.tag:
            return f"{base}:{self.tag}"
        return base


def parse_image_ref(ref: str) -> ImageRef:
    """Parse a ``podman pull``-style quay.io image reference.

    Leaves ``tag`` as ``None`` when the reference doesn't specify one -
    callers decide their own default rather than this function guessing one.
    """
    if not ref or not ref.strip():
        raise ImageRefError("image reference was empty")
    ref = ref.strip()

    digest: Optional[str] = None
    remainder = ref
    if "@" in ref:
        remainder, _, digest_part = ref.partition("@")
        if not digest_part.startswith(DIGEST_PREFIX):
            raise ImageRefError(
                f"expected a 'sha256:<digest>' after '@' in {ref!r}, got {digest_part!r}"
            )
        digest = digest_part

    segments = [s for s in remainder.split("/") if s]
    if len(segments) < 3:
        raise ImageRefError(
            f"{ref!r} is not a full image reference - expected "
            f"'{SUPPORTED_REGISTRY}/<namespace>/<repo-path>[:tag]'"
        )

    registry = segments[0]
    # A registry host may carry an explicit port (e.g. "quay.io:443") - strip
    # it only for the support check, same as quay_vuln_report's link_parser
    # does for its own host check. The original (with port) is kept in the
    # returned ImageRef so pull_spec reconstructs it faithfully.
    registry_host = registry.split(":", 1)[0]
    if registry_host.lower() != SUPPORTED_REGISTRY:
        raise ImageRefError(
            f"unsupported registry '{registry}' in {ref!r} - this tool only supports "
            f"{SUPPORTED_REGISTRY} images (it wraps the quay-vuln-report skill)"
        )

    namespace = segments[1]
    last = segments[-1]
    tag: Optional[str] = None
    if ":" in last:
        last, _, tag = last.rpartition(":")
        segments[-1] = last
    repo_path = "/".join(segments[2:])
    if not repo_path:
        raise ImageRefError(
            f"{ref!r} is missing the repository path after the namespace '{namespace}'"
        )

    return ImageRef(registry=registry, namespace=namespace, repo_path=repo_path, tag=tag, digest=digest)
