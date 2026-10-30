"""Tests for provider-profiles/*.yaml shape.

Requires PyYAML (python3-pyyaml), which is a lint build-root dependency.
Tests are skipped when the module is absent so the baseline stays clean
in environments that have not installed it.
"""

import re
import unittest
from pathlib import Path

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

PROFILES_DIR = Path(__file__).parent.parent.parent / "provider-profiles"
PROFILE_PATH = PROFILES_DIR / "openshell.yaml"

# ProviderProfileCategory enum values at v0.1.5 (sdk/go/proto/openshellv1/openshell.pb.go).
# "infrastructure" is not in this enum — use "other" for categories that don't fit.
_VALID_CATEGORIES = frozenset(
    {"other", "inference", "agent", "source_control", "messaging", "data", "knowledge"}
)


def _is_keycloak(endpoint):
    return "keycloak." in endpoint.get("host", "")


def _is_gateway(endpoint):
    return "gw-openshell-" in endpoint.get("host", "")


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML required (install python3-pyyaml)")
class TestOpenshellProfileShape(unittest.TestCase):
    def setUp(self):
        with open(PROFILE_PATH, encoding="utf-8") as fh:
            self.profile = yaml.safe_load(fh)
        self.endpoints = self.profile.get("endpoints", [])

    # --- top-level fields ---

    def test_profile_file_exists(self):
        self.assertTrue(PROFILE_PATH.exists(), f"missing {PROFILE_PATH}")

    def test_id(self):
        self.assertEqual(self.profile["id"], "openshell")

    def test_category_is_valid_enum(self):
        cat = self.profile.get("category")
        self.assertIn(
            cat,
            _VALID_CATEGORIES,
            f"category {cat!r} is not a valid ProviderProfileCategory enum value; "
            f"valid values: {sorted(_VALID_CATEGORIES)}",
        )

    def test_inference_capable_false(self):
        self.assertIs(self.profile.get("inference_capable"), False)

    def test_binaries_exactly_openshellctl(self):
        self.assertEqual(
            self.profile.get("binaries"),
            ["/usr/local/bin/openshellctl"],
        )

    # --- credential ---

    def test_credential_env_var_present(self):
        creds = self.profile.get("credentials", [])
        all_env_vars = [v for c in creds for v in c.get("env_vars", [])]
        self.assertIn(
            "OPENSHELL_OIDC_CLIENT_SECRET",
            all_env_vars,
            "OPENSHELL_OIDC_CLIENT_SECRET must be declared as an env_var",
        )

    def test_credential_required(self):
        for cred in self.profile.get("credentials", []):
            if "OPENSHELL_OIDC_CLIENT_SECRET" in cred.get("env_vars", []):
                self.assertTrue(
                    cred.get("required"),
                    "OPENSHELL_OIDC_CLIENT_SECRET must be required: true",
                )

    # --- Keycloak endpoints ---

    def test_has_at_least_one_keycloak_endpoint(self):
        self.assertGreater(
            sum(1 for e in self.endpoints if _is_keycloak(e)),
            0,
            "at least one Keycloak endpoint required",
        )

    def test_keycloak_protocol_rest(self):
        for ep in self.endpoints:
            if _is_keycloak(ep):
                self.assertEqual(
                    ep.get("protocol"),
                    "rest",
                    f"Keycloak host {ep['host']} must have protocol: rest",
                )

    def test_keycloak_enforcement_enforce(self):
        for ep in self.endpoints:
            if _is_keycloak(ep):
                self.assertEqual(
                    ep.get("enforcement"),
                    "enforce",
                    f"Keycloak host {ep['host']} must have enforcement: enforce",
                )

    def test_keycloak_request_body_credential_rewrite(self):
        for ep in self.endpoints:
            if _is_keycloak(ep):
                self.assertTrue(
                    ep.get("request_body_credential_rewrite"),
                    f"Keycloak host {ep['host']} must have request_body_credential_rewrite: true",
                )

    def test_keycloak_exactly_two_rules(self):
        for ep in self.endpoints:
            if _is_keycloak(ep):
                rules = ep.get("rules", [])
                self.assertEqual(
                    len(rules),
                    2,
                    f"Keycloak host {ep['host']} must have exactly 2 rules, got {len(rules)}",
                )

    def test_keycloak_rule_get_discovery(self):
        for ep in self.endpoints:
            if _is_keycloak(ep):
                get_paths = [
                    r["allow"]["path"]
                    for r in ep.get("rules", [])
                    if r.get("allow", {}).get("method") == "GET"
                ]
                self.assertTrue(
                    any(".well-known/openid-configuration" in p for p in get_paths),
                    f"Keycloak host {ep['host']} must allow GET openid-configuration",
                )

    def test_keycloak_rule_post_token(self):
        for ep in self.endpoints:
            if _is_keycloak(ep):
                post_paths = [
                    r["allow"]["path"]
                    for r in ep.get("rules", [])
                    if r.get("allow", {}).get("method") == "POST"
                ]
                self.assertTrue(
                    any("openid-connect/token" in p for p in post_paths),
                    f"Keycloak host {ep['host']} must allow POST openid-connect/token",
                )

    # --- Gateway endpoints (L4 — no L7 fields) ---

    def test_has_at_least_one_gateway_endpoint(self):
        self.assertGreater(
            sum(1 for e in self.endpoints if _is_gateway(e)),
            0,
            "at least one gateway endpoint (gw-openshell-...) required",
        )

    def test_gateway_no_protocol(self):
        for ep in self.endpoints:
            if _is_gateway(ep):
                self.assertNotIn(
                    "protocol",
                    ep,
                    f"gateway host {ep['host']} must not have 'protocol' (must be L4)",
                )

    def test_gateway_no_access(self):
        for ep in self.endpoints:
            if _is_gateway(ep):
                self.assertNotIn(
                    "access",
                    ep,
                    f"gateway host {ep['host']} must not have 'access' field",
                )

    def test_gateway_no_rules(self):
        for ep in self.endpoints:
            if _is_gateway(ep):
                self.assertNotIn(
                    "rules",
                    ep,
                    f"gateway host {ep['host']} must not have 'rules' (L4 endpoint)",
                )

    def test_gateway_allow_uninspected_credentials(self):
        # The linter requires this field when the sandbox holds a declared
        # credential and the endpoint is L4. It is an explicit acknowledgement
        # that the credential does NOT travel to this host, not a security
        # relaxation. Without it, `openshell profile lint` fails.
        for ep in self.endpoints:
            if _is_gateway(ep):
                self.assertTrue(
                    ep.get("allow_uninspected_credentials"),
                    f"gateway host {ep['host']} must have allow_uninspected_credentials: true "
                    f"(linter requires explicit opt-in for L4 endpoints in credentialed profiles)",
                )

    def test_gateway_port_443(self):
        for ep in self.endpoints:
            if _is_gateway(ep):
                self.assertEqual(
                    ep.get("port"),
                    443,
                    f"gateway host {ep['host']} must have port: 443",
                )

    # --- placeholder literal guard ---

    def test_no_raw_placeholder_literal(self):
        content = PROFILE_PATH.read_text(encoding="utf-8")
        # Build the forbidden prefix dynamically so this file itself does not
        # contain the literal (check_placeholder_literals.py would flag it).
        # The spaced form (env :*) is safe; the unspaced form is forbidden.
        _forbidden = ":".join(["openshell", "resolve", "env"]) + ":"
        match = re.search(re.escape(_forbidden) + r"[^\s]", content, re.IGNORECASE)
        self.assertIsNone(
            match,
            "raw credential-placeholder literal found; use 'openshell:resolve:env :' with a space",
        )


@unittest.skipUnless(YAML_AVAILABLE, "PyYAML required (install python3-pyyaml)")
class TestAllProfileCategories(unittest.TestCase):
    """Every provider profile must use a valid ProviderProfileCategory enum value.

    Catches copy-paste errors (e.g. 'infrastructure') before the profile is
    linted against a live gateway, where the parse failure is less informative.
    Applies to every *.yaml in provider-profiles/ so new profiles are covered
    automatically.
    """

    def test_all_profiles_have_valid_category(self):
        yaml_files = sorted(PROFILES_DIR.glob("*.yaml"))
        self.assertTrue(yaml_files, f"no YAML files found in {PROFILES_DIR}")
        violations = []
        for path in yaml_files:
            with open(path, encoding="utf-8") as fh:
                profile = yaml.safe_load(fh)
            cat = profile.get("category") if isinstance(profile, dict) else None
            if cat not in _VALID_CATEGORIES:
                violations.append(
                    f"  {path.name}: category={cat!r} — "
                    f"must be one of {sorted(_VALID_CATEGORIES)}"
                )
        self.assertFalse(
            violations,
            "Invalid category in provider profile(s):\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
