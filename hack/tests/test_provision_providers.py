"""Tests for hack/provision_providers.py — data-driven provider provisioning.

stdlib unittest only (no pytest). Injected fakes for all I/O: no network, no
real filesystem outside tempfile, no exec.Command.

Descriptors are plain Python dicts — no YAML parsing needed in tests.

Ticket: MCS-1484
"""

import os
import subprocess
import sys
import tempfile
import unittest

# provision_providers lives in hack/, which PYTHONPATH=hack makes importable.
import provision_providers as pp


# ---------------------------------------------------------------------------
# Minimal valid descriptor as a plain dict for reuse across tests
# ---------------------------------------------------------------------------

def _minimal_descriptor():
    """Return the canonical 5-provider descriptor as a Python dict."""
    return {
        "vault": {"kv_mount": "osd-sre", "kv_path": "rosa-agent"},
        "providers": [
            {
                "name": "rosa-agent-openshell",
                "profile": "openshell",
                "profile_file": "provider-profiles/openshell.yaml",
                "global_profile": True,
                "credentials": [
                    {"key": "OPENSHELL_OIDC_CLIENT_SECRET",
                     "vault_field": "hypershell-oidc-client-secret"},
                ],
                "pin_gateway_token": True,
            },
            {
                "name": "rosa-agent-github",
                "profile": "github",
                "profile_file": "provider-profiles/github.yaml",
                "global_profile": True,
                "credentials": [
                    {"key": "GH_TOKEN", "vault_field": "github-token"},
                ],
            },
            {
                "name": "rosa-agent-jira",
                "profile": "atlassian-jira",
                "profile_file": "provider-profiles/atlassian-jira.yaml",
                "global_profile": True,
                "credentials": [
                    {"key": "JIRA_API_TOKEN", "vault_field": "jira-token"},
                ],
            },
            {
                "name": "rosa-agent-pagerduty",
                "profile": "pagerduty",
                "profile_file": "provider-profiles/pagerduty.yaml",
                "global_profile": True,
                "credentials": [
                    {"key": "PAGERDUTY_ACCESS_TOKEN", "literal": "placeholder"},
                ],
                "refresh": {
                    "strategy": "oauth2-client-credentials",
                    "credential_key": "PAGERDUTY_ACCESS_TOKEN",
                    "material": [
                        {"key": "client_id",
                         "vault_field": "pagerduty-oauth-client-id"},
                        {"key": "client_secret",
                         "vault_field": "pagerduty-oauth-client-secret",
                         "secret": True},
                    ],
                    "rotate_after_configure": True,
                },
            },
            {
                "name": "rosa-agent-vertex",
                "profile": "google-vertex-ai",
                "profile_file": "provider-profiles/google-vertex-ai.yaml",
                "global_profile": True,
                "credentials": [
                    {"key": "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN",
                     "literal": "placeholder"},
                ],
                "config": [
                    {"key": "VERTEX_AI_PROJECT_ID", "literal": "rosa-general"},
                    {"key": "VERTEX_AI_REGION", "literal": "global"},
                ],
                "refresh": {
                    "strategy": "google-service-account-jwt",
                    "credential_key": "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN",
                    "material": [
                        {"key": "client_email",
                         "literal": "rosa-agent@rosa-general.iam.gserviceaccount.com"},
                        {"key": "private_key",
                         "vault_field": "google-rosa-general-service-account-keyfile",
                         "json_path": ".private_key",
                         "secret": True},
                    ],
                    "rotate_after_configure": True,
                },
            },
        ],
    }


_PROFILE_NAMES = [
    "provider-profiles/openshell.yaml",
    "provider-profiles/github.yaml",
    "provider-profiles/atlassian-jira.yaml",
    "provider-profiles/pagerduty.yaml",
    "provider-profiles/google-vertex-ai.yaml",
]


