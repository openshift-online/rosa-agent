#!/usr/bin/env bash
# E2E test: nested sandbox creation via the openshell provider profile.
#
# HUMAN-RUN ONLY — not wired into `make test` because Prow/Konflux cannot
# reach the HyperShell OIDC endpoint (see Makefile "Scheduled jobs" comment).
#
# Prerequisites:
#   - VAULT_ADDR is set and a valid Vault token exists (run `make openshell-vault-login` first)
#   - OPENSHELL_VAULT_KV_MOUNT, OPENSHELL_VAULT_KV_PATH, OPENSHELL_VAULT_FIELD_PREFIX are set
#   - openshellctl is on PATH and the gateway is registered (run `make openshell-gateway` first)
#   - The openshell profile and provider are provisioned (run `make openshell-provider` first)
#
# Exit codes:
#   0  all three checks passed
#   1  check 1 did not deny as expected, or check 2 exited nonzero, or check 3 exited nonzero
#
# Cleanup: any e2e-nested-* sandboxes are deleted on EXIT via a trap.
# Inner-sandbox cleanup is NOT assumed — the host tears everything down.
#
# Usage:
#   bash hack/e2e/nested_sandbox.sh
#   # Optional overrides:
#   IMAGE=quay.io/... GATEWAY_NAME=rosa-agent bash hack/e2e/nested_sandbox.sh

set -euo pipefail

VAULT_MOUNT=${OPENSHELL_VAULT_KV_MOUNT:-osd-sre}
VAULT_PATH=${OPENSHELL_VAULT_KV_PATH:-rosa-agent}
VAULT_PREFIX=${OPENSHELL_VAULT_FIELD_PREFIX:-hypershell}
GATEWAY_NAME=${GATEWAY_NAME:-rosa-agent}
IMAGE=${IMAGE:-quay.io/redhat-services-prod/rosa-tenant/rosa-agent/rosa-agent:latest}

# Read non-secret OIDC/gateway values from Vault so nothing environment-specific
# is hardcoded in this script.
GATEWAY_ENDPOINT=$(vault kv get -mount="$VAULT_MOUNT" -field="${VAULT_PREFIX}-gateway-endpoint" "$VAULT_PATH")
OIDC_ISSUER=$(vault kv get -mount="$VAULT_MOUNT" -field="${VAULT_PREFIX}-oidc-issuer" "$VAULT_PATH")
OIDC_CLIENT_ID=$(vault kv get -mount="$VAULT_MOUNT" -field="${VAULT_PREFIX}-oidc-client-id" "$VAULT_PATH")
OIDC_AUDIENCE=$(vault kv get -mount="$VAULT_MOUNT" -field="${VAULT_PREFIX}-oidc-audience" "$VAULT_PATH")

# Unique run id keeps concurrent E2E runs from colliding.
RUN_ID=$(date +%s)
PASS=0; FAIL=0

