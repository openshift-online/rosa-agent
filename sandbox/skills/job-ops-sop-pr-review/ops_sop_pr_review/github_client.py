"""Thin client for GitHub's REST API, via the `gh` CLI.

HTTP transport: this shells out to the `gh` binary (`gh api ...`) rather than
using the `requests` library or Python's own `urllib`. That mirrors
sandbox/skills/quay-vuln-report's choice to shell out to `curl` for quay.io -
same rationale, different binary: in a sandbox that allow-lists egress by
binary path rather than by arbitrary outbound socket, a raw connection opened
from inside the Python interpreter is a *different* binary than any
allow-listed one and gets denied even if the destination host itself is
allowed. `gh` is the correct allow-listed, pre-authenticated tool for
github.com specifically (see AGENTS.md / the `github` skill - credentials are
pre-injected into `gh`, never into a Python HTTP client), so it is used here
in exactly the same subprocess role `curl` plays in quay_vuln_report. Either
way, this package has zero pip/PyPI runtime dependency, matching the
sandbox's stdlib-only Python requirement (no package manager available at
runtime).

Only REST paths are used - see the `github` skill: GraphQL-backed `gh`
subcommands (`gh pr list`, `gh issue list`, etc.) are blocked in this
sandbox, so every call here is `gh api <rest-path>`.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

DEFAULT_GH_BIN = "gh"
DEFAULT_TIMEOUT = 60


class GhApiError(RuntimeError):
    """Raised for a failed `gh api` invocation or unparseable output."""

    def __init__(self, message: str, exit_code: Optional[int] = None):
        super().__init__(message)
        self.exit_code = exit_code


class GithubClient:
    def __init__(self, gh_bin: str = DEFAULT_GH_BIN, timeout: int = DEFAULT_TIMEOUT):
        self._gh_bin = gh_bin
        self._timeout = timeout

    def _run(self, path_with_query: str, extra_args: Optional[List[str]] = None) -> str:
        cmd = [self._gh_bin, "api", path_with_query] + list(extra_args or [])
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=self._timeout
            )
        except OSError as exc:
            raise GhApiError(f"failed to run '{self._gh_bin}': {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GhApiError(f"'{self._gh_bin} api {path_with_query}' timed out after {self._timeout}s") from exc

        if proc.returncode != 0:
            raise GhApiError(
                f"gh api {path_with_query} exited {proc.returncode}: "
                f"{proc.stderr.strip() or '(no stderr output)'}",
                exit_code=proc.returncode,
            )
        return proc.stdout

    @staticmethod
    def _build_path(path: str, params: Optional[Dict[str, Any]]) -> str:
        if not params:
            return path
        return f"{path}?{urlencode(params)}"

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET a single (non-list) REST resource and return its parsed JSON."""
        full_path = self._build_path(path, params)
        stdout = self._run(full_path)
        return _parse_json(stdout, full_path)

    def get_paginated(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """GET every page of a list endpoint (`gh api ... --paginate`) and
        return a single flat list."""
        full_path = self._build_path(path, params)
        stdout = self._run(full_path, extra_args=["--paginate"])
        return parse_paginated_json(stdout, full_path)


def _parse_json(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GhApiError(f"gh api {source} did not return valid JSON: {exc}") from exc


def parse_paginated_json(text: str, source: str) -> List[Dict[str, Any]]:
    """`gh api ... --paginate` prints one JSON array (or object) per page,
    concatenated back-to-back with no separator - not a single valid JSON
    document. Decode each top-level value in turn and flatten any lists into
    one result list.
    """
    decoder = json.JSONDecoder()
    items: List[Dict[str, Any]] = []
    text = text.strip()
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            raise GhApiError(
                f"gh api {source} --paginate output was not parseable JSON at offset {idx}: {exc}"
            ) from exc
        if isinstance(obj, list):
            items.extend(obj)
        else:
            items.append(obj)
        idx = end
    return items