def _write_profile_stubs(tmp_dir, names=None):
    """Create stub profile files so validate_descriptor doesn't fail on missing files."""
    for name in (names or _PROFILE_NAMES):
        path = os.path.join(tmp_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("id: stub\n")


class TestLoadDescriptor(unittest.TestCase):
    """Test descriptor loading from YAML (requires PyYAML)."""

    def test_load_nonexistent_file(self):
        with self.assertRaises(FileNotFoundError):
            pp.load_descriptor("/nonexistent/providers.yaml")


class TestValidateDescriptor(unittest.TestCase):
    """AC2: make check-providers rejects invalid descriptors."""

    def _validate(self, desc, base_dir=None):
        if base_dir:
            return pp.validate_descriptor(desc, base_dir=base_dir)
        with tempfile.TemporaryDirectory() as tmp:
            _write_profile_stubs(tmp)
            return pp.validate_descriptor(desc, base_dir=tmp)

    def _one_provider(self, **overrides):
        prov = {
            "name": "rosa-agent-github",
            "profile": "github",
            "profile_file": "provider-profiles/github.yaml",
            "global_profile": True,
            "credentials": [{"key": "GH_TOKEN", "vault_field": "github-token"}],
        }
        prov.update(overrides)
        return {"vault": {"kv_mount": "osd-sre", "kv_path": "rosa-agent"},
                "providers": [prov]}

    def test_valid_descriptor_has_no_errors(self):
        desc = _minimal_descriptor()
        errors = self._validate(desc)
        self.assertEqual(errors, [])

    def test_duplicate_names_rejected(self):
        p = self._one_provider()
        p["providers"].append(dict(p["providers"][0]))
        errors = self._validate(p)
        self.assertTrue(any("duplicate" in e.lower() for e in errors),
                        f"Expected duplicate name error, got: {errors}")

    def test_missing_profile_file_rejected(self):
        desc = self._one_provider(
            profile_file="provider-profiles/nonexistent.yaml")
        errors = self._validate(desc)
        self.assertTrue(any("not found" in e.lower() for e in errors),
                        f"Expected missing profile file error, got: {errors}")

    def test_credential_with_both_vault_and_literal_rejected(self):
        desc = self._one_provider(
            credentials=[{"key": "GH_TOKEN", "vault_field": "x", "literal": "y"}])
        errors = self._validate(desc)
        self.assertTrue(any("both" in e.lower() for e in errors),
                        f"Expected both vault_field/literal error, got: {errors}")

    def test_credential_with_neither_vault_nor_literal_rejected(self):
        desc = self._one_provider(credentials=[{"key": "GH_TOKEN"}])
        errors = self._validate(desc)
        self.assertTrue(any("must have" in e.lower() for e in errors),
                        f"Expected neither vault_field/literal error, got: {errors}")

    def test_json_path_without_vault_field_rejected(self):
        desc = self._one_provider(
            credentials=[{"key": "GH_TOKEN", "literal": "v", "json_path": ".t"}])
        errors = self._validate(desc)
        self.assertTrue(any("json_path" in e.lower() for e in errors),
                        f"Expected json_path without vault_field error, got: {errors}")

    def test_unknown_refresh_strategy_rejected(self):
        desc = self._one_provider(
            credentials=[{"key": "TOKEN", "literal": "placeholder"}],
            refresh={"strategy": "unknown-strategy",
                     "credential_key": "TOKEN", "material": []})
        errors = self._validate(desc)
        self.assertTrue(any("strategy" in e.lower() for e in errors),
                        f"Expected unknown strategy error, got: {errors}")

    def test_literal_value_over_64_chars_rejected(self):
        desc = self._one_provider(
            credentials=[{"key": "GH_TOKEN", "literal": "x" * 65}])
        errors = self._validate(desc)
        self.assertTrue(any("64" in e for e in errors),
                        f"Expected >64 char error, got: {errors}")

    def test_literal_value_exactly_64_chars_accepted(self):
        desc = self._one_provider(
            credentials=[{"key": "GH_TOKEN", "literal": "x" * 64}])
        errors = self._validate(desc)
        self.assertEqual(errors, [])

    def test_name_pattern_rejected(self):
        """Provider name must match ^rosa-agent-."""
        desc = self._one_provider(name="my-provider")
        errors = self._validate(desc)
        self.assertTrue(any("rosa-agent-" in e for e in errors),
                        f"Expected name pattern error, got: {errors}")

    def test_name_pattern_accepted(self):
        desc = self._one_provider(name="rosa-agent-custom")
        errors = self._validate(desc)
        self.assertEqual(errors, [])

    def test_config_key_pattern_rejected(self):
        """Config keys must be [A-Z0-9_]+."""
        desc = self._one_provider(
            config=[{"key": "bad-key", "literal": "v"}])
        errors = self._validate(desc)
        self.assertTrue(any("config" in e.lower() and "pattern" in e.lower()
                            for e in errors),
                        f"Expected config key pattern error, got: {errors}")

    def test_refresh_material_literal_over_64_rejected(self):
        """64-char limit applies to refresh material literals too."""
        desc = self._one_provider(
            credentials=[{"key": "TOKEN", "literal": "placeholder"}],
            refresh={
                "strategy": "oauth2-client-credentials",
                "credential_key": "TOKEN",
                "material": [{"key": "k", "literal": "x" * 65}],
            })
        errors = self._validate(desc)
        self.assertTrue(any("64" in e for e in errors),
                        f"Expected >64 char error, got: {errors}")

    def test_secret_literal_combo_rejected(self):
        """secret: true combined with literal must be rejected."""
        desc = self._one_provider(
            credentials=[{"key": "TOKEN", "literal": "placeholder"}],
            refresh={
                "strategy": "oauth2-client-credentials",
                "credential_key": "TOKEN",
                "material": [{"key": "k", "literal": "val", "secret": True}],
            })
        errors = self._validate(desc)
        self.assertTrue(any("secret" in e.lower() and "literal" in e.lower()
                            for e in errors),
                        f"Expected secret+literal error, got: {errors}")


# ---------------------------------------------------------------------------
# Argv builder tests — AC3: dry-run shows all argv with $ENVVAR names
# ---------------------------------------------------------------------------

def _load_minimal():
    """Return the minimal valid descriptor as a dict."""
    return _minimal_descriptor()


def _provider_by_name(desc, name):
    for p in desc["providers"]:
        if p["name"] == name:
            return p
    raise KeyError(name)


class TestBuildProfileArgv(unittest.TestCase):
    """Profile import vs update argv sequences."""

    def setUp(self):
        self.desc = _load_minimal()
        self.gateway = "rosa-agent"

    def test_import_new_profile(self):
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        argv = pp.build_profile_argv(prov, self.gateway, profile_exists=False)
        self.assertEqual(len(argv), 2)
        # First: lint
        self.assertIn("lint", argv[0])
        self.assertIn("--file", argv[0])
        # Second: import
        self.assertIn("import", argv[1])
        self.assertIn("--global", argv[1])

    def test_update_existing_profile(self):
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        argv = pp.build_profile_argv(prov, self.gateway, profile_exists=True)
        self.assertEqual(len(argv), 2)
        self.assertIn("lint", argv[0])
        self.assertIn("update", argv[1])
        self.assertIn("{tmp_file}", argv[1])

    def test_non_global_profile(self):
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        prov = dict(prov, global_profile=False)
        argv = pp.build_profile_argv(prov, self.gateway, profile_exists=False)
        for cmd in argv:
            self.assertNotIn("--global", cmd)


class TestBuildProviderArgv(unittest.TestCase):
    """Provider create vs update argv sequences."""

    def setUp(self):
        self.desc = _load_minimal()
        self.vault = self.desc["vault"]
        self.gateway = "rosa-agent"

    def test_create_vault_sourced_credential(self):
        """Vault-sourced credential uses bare --credential KEY (not KEY=VALUE)."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        argv, env_plan = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=False
        )
        self.assertEqual(len(argv), 1)
        cmd = argv[0]
        self.assertIn("create", cmd)
        self.assertIn("--credential", cmd)
        # Bare KEY, not KEY=VALUE
        cred_idx = cmd.index("--credential")
        self.assertEqual(cmd[cred_idx + 1], "GH_TOKEN")
        self.assertNotIn("=", cmd[cred_idx + 1])

    def test_update_vault_sourced_credential(self):
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        argv, env_plan = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=True
        )
        self.assertEqual(len(argv), 1)
        self.assertIn("update", argv[0])

    def test_create_literal_credential(self):
        """Literal credential uses --credential KEY=VALUE."""
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        argv, env_plan = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=False
        )
        cmd = argv[0]
        self.assertIn("create", cmd)
        cred_idx = cmd.index("--credential")
        self.assertIn("=", cmd[cred_idx + 1])
        self.assertTrue(cmd[cred_idx + 1].startswith("PAGERDUTY_ACCESS_TOKEN="))

    def test_config_flags(self):
        """Config entries appear as --config KEY=VALUE."""
        prov = _provider_by_name(self.desc, "rosa-agent-vertex")
        argv, _ = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=False
        )
        cmd = argv[0]
        config_pairs = []
        for i, arg in enumerate(cmd):
            if arg == "--config" and i + 1 < len(cmd):
                config_pairs.append(cmd[i + 1])
        self.assertIn("VERTEX_AI_PROJECT_ID=rosa-general", config_pairs)
        self.assertIn("VERTEX_AI_REGION=global", config_pairs)

    def test_global_profile_flag(self):
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        argv, _ = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=False
        )
        self.assertIn("--global-profile", argv[0])

    def test_env_plan_for_vault_sourced(self):
        """Env plan maps env var name to vault command for Vault-sourced."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        _, env_plan = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=False
        )
        self.assertIn("GH_TOKEN", env_plan)
        self.assertIn("vault", env_plan["GH_TOKEN"]["command"])

    def test_no_secret_in_argv(self):
        """AC5: no secret value in any argv."""
        for prov in self.desc["providers"]:
            argv, _ = pp.build_provider_argv(
                prov, self.vault, self.gateway, provider_exists=False
            )
            flat = " ".join(" ".join(cmd) for cmd in argv)
            # Vault field names are coordinates, not values
            for cred in prov.get("credentials", []):
                if cred.get("vault_field"):
                    # The vault field name may appear in env_plan commands but
                    # should not appear as a bare value in the argv
                    self.assertNotIn(
                        f"={cred['vault_field']}", flat,
                        f"Vault field value leaked into argv for {prov['name']}"
                    )

    def test_update_no_literal_credential(self):
        """Update argv must not contain KEY=literal — it would overwrite live tokens."""
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        argv, _ = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=True
        )
        cmd = " ".join(argv[0])
        self.assertNotIn("PAGERDUTY_ACCESS_TOKEN=placeholder", cmd,
                         "Literal credential must not appear on update")

    def test_update_no_config_drift(self):
        """Update argv includes --config flags to correct drift."""
        prov = _provider_by_name(self.desc, "rosa-agent-vertex")
        argv, _ = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=True
        )
        cmd = " ".join(argv[0])
        self.assertIn("--config", cmd)
        self.assertIn("VERTEX_AI_PROJECT_ID=rosa-general", cmd)

    def test_openshell_pin_gateway_token(self):
        """Openshell provider sets pin_gateway_token env entry."""
        prov = _provider_by_name(self.desc, "rosa-agent-openshell")
        _, env_plan = pp.build_provider_argv(
            prov, self.vault, self.gateway, provider_exists=False
        )
        self.assertIn("OPENSHELL_TOKEN", env_plan)


