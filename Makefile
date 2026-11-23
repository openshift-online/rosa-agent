CONTAINER_ENGINE ?= podman

# Fully-qualified sandbox image WITHOUT tag, e.g. quay.io/<org>/rosa-agent.
# Intentionally unset by default: there is no shared registry baked in, so a
# push must be given one explicitly (SANDBOX_IMAGE=... make sandbox-push).
SANDBOX_IMAGE ?=

# The tag is the short git SHA so every push is immutable and pods can pin an
# exact build; `latest` is also pushed as a moving convenience tag.
SANDBOX_TAG ?= $(shell git rev-parse --short=7 HEAD)
SANDBOX_CONTAINERFILE := Containerfile
SANDBOX_CONTEXT := .

.PHONY: lint markdown-lint check-placeholders lint-sandbox check-hypershell check-providers generate-hypershell test test-hack test-e2e-nested-sandbox sandbox-build sandbox-push require-sandbox-image providers provider providers-dry-run openshell-provider openshell-gateway openshell-vault-login

# Parent lint target: runs every linter. The OpenShift CI `ci/prow/lint`
# presubmit runs `make lint`. Add new linters (shell, yaml, ...) as their own
# targets and list them as prerequisites here.
#
# check-hypershell needs kustomize and PyYAML, which the `ci/prow/lint`
# build root provides. That build root is a dockerfile_literal in
# openshift/release, NOT a Dockerfile in this repo:
#   ci-operator/config/openshift-online/rosa-agent/
#     openshift-online-rosa-agent-main.yaml
# Adding a tool this target depends on means editing that file too.
lint: markdown-lint check-placeholders lint-sandbox check-hypershell check-providers

# Validate the .hypershell/ Kustomize tree: every build root renders, every
# rendered resource is an importable Kind with a metadata.name, and each
# overlay produces the gateway name its instance expects.
#
# This matters more than a normal lint because both consumers of this tree
# fail quietly. The import silently skips non-importable Kinds, and nothing
# ever writes Repository.status, so a broken tree looks identical to a
# working one from outside. `hsctl apply -k --dry-run` is not a substitute:
# it prints kind and name only and validates nothing server-side.
check-hypershell:
	python3 hack/check_hypershell.py

# Regenerate the policy JSON embedded in the rosa-agent SandboxTemplate.
# Run this after every edit to policies/default.yaml, then re-run
# check-hypershell to confirm the drift check passes.
generate-hypershell:
	python3 hack/generate_hypershell.py

# Lint all Markdown docs. Requires markdownlint-cli2 (npm i -g markdownlint-cli2).
# Rules live in .markdownlint-cli2.yaml.
markdown-lint:
	markdownlint-cli2 "**/*.md"

# Fail if any tracked file contains raw credential-placeholder literals
# that trip the supervisor's request-body marker guard.  Containerfile
# and the checker itself are allowlisted.
check-placeholders:
	python3 hack/check_placeholder_literals.py

# Guard against re-introduction of removed Bedrock/inference.local config.
# Covers sandbox files, docs, provider profiles, and policies. See MCS-324.
SANDBOX_FORBIDDEN := inference\.local|ANTHROPIC_API_KEY|ANTHROPIC_BASE_URL|rosa-general-vertex|vertex-a([^i]|$$)
SANDBOX_LINT_PATHS := sandbox/templates/ 'sandbox/skills/**/*-cron.yaml' README.md provider-profiles/ policies/ docs/
lint-sandbox:
	@git grep --extended-regexp --line-number '$(SANDBOX_FORBIDDEN)' -- $(SANDBOX_LINT_PATHS); \
	rc=$$?; \
	if [ $$rc -eq 0 ]; then echo "ERROR: forbidden Bedrock-era config found (MCS-324)"; exit 1; fi; \
	if [ $$rc -ne 1 ]; then echo "ERROR: git grep failed (rc=$$rc)"; exit $$rc; fi; \
	echo "lint-sandbox: OK"

