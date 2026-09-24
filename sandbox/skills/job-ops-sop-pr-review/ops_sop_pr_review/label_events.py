"""Find who most recently applied a given label to an issue/PR."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def find_label_adder(events: Iterable[Dict[str, Any]], label_name: str) -> Optional[str]:
    """Return the login of whoever most recently applied `label_name`.

    Issue events come back from the API in chronological order, so the LAST
    matching "labeled" event wins - if a label was removed and re-applied,
    this returns whoever did it most recently, not the first time ever.
    Returns None if the label was never applied by anyone with events
    (e.g. it was set at PR-creation time with no separate event, or the
    actor is missing from the payload).
    """
    adder = None
    for event in events:
        if event.get("event") != "labeled":
            continue
        label = event.get("label") or {}
        if label.get("name") == label_name:
            actor = event.get("actor") or {}
            login = actor.get("login")
            if login:
                adder = login
    return adder