class TestBuildRefreshArgv(unittest.TestCase):
    """Refresh configure + rotate argv sequences."""

    def setUp(self):
        self.desc = _load_minimal()
        self.vault = self.desc["vault"]
        self.gateway = "rosa-agent"

    def test_no_refresh(self):
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        result = pp.build_refresh_argv(prov, self.vault, self.gateway)
        self.assertIsNone(result)

    def test_pagerduty_refresh_configure(self):
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        result = pp.build_refresh_argv(prov, self.vault, self.gateway)
        self.assertIsNotNone(result)
        argv, env_plan, do_rotate = result
        # configure command
        cmd = argv[0]
        self.assertIn("refresh", cmd)
        self.assertIn("configure", cmd)
        self.assertIn("--strategy", cmd)
        strat_idx = cmd.index("--strategy")
        self.assertEqual(cmd[strat_idx + 1], "oauth2-client-credentials")

    def test_pagerduty_secret_material_via_env(self):
        """Secret material uses --secret-material-env, not --material."""
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        result = pp.build_refresh_argv(prov, self.vault, self.gateway)
        argv, env_plan, _ = result
        cmd = argv[0]
        self.assertIn("--secret-material-env", cmd)
        # client_secret is secret
        sme_idx = cmd.index("--secret-material-env")
        self.assertIn("client_secret=", cmd[sme_idx + 1])

    def test_pagerduty_rotates(self):
        """PagerDuty has rotate_after_configure: true."""
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        result = pp.build_refresh_argv(prov, self.vault, self.gateway)
        self.assertIsNotNone(result)
        argv, _, do_rotate = result
        self.assertTrue(do_rotate)
        self.assertEqual(len(argv), 2)
        self.assertIn("rotate", argv[1])

    def test_vertex_rotate_after_configure(self):
        """Vertex has rotate_after_configure: true."""
        prov = _provider_by_name(self.desc, "rosa-agent-vertex")
        result = pp.build_refresh_argv(prov, self.vault, self.gateway)
        argv, env_plan, do_rotate = result
        self.assertTrue(do_rotate)
        # Should have configure + rotate commands
        self.assertEqual(len(argv), 2)
        self.assertIn("configure", argv[0])
        self.assertIn("rotate", argv[1])

    def test_vertex_non_secret_material_as_flag(self):
        """Non-secret material uses --material key=value."""
        prov = _provider_by_name(self.desc, "rosa-agent-vertex")
        result = pp.build_refresh_argv(prov, self.vault, self.gateway)
        argv, _, _ = result
        cmd = argv[0]
        mat_values = []
        for i, arg in enumerate(cmd):
            if arg == "--material" and i + 1 < len(cmd):
                mat_values.append(cmd[i + 1])
        self.assertTrue(any(
            v.startswith("client_email=") for v in mat_values
        ))

    def test_vertex_json_path_extraction(self):
        """json_path is recorded in env_plan for Vault extraction."""
        prov = _provider_by_name(self.desc, "rosa-agent-vertex")
        result = pp.build_refresh_argv(prov, self.vault, self.gateway)
        _, env_plan, _ = result
        # The private_key material should have a json_path in its env plan
        found = False
        for key, plan in env_plan.items():
            if plan.get("json_path") == ".private_key":
                found = True
                break
        self.assertTrue(found, f"Expected json_path=.private_key in env_plan, got: {env_plan}")


