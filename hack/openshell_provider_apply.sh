#!/usr/bin/env bash
# Idempotent provider create-or-update on an OpenShell gateway.
#
# Usage: openshell_provider_apply.sh <gateway> <provider-name> <profile-id> <CREDENTIAL_KEY>
#
# CREDENTIAL_KEY must be a bare environment-variable name (e.g. OPENSHELL_OIDC_CLIENT_SECRET),
# not a KEY=VALUE pair. openshellctl reads the value from the environment via
# ParseCredentialPairs(credentials, os.Getenv), so the secret never reaches argv.
# Supply it as an environment variable from Vault before calling this script:
#
#   env OPENSHELL_OIDC_CLIENT_SECRET="$(vault kv get -field=... ...)" \
#     hack/openshell_provider_apply.sh rosa-agent rosa-agent-openshell openshell OPENSHELL_OIDC_CLIENT_SECRET

set -euo pipefail

usage() {
    echo "usage: $0 <gateway> <provider-name> <profile-id> <CREDENTIAL_KEY>" >&2
    echo "  CREDENTIAL_KEY must be a bare env-var name, not KEY=VALUE" >&2
    exit 1
}

[[ $# -eq 4 ]] || usage

GATEWAY=$1
PROVIDER_NAME=$2
PROFILE_ID=$3
CREDENTIAL_KEY=$4

[[ -n "$GATEWAY" ]]       || { echo "error: gateway name must not be empty" >&2; exit 1; }
[[ -n "$PROVIDER_NAME" ]] || { echo "error: provider name must not be empty" >&2; exit 1; }
[[ -n "$PROFILE_ID" ]]    || { echo "error: profile id must not be empty" >&2; exit 1; }
[[ -n "$CREDENTIAL_KEY" ]] || { echo "error: credential key must not be empty" >&2; exit 1; }

case "$CREDENTIAL_KEY" in
    *=*) echo "error: CREDENTIAL_KEY must be a bare variable name (not KEY=VALUE); got: $CREDENTIAL_KEY" >&2; exit 1 ;;
esac

# Distinguish "not found" from auth or transport failures.
# Swallowing all non-zero exits as "not found" (the old pattern) caused auth
# errors to be misread as absence — the script then called provider create,
# which immediately hit the same backoff the get had already triggered.
GET_EXIT=0
GET_OUTPUT=$(openshellctl -g "$GATEWAY" provider get "$PROVIDER_NAME" 2>&1) || GET_EXIT=$?

if [[ $GET_EXIT -eq 0 ]]; then
    PROVIDER_EXISTS=true
elif echo "$GET_OUTPUT" | grep -qiF "not found"; then
    PROVIDER_EXISTS=false
else
    echo "error: 'openshellctl provider get' failed (exit $GET_EXIT) with unexpected error:" >&2
    echo "$GET_OUTPUT" >&2
    exit 1
fi

if [[ "$PROVIDER_EXISTS" == false ]]; then
    echo "Provider '$PROVIDER_NAME' not found on gateway '$GATEWAY'; creating..."
    openshellctl -g "$GATEWAY" provider create \
        --name "$PROVIDER_NAME" \
        --type "$PROFILE_ID" \
        --global-profile \
        --credential "$CREDENTIAL_KEY"
    echo "Provider '$PROVIDER_NAME' created."
else
    echo "Provider '$PROVIDER_NAME' exists on gateway '$GATEWAY'; updating..."
    openshellctl -g "$GATEWAY" provider update "$PROVIDER_NAME" \
        --credential "$CREDENTIAL_KEY"
    echo "Provider '$PROVIDER_NAME' updated."
fi