# Run every skill's Python unit tests, then any bundled shell-script tests.
# Stdlib unittest only - this image has no pip, so no pytest/coverage
# tooling is available or expected. Each skill's own directory is put on
# sys.path (via unittest's -t), matching how `python3 -m <package>` already
# resolves imports for that skill. Shell-script tests (no bats/shunit2 in
# this image) are plain executable scripts under scripts/tests/ that report
# their own pass/fail and exit non-zero on failure.
test:
	@set -e; for d in sandbox/skills/*/; do \
		if compgen -G "$${d}tests/test_*.py" > /dev/null; then \
			echo "=== $$d ==="; \
			(cd "$$d" && python3 -m unittest discover -s tests -t .); \
		fi; \
		if compgen -G "$${d}scripts/tests/test_*.sh" > /dev/null; then \
			for t in "$$d"scripts/tests/test_*.sh; do \
				echo "=== $$t ==="; \
				bash "$$t"; \
			done; \
		fi; \
	done
	$(MAKE) test-hack

# Unit tests for Python scripts in hack/.
test-hack:
	@echo "=== hack/tests ==="; \
	PYTHONPATH=hack python3 -m unittest discover -s hack/tests

# Local build tag when no image is given, so `make sandbox-build` works without
# a registry. A push always requires SANDBOX_IMAGE (see require-sandbox-image).
SANDBOX_LOCAL_IMAGE := rosa-agent
SANDBOX_BUILD_IMAGE := $(if $(SANDBOX_IMAGE),$(SANDBOX_IMAGE),$(SANDBOX_LOCAL_IMAGE))

sandbox-build:
	$(CONTAINER_ENGINE) build \
		--tag $(SANDBOX_BUILD_IMAGE):$(SANDBOX_TAG) \
		--tag $(SANDBOX_BUILD_IMAGE):latest \
		--file $(SANDBOX_CONTAINERFILE) \
		$(SANDBOX_CONTEXT)

require-sandbox-image:
	@test -n "$(SANDBOX_IMAGE)" || { \
		echo "ERROR: SANDBOX_IMAGE is not set; refusing to push."; \
		echo "Set a fully-qualified image, e.g. SANDBOX_IMAGE=quay.io/<org>/rosa-agent make sandbox-push"; \
		exit 1; \
	}

sandbox-push: require-sandbox-image sandbox-build
	$(CONTAINER_ENGINE) push $(SANDBOX_IMAGE):$(SANDBOX_TAG)
	$(CONTAINER_ENGINE) push $(SANDBOX_IMAGE):latest
	@echo "Pushed $(SANDBOX_IMAGE):$(SANDBOX_TAG)"

# ---------------------------------------------------------------------------
# Provider provisioning — data-driven from provider-profiles/providers.yaml
#
# `make providers` provisions all five rosa-agent providers on the gateway
# idempotently: profile lint + import-or-update, provider create-or-update,
# and optional refresh configure + rotate. Vault-sourced secrets are read at
# runtime into environment variables and never appear in Make output or argv.
#
# Two CLIs are needed because openshellctl (Go) has no profile import/update
# (stubs), and the Rust openshell has no Vault integration. openshellctl
# bridges them by writing a token file that the Rust CLI reads
# (token refresh --write).
#
# Required exports (defaults shown):
#   VAULT_ADDR                           (no default — must be set)
#   OPENSHELL_VAULT_KV_MOUNT  ?= osd-sre
#   OPENSHELL_VAULT_KV_PATH   ?= rosa-agent
#   OPENSHELL_VAULT_FIELD_PREFIX ?= hypershell
# ---------------------------------------------------------------------------

OPENSHELL_GATEWAY_NAME    ?= rosa-agent
PROVIDERS_DESCRIPTOR      := provider-profiles/providers.yaml
OPENSHELL_VAULT_KV_MOUNT  ?= osd-sre
OPENSHELL_VAULT_KV_PATH   ?= rosa-agent
OPENSHELL_VAULT_FIELD_PREFIX ?= hypershell

# Provision all five providers. openshell-gateway handles Vault login,
# gateway registration, and token refresh --write so both CLIs can auth.
providers: openshell-gateway
	python3 hack/provision_providers.py \
		--descriptor $(PROVIDERS_DESCRIPTOR) \
		--gateway $(OPENSHELL_GATEWAY_NAME)

# Provision a single provider by name (e.g. PROVIDER=rosa-agent-github).
provider: openshell-gateway
	@test -n "$(PROVIDER)" || { echo "ERROR: PROVIDER is not set."; echo "Usage: make provider PROVIDER=rosa-agent-github"; exit 1; }
	python3 hack/provision_providers.py \
		--descriptor $(PROVIDERS_DESCRIPTOR) \
		--gateway $(OPENSHELL_GATEWAY_NAME) \
		--only $(PROVIDER)

# Show planned commands without executing (no gateway needed).
providers-dry-run:
	python3 hack/provision_providers.py \
		--descriptor $(PROVIDERS_DESCRIPTOR) \
		--gateway $(OPENSHELL_GATEWAY_NAME) \
		--dry-run

# Validate the providers.yaml descriptor (wired into make lint).
check-providers:
	python3 hack/provision_providers.py \
		--descriptor $(PROVIDERS_DESCRIPTOR) \
		--check

# Deprecated alias. Use `make providers` or `make provider PROVIDER=rosa-agent-openshell`.
openshell-provider:
	@echo "WARNING: 'make openshell-provider' is deprecated. Use 'make providers' or 'make provider PROVIDER=rosa-agent-openshell'." >&2
	$(MAKE) provider PROVIDER=rosa-agent-openshell

# Log in to Vault if no valid token exists. Does nothing if a valid token is
# already cached in ~/.vault-token or VAULT_TOKEN.
openshell-vault-login:
	@vault token lookup >/dev/null 2>&1 || vault login -method=oidc

# Add the gateway to the local openshellctl registry if not already present,
# then refresh the token so the Rust openshell CLI can use it. The token file
# written by --write is also used by pin_gateway_token in providers.yaml to
# avoid OPENSHELL_OIDC_CLIENT_SECRET being interpreted as openshellctl's own
# auth credential (vaultauth.go:171 / pkg/auth/resolve.go:72).
openshell-gateway: openshell-vault-login
	@command -v openshellctl >/dev/null 2>&1 || { echo "ERROR: openshellctl not found on PATH"; exit 1; }
	@command -v openshell   >/dev/null 2>&1 || { echo "ERROR: openshell not found on PATH (Rust CLI required for profile import)"; exit 1; }
	openshellctl gateway list --output json 2>/dev/null \
	  | python3 -c 'import json,sys; gws=json.load(sys.stdin); gws=gws if isinstance(gws,list) else gws.get("gateways",gws.get("items",[])); sys.exit(0 if any(g.get("name")=="'"$(OPENSHELL_GATEWAY_NAME)"'" for g in gws) else 1)' \
	  2>/dev/null || \
	  openshellctl gateway add \
	    "$$(vault kv get -mount=$(OPENSHELL_VAULT_KV_MOUNT) -field=$(OPENSHELL_VAULT_FIELD_PREFIX)-gateway-endpoint $(OPENSHELL_VAULT_KV_PATH))" \
	    --name $(OPENSHELL_GATEWAY_NAME) \
	    --vault-kv-mount $(OPENSHELL_VAULT_KV_MOUNT) \
	    --vault-kv-path $(OPENSHELL_VAULT_KV_PATH) \
	    --vault-field-prefix $(OPENSHELL_VAULT_FIELD_PREFIX)
	openshellctl -g $(OPENSHELL_GATEWAY_NAME) token refresh --write --no-browser

# ---------------------------------------------------------------------------
# E2E tests — human-run only; Prow/Konflux cannot reach the HyperShell OIDC
# endpoint, so this target is excluded from `make test`. Requires: VAULT_ADDR
# plus the same OPENSHELL_VAULT_* exports as the provisioning targets above.
# ---------------------------------------------------------------------------

# Nested-sandbox E2E: three ordered checks proving the openshell profile gates
# access correctly and that a sandbox can create a child sandbox.
test-e2e-nested-sandbox: openshell-vault-login
	bash hack/e2e/nested_sandbox.sh

# ---------------------------------------------------------------------------
# Scheduled jobs
#
# CronJobs are deployed directly to the rosaeng cluster (not via Konflux)
# because Konflux build clusters cannot reach the Hypershell OIDC endpoint.
# Each CronJob's manifest lives alongside its job skill and contains an
# openshellctl script (gateway add, doctor preflight, sandbox create via
# sandbox/templates/noninteractive.yaml):
#   sandbox/skills/job-sop-improve/job-sop-improve-cron.yaml
#   sandbox/skills/job-image-vuln-check/job-image-vuln-check-cron.yaml
#   sandbox/skills/job-ops-sop-pr-review/job-ops-sop-pr-review-cron.yaml
# Apply each manually with `oc apply -f` plus the openshell-oidc secret.
# ---------------------------------------------------------------------------