_info()  { printf '[e2e] %s\n' "$*"; }
_pass()  { printf '[e2e] PASS: %s\n' "$*"; PASS=$((PASS + 1)); }
_fail()  { printf '[e2e] FAIL: %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }

# ---------------------------------------------------------------------------
# Cleanup: remove any leftover e2e-nested-* sandboxes from the host.
# Uses --output json so the name field is unambiguous regardless of table format.
# Does not rely on the inner sandbox cleaning up after itself.
# ---------------------------------------------------------------------------
_cleanup() {
    _info "Cleaning up e2e-nested-* sandboxes..."
    local names
    names=$(openshellctl -g "$GATEWAY_NAME" sandbox list --output json 2>/dev/null \
        | python3 -c "
import sys, json
data = json.load(sys.stdin)
sandboxes = data if isinstance(data, list) else data.get('sandboxes', data.get('items', []))
for s in sandboxes:
    name = s.get('name', s.get('metadata', {}).get('name', ''))
    if name.startswith('e2e-nested-'):
        print(name)
" 2>/dev/null) || true
    if [[ -n "$names" ]]; then
        while IFS= read -r name; do
            [[ -n "$name" ]] || continue
            openshellctl -g "$GATEWAY_NAME" sandbox delete "$name" 2>/dev/null || true
        done <<< "$names"
    fi
    _info "Cleanup done."
}
trap '_cleanup' EXIT

# ---------------------------------------------------------------------------
# Helper: create an outer sandbox, wait for it, return its exit code.
# Extra args (zero or more --provider NAME pairs) are forwarded verbatim.
# ---------------------------------------------------------------------------
_run_sandbox() {
    local name=$1 script=$2
    shift 2
    _info "Starting sandbox: $name"
    openshellctl -g "$GATEWAY_NAME" sandbox create \
        --name "$name" \
        -f "$(dirname "$0")/nested-sandbox.yaml" \
        -p "NAME=$name" \
        -p "IMAGE=$IMAGE" \
        -p "SCRIPT=$script" \
        -p "GATEWAY_ENDPOINT=$GATEWAY_ENDPOINT" \
        -p "OIDC_ISSUER=$OIDC_ISSUER" \
        -p "OIDC_CLIENT_ID=$OIDC_CLIENT_ID" \
        -p "OIDC_AUDIENCE=$OIDC_AUDIENCE" \
        --no-keep --no-tty --no-auto-providers \
        "$@" 2>&1
}

# ---------------------------------------------------------------------------
# Check 1: Negative control — no provider → proxy must deny the OIDC call.
# A dummy client secret is injected so openshellctl actually attempts the
# token POST to Keycloak; without the openshell provider the proxy has no
# Keycloak endpoint in its allow-list and emits policy_denied.
# Any nonzero exit WITHOUT a proxy-shaped denial is a FAIL (it means the
# sandbox died before reaching the proxy — e.g. missing credential locally).
# ---------------------------------------------------------------------------
_info "=== Check 1: negative control (no provider) ==="
NAME1="e2e-nested-${RUN_ID}-1"
# Prefix OPENSHELL_OIDC_CLIENT_SECRET=e2e-dummy so openshellctl attempts the
# token POST; the proxy (no openshell provider attached) must deny it.
SCRIPT1="OPENSHELL_OIDC_CLIENT_SECRET=e2e-dummy openshellctl gateway add \"\$OPENSHELL_GATEWAY_ENDPOINT\" --name inner && openshellctl whoami"
set +e
output1=$(_run_sandbox "$NAME1" "$SCRIPT1" 2>&1)
exit1=$?
set -e

if echo "$output1" | grep -qiE "policy.denied|policy_denied|credential_endpoint_mismatch|forbidden|denied"; then
    _pass "Check 1: proxy denied the OIDC call as expected (exit=$exit1)"
else
    _fail "Check 1: no proxy-shaped denial in output — provider gate may not be enforced (exit=$exit1)"
    echo "--- output ---"
    echo "$output1"
fi

# ---------------------------------------------------------------------------
# Check 2: Deterministic — with openshell provider, sandbox creates child.
# ---------------------------------------------------------------------------
_info "=== Check 2: deterministic nested sandbox ==="
NAME2="e2e-nested-${RUN_ID}-2"
CHILD2="${NAME2}-child"
SCRIPT2=$(cat <<'INNERSCRIPT'
set -euo pipefail
openshellctl gateway add "$OPENSHELL_GATEWAY_ENDPOINT" --name inner
openshellctl doctor
openshellctl -g inner sandbox create \
    --name "${NAME}-child" \
    --from "$IMAGE" \
    --no-keep --no-tty --no-auto-providers \
    -- echo CHILD_OK
INNERSCRIPT
)

set +e
output2=$(_run_sandbox "$NAME2" "$SCRIPT2" --provider "rosa-agent-openshell" 2>&1)
exit2=$?
set -e

if [[ $exit2 -eq 0 ]] && echo "$output2" | grep -q "CHILD_OK"; then
    _pass "Check 2: child sandbox exited 0 and output contains CHILD_OK"
else
    _fail "Check 2: exit=$exit2, CHILD_OK=$(echo "$output2" | grep -c CHILD_OK || echo 0)"
    echo "--- output ---"
    echo "$output2"
fi

# ---------------------------------------------------------------------------
# Check 3: Agent-driven — Claude creates the child sandbox via openshellctl.
# Resolved values (non-secrets) are substituted directly so the Claude prompt
# has concrete values rather than unexpanded shell variable names.
# ---------------------------------------------------------------------------
_info "=== Check 3: agent-driven nested sandbox ==="
NAME3="e2e-nested-${RUN_ID}-3"
CHILD3="${NAME3}-child"
PROMPT3="Using openshellctl, register the gateway at ${GATEWAY_ENDPOINT} as 'inner', run openshellctl doctor, then create a sandbox named ${CHILD3} from image ${IMAGE} with --no-keep --no-tty --no-auto-providers whose command is \`echo CHILD_OK\`. Print the child's output verbatim and nothing else."
SCRIPT3="claude --dangerously-skip-permissions --print \"$PROMPT3\""

set +e
output3=$(_run_sandbox "$NAME3" "$SCRIPT3" \
    --provider "rosa-agent-openshell" \
    --provider "rosa-agent-vertex" 2>&1)
exit3=$?
set -e

if [[ $exit3 -eq 0 ]] && echo "$output3" | grep -q "CHILD_OK"; then
    _pass "Check 3: agent-driven child sandbox exited 0 and output contains CHILD_OK"
else
    _fail "Check 3: exit=$exit3, CHILD_OK=$(echo "$output3" | grep -c CHILD_OK || echo 0)"
    echo "--- output ---"
    echo "$output3"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
_info "Results: PASS=$PASS FAIL=$FAIL"
if [[ $FAIL -ne 0 ]]; then
    echo "[e2e] FAILED ($FAIL checks failed)" >&2
    exit 1
fi
echo "[e2e] All $PASS checks passed."
