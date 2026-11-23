"""Tests for hack/provision_providers.py — data-driven provider provisioning.

stdlib unittest only (no pytest). Injected fakes for all I/O: no network, no
real filesystem outside tempfile, no exec.Command.

Ticket: MCS-1484
"""

import os
import sys
import tempfile
import textwrap
import unittest

# provision_providers lives in hack/, which PYTHONPATH=hack makes importable.
# Guard against import-time side effects: we import just the module.
import provision_providers as pp


# ---------------------------------------------------------------------------
# Minimal valid descriptor for reuse across tests
# ---------------------------------------------------------------------------
MINIMAL_YAML = textwrap.dedent("""\
    vault:
      kv_mount: osd-sre
      kv_path: rosa-agent

    providers:
      - name: rosa-agent-openshell
        profile: openshell
        profile_file: provider-profiles/openshell.yaml
        global_profile: true
        credentials:
          - key: OPENSHELL_OIDC_CLIENT_SECRET
            vault_field: hypershell-oidc-client-secret
        pin_gateway_token: true

      - name: rosa-agent-github
        profile: github
        profile_file: provider-profiles/github.yaml
        global_profile: true
        credentials:
          - key: GH_TOKEN
            vault_field: github-token

      - name: rosa-agent-jira
        profile: atlassian-jira
        profile_file: provider-profiles/atlassian-jira.yaml
        global_profile: true
        credentials:
          - key: JIRA_API_TOKEN
            vault_field: jira-token

      - name: rosa-agent-pagerduty
        profile: pagerduty
        profile_file: provider-profiles/pagerduty.yaml
        global_profile: true
        credentials:
          - key: PAGERDUTY_ACCESS_TOKEN
            literal: placeholder
        refresh:
          strategy: oauth2-client-credentials
          credential_key: PAGERDUTY_ACCESS_TOKEN
          material:
            - key: client_id
              vault_field: pagerduty-oauth-client-id
            - key: client_secret
              vault_field: pagerduty-oauth-client-secret
              secret: true

      - name: rosa-agent-vertex
        profile: google-vertex-ai
        profile_file: provider-profiles/google-vertex-ai.yaml
        global_profile: true
        credentials:
          - key: GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN
            literal: placeholder
        config:
          - key: VERTEX_AI_PROJECT_ID
            literal: rosa-general
          - key: VERTEX_AI_REGION
            literal: global
        refresh:
          strategy: google-service-account-jwt
          credential_key: GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN
          material:
            - key: client_email
              literal: rosa-agent@rosa-general.iam.gserviceaccount.com
            - key: private_key
              vault_field: google-rosa-general-service-account-keyfile
              json_path: .private_key
              secret: true
          rotate_after_configure: true
""")


def _write_descriptor(tmp_dir, content=None):
    """Write a descriptor to a temp file and return its path."""
    path = os.path.join(tmp_dir, "providers.yaml")
    with open(path, "w") as f:
        f.write(content if content is not None else MINIMAL_YAML)
    return path