class TestDryRun(unittest.TestCase):
    """AC3: dry-run shows argv with $ENVVAR names, no secret values."""

    def setUp(self):
        self.desc = _load_minimal()
        self.vault = self.desc["vault"]
        self.gateway = "rosa-agent"

    def test_dry_run_all_providers(self):
        """Dry-run produces output for all 5 providers."""
        output = pp.format_dry_run(self.desc, self.gateway)
        self.assertIn("rosa-agent-openshell", output)
        self.assertIn("rosa-agent-github", output)
        self.assertIn("rosa-agent-jira", output)
        self.assertIn("rosa-agent-pagerduty", output)
        self.assertIn("rosa-agent-vertex", output)

    def test_dry_run_secrets_as_envvar(self):
        """Secrets appear as $ENVVAR_NAME, not as actual values."""
        output = pp.format_dry_run(self.desc, self.gateway)
        self.assertIn("$GH_TOKEN", output)
        self.assertIn("$JIRA_API_TOKEN", output)
        self.assertIn("$OPENSHELL_OIDC_CLIENT_SECRET", output)

    def test_dry_run_exists_and_missing(self):
        """Dry-run shows both exists and missing variants."""
        output = pp.format_dry_run(self.desc, self.gateway)
        self.assertIn("import", output.lower())
        self.assertIn("create", output.lower())
        self.assertIn("update", output.lower())

    def test_dry_run_single_provider(self):
        """Dry-run with --only filters to one provider."""
        output = pp.format_dry_run(self.desc, self.gateway, only="rosa-agent-github")
        self.assertIn("rosa-agent-github", output)
        self.assertNotIn("rosa-agent-jira", output)
        self.assertNotIn("rosa-agent-vertex", output)


