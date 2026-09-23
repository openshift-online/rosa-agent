"""Deterministic CI-failure detection, excluding tide's own status context.

GitHub exposes CI results for a ref two ways: the newer Checks API
(check-runs) and the legacy Commit Status API (statuses). Prow-based CI (used
by openshift/ops-sop) posts both; tide itself posts a legacy status whose
context name starts with "tide" reflecting merge-readiness (lgtm/approved
label state), not a build/test result - the task explicitly excludes that
one ("failing tests OTHER than the tide lgtm/approve status context"), not
genuine CI failures.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

TIDE_CONTEXT_PREFIX = "tide"

FAILING_CHECK_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}
FAILING_STATUS_STATES = {"failure", "error"}


def _is_tide_context(name: str) -> bool:
    return (name or "").strip().lower().startswith(TIDE_CONTEXT_PREFIX)


def failing_check_runs(check_runs: Iterable[Dict[str, Any]]) -> List[str]:
    names = []
    for run in check_runs:
        name = run.get("name", "")
        if _is_tide_context(name):
            continue
        if run.get("status") == "completed" and run.get("conclusion") in FAILING_CHECK_CONCLUSIONS:
            names.append(name)
    return names


def failing_legacy_statuses(statuses: Iterable[Dict[str, Any]]) -> List[str]:
    names = []
    for status in statuses:
        context = status.get("context", "")
        if _is_tide_context(context):
            continue
        if status.get("state") in FAILING_STATUS_STATES:
            names.append(context)
    return names


def failing_contexts(check_runs: Iterable[Dict[str, Any]], statuses: Iterable[Dict[str, Any]]) -> List[str]:
    """Union of failing check-run names and failing legacy status contexts,
    tide excluded, de-duplicated, order-preserving."""
    seen: List[str] = []
    for name in failing_check_runs(check_runs) + failing_legacy_statuses(statuses):
        if name not in seen:
            seen.append(name)
    return seen
