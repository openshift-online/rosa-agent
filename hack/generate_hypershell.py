#!/usr/bin/env python3
"""Regenerate the policy JSON embedded in the rosa-agent SandboxTemplate.

Reads policies/default.yaml, serialises it to JSON (no field renaming; same
transform as _policy_yaml_to_json in check_hypershell.py), and writes the
result into .hypershell/base/platform/sandbox-template.yaml in-place.

Run this whenever policies/default.yaml changes:
    make generate-hypershell

Or directly:
    python3 hack/generate_hypershell.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit(
        "ERROR: PyYAML is required.\n"
        "  Fedora/RHEL: sudo dnf install -y python3-pyyaml\n"
        "  venv:        python3 -m pip install pyyaml"
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / ".hypershell/base/platform/sandbox-template.yaml"
POLICY_PATH = REPO_ROOT / "policies/default.yaml"


def main() -> int:
    with open(POLICY_PATH) as f:
        policy_src = yaml.safe_load(f)

    policy_json = json.dumps(policy_src, separators=(",", ":"))

    # Single-quoted YAML scalars escape a literal apostrophe as ''.
    # Use a callable replacement so re.subn never interprets backslashes in
    # the JSON (e.g. \\d+ in an L7 path regex would be mangled otherwise).
    yaml_value = policy_json.replace("'", "''")
    text = TEMPLATE_PATH.read_text()
    # spec.policy is a single-quoted YAML scalar on one line.
    updated, count = re.subn(
        r"^  policy: '.*'$",
        lambda _: f"  policy: '{yaml_value}'",
        text,
        flags=re.MULTILINE,
    )
    if count != 1:
        print(
            f"ERROR: expected exactly 1 spec.policy line in {TEMPLATE_PATH}, "
            f"found {count}. Has the file structure changed?",
            file=sys.stderr,
        )
        return 1

    TEMPLATE_PATH.write_text(updated)
    print(f"generate-hypershell: wrote {len(policy_json)} bytes to spec.policy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
