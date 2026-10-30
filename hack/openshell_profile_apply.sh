#!/usr/bin/env bash
# Idempotent profile import-or-update on an OpenShell gateway.
#
# Usage: openshell_profile_apply.sh <gateway> <profile-yaml> [--global]
#
# Reads the profile id from the 'id:' field in the YAML file.
# On first run: imports the profile via `openshell profile import`.
# On re-run:   reads resource_version from a fresh export, splices it into a
#              0600 temp file, and runs `openshell profile update`. The repo
#              file is never modified. See docs/hypershell-providers.md for why
#              resource_version must come from a live export rather than the
#              repo file.
#
# The temp file is always removed on exit (even on error) via a trap.

set -euo pipefail

usage() {
    echo "usage: $0 <gateway> <profile-yaml> [--global]" >&2
    exit 1
}

[[ $# -ge 2 ]] || usage

GATEWAY=$1
PROFILE_YAML=$2
GLOBAL_FLAG=${3:-}

[[ -n "$GATEWAY" ]] || { echo "error: gateway name must not be empty" >&2; exit 1; }
[[ -f "$PROFILE_YAML" ]] || { echo "error: profile file not found: $PROFILE_YAML" >&2; exit 1; }

PROFILE_ID=$(grep -m1 '^id:' "$PROFILE_YAML" | awk '{print $2}')
[[ -n "$PROFILE_ID" ]] || { echo "error: could not find 'id:' field in $PROFILE_YAML" >&2; exit 1; }

# Build flag list for profile subcommands (--global is optional)
GLOBAL_ARGS=()
[[ -n "$GLOBAL_FLAG" ]] && GLOBAL_ARGS+=("--global")

EXPORT_OUTPUT=$(openshell -g "$GATEWAY" profile export "$PROFILE_ID" "${GLOBAL_ARGS[@]}" 2>&1) \
    && PROFILE_EXISTS=true || PROFILE_EXISTS=false

if [[ "$PROFILE_EXISTS" == false ]]; then
    echo "Profile '$PROFILE_ID' not found on gateway '$GATEWAY'; importing..."
    openshell -g "$GATEWAY" profile import --file "$PROFILE_YAML" "${GLOBAL_ARGS[@]}"
    echo "Profile '$PROFILE_ID' imported."
else
    echo "Profile '$PROFILE_ID' exists on gateway '$GATEWAY'; updating..."

    RESOURCE_VERSION=$(printf '%s' "$EXPORT_OUTPUT" | grep '^resource_version:' | head -1 | awk '{print $2}')
    [[ -n "$RESOURCE_VERSION" ]] || {
        echo "error: could not extract resource_version from export output" >&2
        exit 1
    }

    TMP_FILE=$(mktemp)
    chmod 600 "$TMP_FILE"
    trap 'rm -f "$TMP_FILE"' EXIT

    cp "$PROFILE_YAML" "$TMP_FILE"
    printf 'resource_version: %s\n' "$RESOURCE_VERSION" >> "$TMP_FILE"

    openshell -g "$GATEWAY" profile update "$PROFILE_ID" --file "$TMP_FILE" "${GLOBAL_ARGS[@]}"
    echo "Profile '$PROFILE_ID' updated."
fi