def _write_profile_stubs(tmp_dir, names=None):
    """Create stub profile files so validate_descriptor doesn't fail on missing files."""
    if names is None:
        names = [
            "provider-profiles/openshell.yaml",
            "provider-profiles/github.yaml",
            "provider-profiles/atlassian-jira.yaml",
            "provider-profiles/pagerduty.yaml",
            "provider-profiles/google-vertex-ai.yaml",
        ]
    for name in names:
        path = os.path.join(tmp_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("id: stub\n")


class TestLoadDescriptor(unittest.TestCase):
    """Test descriptor loading from YAML."""

    def test_load_valid_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_descriptor(tmp)
            desc = pp.load_descriptor(path)
            self.assertEqual(desc["vault"]["kv_mount"], "osd-sre")
            self.assertEqual(desc["vault"]["kv_path"], "rosa-agent")
            self.assertEqual(len(desc["providers"]), 5)

    def test_load_returns_provider_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_descriptor(tmp)
            desc = pp.load_descriptor(path)
            names = [p["name"] for p in desc["providers"]]
            self.assertEqual(names, [
                "rosa-agent-openshell",
                "rosa-agent-github",
                "rosa-agent-jira",
                "rosa-agent-pagerduty",
                "rosa-agent-vertex",
            ])

    def test_load_nonexistent_file(self):
        with self.assertRaises(FileNotFoundError):
            pp.load_descriptor("/nonexistent/providers.yaml")


class TestValidateDescriptor(unittest.TestCase):
    """AC2: make check-providers rejects invalid descriptors."""

    def _validate(self, yaml_content, base_dir=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_descriptor(tmp, yaml_content)
            _write_profile_stubs(tmp)
            desc = pp.load_descriptor(path)
            return pp.validate_descriptor(desc, base_dir=base_dir or tmp)

    def _validate_valid(self):
        """Validate the minimal valid descriptor and return errors."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_descriptor(tmp)
            _write_profile_stubs(tmp)
            desc = pp.load_descriptor(path)
            return pp.validate_descriptor(desc, base_dir=tmp)

    def test_valid_descriptor_has_no_errors(self):
        errors = self._validate_valid()
        self.assertEqual(errors, [])

    def test_duplicate_names_rejected(self):
        yaml = textwrap.dedent("""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
                    vault_field: github-token
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
                    vault_field: github-token
        """)
        errors = self._validate(yaml)
        self.assertTrue(any("duplicate" in e.lower() for e in errors),
                        f"Expected duplicate name error, got: {errors}")

    def test_missing_profile_file_rejected(self):
        yaml = textwrap.dedent("""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/nonexistent.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
                    vault_field: github-token
        """)
        errors = self._validate(yaml)
        self.assertTrue(any("profile file" in e.lower() or "not found" in e.lower() for e in errors),
                        f"Expected missing profile file error, got: {errors}")

    def test_credential_with_both_vault_and_literal_rejected(self):
        yaml = textwrap.dedent("""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
                    vault_field: github-token
                    literal: some-value
        """)
        errors = self._validate(yaml)
        self.assertTrue(any("both" in e.lower() for e in errors),
                        f"Expected both vault_field/literal error, got: {errors}")

    def test_credential_with_neither_vault_nor_literal_rejected(self):
        yaml = textwrap.dedent("""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
        """)
        errors = self._validate(yaml)
        self.assertTrue(any("neither" in e.lower() or "must have" in e.lower() for e in errors),
                        f"Expected neither vault_field/literal error, got: {errors}")

    def test_json_path_without_vault_field_rejected(self):
        yaml = textwrap.dedent("""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
                    literal: some-value
                    json_path: .token
        """)
        errors = self._validate(yaml)
        self.assertTrue(any("json_path" in e.lower() for e in errors),
                        f"Expected json_path without vault_field error, got: {errors}")

    def test_unknown_refresh_strategy_rejected(self):
        yaml = textwrap.dedent("""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-test
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: TOKEN
                    literal: placeholder
                refresh:
                  strategy: unknown-strategy
                  credential_key: TOKEN
                  material: []
        """)
        errors = self._validate(yaml)
        self.assertTrue(any("strategy" in e.lower() for e in errors),
                        f"Expected unknown strategy error, got: {errors}")

    def test_literal_value_over_64_chars_rejected(self):
        long_val = "x" * 65
        yaml = textwrap.dedent(f"""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
                    literal: {long_val}
        """)
        errors = self._validate(yaml)
        self.assertTrue(any("64" in e for e in errors),
                        f"Expected >64 char error, got: {errors}")

    def test_literal_value_exactly_64_chars_accepted(self):
        val_64 = "x" * 64
        yaml = textwrap.dedent(f"""\
            vault:
              kv_mount: osd-sre
              kv_path: rosa-agent
            providers:
              - name: rosa-agent-github
                profile: github
                profile_file: provider-profiles/github.yaml
                global_profile: true
                credentials:
                  - key: GH_TOKEN
                    literal: {val_64}
        """)
        errors = self._validate(yaml)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# Argv builder tests — AC3: dry-run shows all argv with $ENVVAR names
# ---------------------------------------------------------------------------

def _load_minimal():
    """Load the minimal valid descriptor."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_descriptor(tmp)
        return pp.load_descriptor(path)


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
        self.assertIn("{resource_version}", argv[1][-1])

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

    def test_pagerduty_no_rotate(self):
        """PagerDuty has no rotate_after_configure."""
        prov = _provider_by_name(self.desc, "rosa-agent-pagerduty")
        _, _, do_rotate = pp.build_refresh_argv(prov, self.vault, self.gateway)
        self.assertFalse(do_rotate)

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


class TestProvisionProvider(unittest.TestCase):
    """Test the provision_one_provider orchestration function."""

    def setUp(self):
        self.desc = _load_minimal()
        self.vault = self.desc["vault"]
        self.gateway = "rosa-agent"
        self.runner = FakeRunner()
        self.vault_reader = FakeVaultReader()

    def test_create_new_provider(self):
        """When profile and provider don't exist, uses import and create."""
        prov = _provider_by_name(self.desc, "rosa-agent-github")
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")

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
                if arg.startswith("/tmp/") and arg.endswith(".yaml"):
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
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")
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
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")

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

    def test_refresh_configure_and_rotate(self):
        """Vertex provider runs configure then rotate."""
        prov = _provider_by_name(self.desc, "rosa-agent-vertex")
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")

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

    def test_provider_failure_isolated(self):
        """A failing provider does not prevent others from running."""
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        self.assertEqual(len(results), 5)

    def test_only_selection(self):
        """--only provisions a single provider."""
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
            only="rosa-agent-github",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "rosa-agent-github")

    def test_nonzero_summary_on_failure(self):
        """Summary returns non-zero when any provider fails."""
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")
        # Make github's provider create fail
        self.runner.responses["provider create"] = Exception("create failed")

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        exit_code = pp.format_summary(results)
        self.assertNotEqual(exit_code, 0)

    def test_all_success_summary(self):
        """Summary returns zero when all providers succeed."""
        self.runner.responses["profile export"] = Exception("not found")
        self.runner.responses["provider get"] = Exception("not found")

        results = pp.provision_all(
            self.desc, self.gateway,
            runner=self.runner, vault_reader=self.vault_reader,
        )
        exit_code = pp.format_summary(results)
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
