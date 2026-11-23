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


if __name__ == "__main__":
    unittest.main()
