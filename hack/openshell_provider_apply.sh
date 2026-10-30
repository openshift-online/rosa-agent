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

openshellctl -g "$GATEWAY" provider get "$PROVIDER_NAME" >/dev/null 2>&1 \
    && PROVIDER_EXISTS=true || PROVIDER_EXISTS=false

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