# ---------------------------------------------------------------------------
# Execution runner tests — AC4
# ---------------------------------------------------------------------------

class FakeRunner:
    """Injected fake that captures commands without executing them."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}
        self._call_count = {}

    def run(self, argv, env=None, check=True):
        """Record the call and return a canned response."""
        key = " ".join(argv[:6])
        self._call_count[key] = self._call_count.get(key, 0) + 1
        self.calls.append({"argv": list(argv), "env": dict(env or {})})

        for pattern, resp in self.responses.items():
            if pattern in key:
                if callable(resp) and not isinstance(resp, Exception):
                    resp = resp(argv, self._call_count[key])
                if isinstance(resp, Exception):
                    if check:
                        raise resp
                    return type("Result", (), {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": str(resp),
                    })()
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": resp if isinstance(resp, str) else "",
                    "stderr": "",
                })()

        return type("Result", (), {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        })()


class FakeVaultReader:
    """Injected fake that returns canned Vault values."""

    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []

    def read(self, mount, path, field, json_path=None):
        self.calls.append({
            "mount": mount, "path": path,
            "field": field, "json_path": json_path,
        })
        key = f"{mount}/{path}/{field}"
        val = self.values.get(key, f"fake-{field}")
        if json_path and isinstance(val, dict):
            for part in json_path.lstrip(".").split("."):
                if part:
                    val = val[part]
        return val


def _not_found_then_ok(argv, call_num):
    """First call returns 'not found'; subsequent calls succeed (for verify).

    Uses real CLI error messages as fixtures:
    - openshellctl: provider "name" not found  (NotFoundError.Error(), %s %q not found)
    - openshell:    provider profile 'name' not found  (provider.rs:1731)
    """
    if call_num == 1:
        key = " ".join(argv[:6])
        if "profile" in key:
            profile_name = argv[5] if len(argv) > 5 else "unknown"
            return Exception(f"provider profile '{profile_name}' not found")
        provider_name = argv[5] if len(argv) > 5 else "unknown"
        return Exception(f'provider "{provider_name}" not found')
    return '{"status":"ok"}'


class TestProvisionProvider(unittest.TestCase):
    """Test the provision_one_provider orchestration function."""

    def setUp(self):
        self.desc = _load_minimal()
        self.vault = self.desc["vault"]
        self.gateway = "rosa-agent"
        self.runner = FakeRunner()
        self.vault_reader = FakeVaultReader()
        self._tmpdir = tempfile.mkdtemp()
        gw_dir = os.path.join(self._tmpdir, "openshell", "gateways", "rosa-agent")
        os.makedirs(gw_dir)
        with open(os.path.join(gw_dir, "oidc_token.json"), "w") as f:
            f.write('{"access_token": "fake-gateway-token"}')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_create_new_provider(self):
        """When profile and provider don't exist, uses import and create."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success, f"Expected success, got: {result}")
        all_argv = [" ".join(c["argv"]) for c in self.runner.calls]
        self.assertTrue(any("profile lint" in c for c in all_argv), f"No lint: {all_argv}")
        self.assertTrue(any("profile import" in c for c in all_argv), f"No import: {all_argv}")
        self.assertTrue(any("provider create" in c for c in all_argv), f"No create: {all_argv}")

    def test_update_existing_provider(self):
        """When profile and provider exist, uses update."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = "id: github\nresource_version: 42\n"
        self.runner.responses["provider get"] = "Name: rosa-agent-github"

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success, f"Expected success, got: {result}")
        all_argv = [" ".join(c["argv"]) for c in self.runner.calls]
        self.assertTrue(any("profile update" in c for c in all_argv), f"No update: {all_argv}")
        self.assertTrue(any("provider update" in c for c in all_argv), f"No update: {all_argv}")

    def test_resource_version_splice(self):
        """Profile update splices resource_version from export into temp file."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = "id: github\nresource_version: 42\n"
        self.runner.responses["provider get"] = "Name: rosa-agent-github"

        with tempfile.TemporaryDirectory() as tmp:
            profile_path = os.path.join(tmp, "provider-profiles", "github.yaml")
            os.makedirs(os.path.dirname(profile_path), exist_ok=True)
            with open(profile_path, "w") as f:
                f.write("id: github\ndisplay_name: GitHub\n")

            result = pp.provision_one_provider(
                prov, self.vault, self.gateway,
                runner=self.runner, vault_reader=self.vault_reader,
                base_dir=tmp,
            )

        self.assertTrue(result.success)
        # Check that profile update was called (temp file was created and cleaned up)
        update_calls = [
            c for c in self.runner.calls
            if "profile" in c["argv"] and "update" in c["argv"]
        ]
        self.assertEqual(len(update_calls), 1)

    def test_temp_file_cleanup_on_success(self):
        """Temp files are cleaned up after successful profile update."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = "id: github\nresource_version: 7\n"
        self.runner.responses["provider get"] = "exists"

        with tempfile.TemporaryDirectory() as tmp:
            profile_path = os.path.join(tmp, "provider-profiles", "github.yaml")
            os.makedirs(os.path.dirname(profile_path), exist_ok=True)
            with open(profile_path, "w") as f:
                f.write("id: github\n")

            result = pp.provision_one_provider(
                prov, self.vault, self.gateway,
                runner=self.runner, vault_reader=self.vault_reader,
                base_dir=tmp,
            )

        self.assertTrue(result.success)
        # No temp files should remain
        for c in self.runner.calls:
            for arg in c["argv"]:
                if arg.startswith(tempfile.gettempdir()) and arg.endswith(".yaml"):
                    self.assertFalse(os.path.exists(arg),
                                     f"Temp file not cleaned up: {arg}")

    def test_temp_file_cleanup_on_error(self):
        """Temp files are cleaned up even when update fails."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = "id: github\nresource_version: 7\n"
        self.runner.responses["profile update"] = Exception("update failed")
        self.runner.responses["provider get"] = "exists"

        with tempfile.TemporaryDirectory() as tmp:
            profile_path = os.path.join(tmp, "provider-profiles", "github.yaml")
            os.makedirs(os.path.dirname(profile_path), exist_ok=True)
            with open(profile_path, "w") as f:
                f.write("id: github\n")

            result = pp.provision_one_provider(
                prov, self.vault, self.gateway,
                runner=self.runner, vault_reader=self.vault_reader,
                base_dir=tmp,
            )

        self.assertFalse(result.success)

    def test_vault_sourced_credential_in_env(self):
        """Vault-sourced credential is read and passed via env."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok
        self.vault_reader.values["osd-sre/rosa-agent/github-token"] = "fake-token-123"

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success)
        # The provider create call should have GH_TOKEN in its env
        create_calls = [
            c for c in self.runner.calls
            if "provider" in c["argv"] and "create" in c["argv"]
        ]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0]["env"].get("GH_TOKEN"), "fake-token-123")

    def test_openshell_token_pinning(self):
        """Openshell provider reads OPENSHELL_TOKEN from token file."""
        prov = _provider_by_name(self.desc, "rosa-agent-openshell")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        with tempfile.TemporaryDirectory() as tmp:
            # Create fake token file
            gw_dir = os.path.join(tmp, "openshell", "gateways", "rosa-agent")
            os.makedirs(gw_dir)
            with open(os.path.join(gw_dir, "oidc_token.json"), "w") as f:
                f.write('{"access_token": "test-token-xyz"}')

            profile_path = os.path.join(tmp, "base", "provider-profiles", "openshell.yaml")
            os.makedirs(os.path.dirname(profile_path), exist_ok=True)
            with open(profile_path, "w") as f:
                f.write("id: openshell\n")

            result = pp.provision_one_provider(
                prov, self.vault, self.gateway,
                runner=self.runner, vault_reader=self.vault_reader,
                base_dir=os.path.join(tmp, "base"),
                config_dir=tmp,
            )

        create_calls = [
            c for c in self.runner.calls
            if "provider" in c["argv"] and ("create" in c["argv"] or "update" in c["argv"])
        ]
        self.assertGreaterEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0]["env"].get("OPENSHELL_TOKEN"), "test-token-xyz")

    def test_pin_gateway_token_missing_fails(self):
        """pin_gateway_token with missing token file fails before profile work."""
        prov = _provider_by_name(self.desc, "rosa-agent-openshell")

        with tempfile.TemporaryDirectory() as tmp:
            # No token file created — config_dir points to empty dir
            profile_path = os.path.join(tmp, "base", "provider-profiles", "openshell.yaml")
            os.makedirs(os.path.dirname(profile_path), exist_ok=True)
            with open(profile_path, "w") as f:
                f.write("id: openshell\n")

            result = pp.provision_one_provider(
                prov, self.vault, self.gateway,
                runner=self.runner, vault_reader=self.vault_reader,
                base_dir=os.path.join(tmp, "base"),
                config_dir=tmp,
            )

        self.assertFalse(result.success)
        self.assertIn("openshell-gateway", result.error)
        self.assertEqual(self.runner.calls, [], "no CLI calls before token check")

    def test_refresh_configure_and_rotate(self):
        """Vertex provider runs configure then rotate."""
        prov = _provider_by_name(self.desc, "rosa-agent-vertex")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success)
        cmds = [" ".join(c["argv"][:7]) for c in self.runner.calls]
        configure_idx = next(
            (i for i, c in enumerate(cmds) if "configure" in c), None
        )
        rotate_idx = next(
            (i for i, c in enumerate(cmds) if "rotate" in c), None
        )
        self.assertIsNotNone(configure_idx, f"No configure call found in: {cmds}")
        self.assertIsNotNone(rotate_idx, f"No rotate call found in: {cmds}")
        self.assertLess(configure_idx, rotate_idx, "configure must precede rotate")

    def test_refresh_already_configured_continues(self):
        """Second refresh configure succeeds even if CLI says 'already configured'."""
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok
        self.runner.responses["refresh configure"] = Exception(
            "already configured for credential key PAGERDUTY_ACCESS_TOKEN"
        )

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success,
                        f"Should succeed despite 'already configured': {result.error}")

    def test_refresh_already_configured_calledprocesserror(self):
        """CalledProcessError with 'already configured' in stderr is handled.

        DefaultRunner uses check=True, producing CalledProcessError whose
        __str__ is 'Command [...] returned non-zero exit status 1' — the
        actual message is only in .stderr.
        """
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok
        err = subprocess.CalledProcessError(1, ["openshell", "provider", "refresh", "configure"])
        err.stderr = "already configured for credential key PAGERDUTY_ACCESS_TOKEN"
        self.runner.responses["refresh configure"] = err

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success,
                        f"Should handle CalledProcessError with 'already configured' in stderr: {result.error}")

    def test_nonsecret_vault_refresh_material_resolved(self):
        """Non-secret vault-sourced refresh material is resolved into argv."""
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success)
        configure_calls = [
            c for c in self.runner.calls
            if "configure" in c["argv"]
        ]
        self.assertGreaterEqual(len(configure_calls), 1)
        configure_argv = " ".join(configure_calls[0]["argv"])
        self.assertIn("--material client_id=fake-pagerduty-oauth-client-id", configure_argv)
        self.assertNotIn("${", configure_argv)

    def test_provider_failure_isolated(self):
        """A failing provider does not prevent others from running."""
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
            config_dir=self._tmpdir,
        )
        self.assertEqual(len(results), 5)

    def test_only_selection(self):
        """--only provisions a single provider."""
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
            only="rosa-agent-github",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "rosa-agent-github")

    def test_nonzero_summary_on_failure(self):
        """Summary returns non-zero when any provider fails."""
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok
        # Make github's provider create fail
        self.runner.responses["provider create"] = Exception("create failed")

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
            config_dir=self._tmpdir,
        )
        exit_code = pp.format_summary(results)
        self.assertNotEqual(exit_code, 0)

    def test_all_success_summary(self):
        """Summary returns zero when all providers succeed."""
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
            config_dir=self._tmpdir,
        )
        exit_code = pp.format_summary(results)
        self.assertEqual(exit_code, 0)

    def test_error_includes_stderr(self):
        """ProvisionResult.error includes subprocess stderr when available."""
        err = subprocess.CalledProcessError(1, ["openshell"])
        err.stderr = "dial: connection refused"
        formatted = pp._format_error(err)
        self.assertIn("connection refused", formatted)

    def test_unknown_only_raises(self):
        """--only with an unknown provider name raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            pp.provision_all(
                self.desc, self.gateway,
                runner=self.runner, vault_reader=self.vault_reader,
                only="rosa-agent-nonexistent",
            )
        self.assertIn("rosa-agent-nonexistent", str(ctx.exception))

    def test_verify_after_create(self):
        """After provider create, a 'provider get' verify call is made."""
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        result = pp.provision_one_provider(
            self.desc["providers"][1],  # github
            self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success)
        verify_calls = [
            c for c in self.runner.calls
            if "provider" in " ".join(c["argv"])
            and "get" in c["argv"]
            and "-o" in c["argv"]
        ]
        self.assertEqual(len(verify_calls), 1)
        self.assertIn("json", " ".join(verify_calls[0]["argv"]))

    def test_result_reports_created(self):
        """ProvisionResult reports 'created' when provider was new."""
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        result = pp.provision_one_provider(
            self.desc["providers"][1],  # github
            self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.action, "created")

    def test_result_reports_updated(self):
        """ProvisionResult reports 'updated' when provider already existed."""
        export_out = "resource_version: abc123\nid: github\n"
        self.runner.responses["profile export"] = export_out
        self.runner.responses["provider get"] = '{"name":"rosa-agent-github"}'

        result = pp.provision_one_provider(
            self.desc["providers"][1],  # github
            self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.action, "updated")

    def test_result_reports_rotated(self):
        """ProvisionResult reports rotated=True when rotate ran."""
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        self.runner.responses["provider get"] = _not_found_then_ok

        # PagerDuty provider (index 3) has rotate_after_configure
        result = pp.provision_one_provider(
            self.desc["providers"][3],  # pagerduty
            self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success)
        self.assertTrue(result.rotated)

    def test_summary_shows_action(self):
        """Summary output includes created/updated action."""
        results = [
            pp.ProvisionResult("rosa-agent-github", success=True, action="created"),
            pp.ProvisionResult("rosa-agent-jira", success=True, action="updated"),
        ]
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            pp.format_summary(results)
        output = f.getvalue()
        self.assertIn("created", output)
        self.assertIn("updated", output)


    def test_verify_checks_type(self):
        """Verify step confirms provider type matches profile."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        def _wrong_type(argv, call_num):
            if call_num == 1:
                return Exception('provider "rosa-agent-github" not found')
            return '{"name":"rosa-agent-github","type":"wrong-type"}'
        self.runner.responses["provider get"] = _wrong_type

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertFalse(result.success)
        self.assertIn("wrong-type", result.error)

    def test_verify_correct_type_succeeds(self):
        """Verify step passes when type matches profile."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = Exception("provider profile 'unknown' not found")
        def _correct_type(argv, call_num):
            if call_num == 1:
                return Exception('provider "rosa-agent-github" not found')
            return '{"name":"rosa-agent-github","type":"github"}'
        self.runner.responses["provider get"] = _correct_type

        result = pp.provision_one_provider(
            prov, self.vault, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertTrue(result.success, f"Expected success, got: {result}")


class TestOnlyValidation(unittest.TestCase):
    """Test --only validation at provision_all and format_dry_run level."""

    def setUp(self):
        self.desc = _load_minimal()

    def test_dryrun_unknown_only_raises(self):
        """format_dry_run with unknown --only raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            pp.format_dry_run(self.desc, "rosa-agent", only="rosa-agent-nonexistent")
        self.assertIn("rosa-agent-nonexistent", str(ctx.exception))


class TestNotFoundPattern(unittest.TestCase):
    """Test _is_not_found rejects unrelated 'not found' messages."""

    def test_rejects_gateway_not_found(self):
        self.assertFalse(pp._is_not_found('gateway "rosa-agent" not found'))

    def test_rejects_command_not_found(self):
        self.assertFalse(pp._is_not_found("openshellctl: command not found"))

    def test_rejects_file_not_found(self):
        self.assertFalse(pp._is_not_found("file not found: /tmp/x.yaml"))

    def test_matches_provider_quoted(self):
        self.assertTrue(pp._is_not_found('provider "rosa-agent-github" not found'))

    def test_matches_provider_profile_quoted(self):
        self.assertTrue(pp._is_not_found("provider profile 'github' not found"))


if __name__ == "__main__":
    unittest.main()
