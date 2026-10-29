"""Tests for hack/check_hypershell.py.

These cover the pure checks only. Rendering is left out deliberately: it
shells out to kustomize, and `make check-hypershell` already exercises
that path against the real tree on every lint run.

Each case here corresponds to a way the live API rejects, or silently
accepts and then ignores, a manifest -- the point of the validator is to
turn those into offline errors.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from check_hypershell import (
    Findings,
    _policy_yaml_to_json,
    check_gateway,
    check_gateway_name,
    check_json_string_fields,
    check_resource,
    check_sandbox_template_policy_drift,
    check_spec_fields,
)


def gateway(**spec):
    """A minimal valid Gateway, overridable per test."""
    base = {
        "placement": {"selector": ["provider/ibm", "network/public"]},
        "route": '{"enabled": true}',
    }
    base.update(spec)
    return {
        "apiVersion": "hypershell.redhat.io/v1",
        "kind": "Gateway",
        "metadata": {"name": "rosa-agent"},
        "spec": base,
    }


class TestCheckResource(unittest.TestCase):
    def test_accepts_a_valid_gateway(self):
        findings = Findings()
        check_resource("root", gateway(), 0, findings)
        self.assertEqual(findings.errors, [])

    def test_rejects_missing_kind(self):
        findings = Findings()
        check_resource("root", {"metadata": {"name": "x"}}, 0, findings)
        self.assertEqual(len(findings.errors), 1)
        self.assertIn("no kind", findings.errors[0])

    def test_rejects_missing_name(self):
        # A nameless importable declaration is a hard import error: the
        # reconciler cannot own or prune it.
        doc = gateway()
        doc["metadata"] = {}
        findings = Findings()
        check_resource("root", doc, 0, findings)
        self.assertTrue(any("metadata.name" in e for e in findings.errors))

    def test_rejects_missing_api_version(self):
        doc = gateway()
        del doc["apiVersion"]
        findings = Findings()
        check_resource("root", doc, 0, findings)
        self.assertTrue(any("apiVersion" in e for e in findings.errors))

    def test_rejects_metadata_description(self):
        # hsctl forwards metadata.description into the create body and the
        # API decodes with DisallowUnknownFields, so this 400s the apply.
        doc = gateway()
        doc["metadata"]["description"] = "a description"
        findings = Findings()
        check_resource("root", doc, 0, findings)
        self.assertTrue(any("description" in e for e in findings.errors))

    def test_rejects_non_importable_kind(self):
        # RoleBinding renders fine and is then silently dropped by the import.
        findings = Findings()
        check_resource(
            "root",
            {"apiVersion": "v1", "kind": "RoleBinding", "metadata": {"name": "rb"}},
            0,
            findings,
        )
        self.assertTrue(any("NOT importable" in e for e in findings.errors))

    def test_rejects_unknown_kind(self):
        findings = Findings()
        check_resource(
            "root",
            {"apiVersion": "v1", "kind": "Wombat", "metadata": {"name": "w"}},
            0,
            findings,
        )
        self.assertTrue(any("not a known HyperShell kind" in e for e in findings.errors))


class TestCheckSpecFields(unittest.TestCase):
    def test_accepts_declared_fields(self):
        findings = Findings()
        check_spec_fields("root", "Gateway", gateway(tls_mode="edge"), findings)
        self.assertEqual(findings.errors, [])

    def test_rejects_unknown_spec_key(self):
        # The API rejects any unrecognised field outright, so a typo fails
        # the whole apply rather than being ignored.
        findings = Findings()
        check_spec_fields("root", "Gateway", gateway(tls_modes="edge"), findings)
        self.assertEqual(len(findings.errors), 1)
        self.assertIn("tls_modes", findings.errors[0])

    def test_reports_every_unknown_key(self):
        findings = Findings()
        check_spec_fields("root", "Gateway", gateway(foo=1, bar=2), findings)
        self.assertEqual(len(findings.errors), 2)

    def test_ignores_kinds_without_a_declared_field_list(self):
        # Kinds not yet taken on are skipped rather than guessed at.
        findings = Findings()
        check_spec_fields("root", "AgentRuntime", {"spec": {"anything": 1}}, findings)
        self.assertEqual(findings.errors, [])

    def test_rejects_non_mapping_spec(self):
        findings = Findings()
        check_spec_fields("root", "Gateway", {"spec": ["not", "a", "map"]}, findings)
        self.assertTrue(any("must be a mapping" in e for e in findings.errors))


class TestCheckGateway(unittest.TestCase):
    def test_accepts_capability_selector(self):
        findings = Findings()
        check_gateway("root", gateway(), findings)
        self.assertEqual(findings.errors, [])

    def test_accepts_local_kind_mode(self):
        findings = Findings()
        check_gateway("root", gateway(placement={"mode": "local-kind"}), findings)
        self.assertEqual(findings.errors, [])

    def test_rejects_cluster_id(self):
        # Pinning defeats a portable tree; placement is the point (MCS-223 AC3).
        findings = Findings()
        check_gateway("root", gateway(cluster_id="01JABC"), findings)
        self.assertTrue(any("cluster_id" in e for e in findings.errors))

    def test_rejects_missing_placement(self):
        doc = gateway()
        del doc["spec"]["placement"]
        findings = Findings()
        check_gateway("root", doc, findings)
        self.assertTrue(any("no placement" in e for e in findings.errors))

    def test_rejects_empty_placement_block(self):
        # A placement block declaring neither selector nor mode makes the
        # import writer error.
        findings = Findings()
        check_gateway("root", gateway(placement={}), findings)
        self.assertTrue(any("neither" in e for e in findings.errors))

    def test_rejects_non_mapping_placement(self):
        findings = Findings()
        check_gateway("root", gateway(placement=["provider/ibm"]), findings)
        self.assertTrue(any("must be a mapping" in e for e in findings.errors))

    def test_rejects_non_string_selector_entries(self):
        findings = Findings()
        check_gateway("root", gateway(placement={"selector": [1, 2]}), findings)
        self.assertTrue(any("list of strings" in e for e in findings.errors))


class TestRouteRequirement(unittest.TestCase):
    """A gateway with no route provisions, reports Running/Healthy, and is
    then unreachable. Nothing in the platform surfaces that as an error, so
    this check is the only thing standing between a clean apply and a gateway
    nobody can connect to."""

    def test_rejects_a_gateway_with_no_route(self):
        doc = gateway()
        del doc["spec"]["route"]
        findings = Findings()
        check_gateway("root", doc, findings)
        self.assertTrue(any("no route" in e for e in findings.errors))

    def test_accepts_an_enabled_route(self):
        findings = Findings()
        check_gateway("root", gateway(route='{"enabled": true}'), findings)
        self.assertEqual(findings.errors, [])

    def test_accepts_an_empty_route_object(self):
        # A non-empty route object defaults to enabled server-side.
        findings = Findings()
        check_gateway("root", gateway(route="{}"), findings)
        self.assertEqual(findings.errors, [])


class TestJSONStringFields(unittest.TestCase):
    """route, oidc and credential_driver are JSON-encoded strings. A YAML
    mapping in those positions renders fine and is then silently dropped,
    because the import writer reads them with specString."""

    def test_accepts_a_json_string(self):
        findings = Findings()
        check_json_string_fields("root", "Gateway", gateway(), findings)
        self.assertEqual(findings.errors, [])

    def test_rejects_a_mapping_where_a_json_string_is_required(self):
        findings = Findings()
        check_json_string_fields(
            "root", "Gateway", gateway(route={"enabled": True}), findings
        )
        self.assertTrue(any("must be a JSON-encoded string" in e for e in findings.errors))

    def test_rejects_malformed_json(self):
        findings = Findings()
        check_json_string_fields(
            "root", "Gateway", gateway(route='{"enabled": tru}'), findings
        )
        self.assertTrue(any("not valid JSON" in e for e in findings.errors))

    def test_checks_oidc_too(self):
        findings = Findings()
        check_json_string_fields("root", "Gateway", gateway(oidc={"issuer": "x"}), findings)
        self.assertTrue(any("spec.oidc" in e for e in findings.errors))

    def test_ignores_absent_fields(self):
        findings = Findings()
        check_json_string_fields("root", "Gateway", gateway(), findings)
        self.assertEqual(findings.errors, [])

    def test_ignores_kinds_with_no_json_string_fields(self):
        findings = Findings()
        check_json_string_fields("root", "AgentRuntime", {"spec": {"cron": "* * * * *"}}, findings)
        self.assertEqual(findings.errors, [])


class TestCapabilityLabels(unittest.TestCase):
    def test_rejects_unknown_namespace(self):
        # The server validates against a closed namespace set.
        findings = Findings()
        check_gateway("root", gateway(placement={"selector": ["cloud/bogus"]}), findings)
        self.assertTrue(any("not recognized" in e for e in findings.errors))

    def test_rejects_label_without_a_namespace(self):
        findings = Findings()
        check_gateway("root", gateway(placement={"selector": ["malformed"]}), findings)
        self.assertTrue(any("namespace/value form" in e for e in findings.errors))

    def test_rejects_empty_value(self):
        findings = Findings()
        check_gateway("root", gateway(placement={"selector": ["provider/"]}), findings)
        self.assertTrue(any("empty value" in e for e in findings.errors))

    def test_rejects_repeated_single_value_namespace(self):
        # provider, region, environment and gateway-version are single-valued.
        findings = Findings()
        check_gateway(
            "root",
            gateway(placement={"selector": ["provider/ibm", "provider/aws"]}),
            findings,
        )
        self.assertTrue(any("only one value" in e for e in findings.errors))

    def test_allows_repeated_multi_value_namespace(self):
        # network and runtime are NOT single-valued; rejecting repeats here
        # would block a legal selector.
        findings = Findings()
        check_gateway(
            "root",
            gateway(placement={"selector": ["network/public", "network/internal"]}),
            findings,
        )
        self.assertEqual(findings.errors, [])

    def test_rejects_an_exact_duplicate_label(self):
        findings = Findings()
        check_gateway(
            "root",
            gateway(placement={"selector": ["network/public", "network/public"]}),
            findings,
        )
        self.assertTrue(any("duplicate" in e for e in findings.errors))

    def test_accepts_a_dotted_gateway_version(self):
        # gateway-version relaxes the DNS-label rule for release tags.
        findings = Findings()
        check_gateway(
            "root",
            gateway(placement={"selector": ["gateway-version/v0.1.5-rhaiv.0"]}),
            findings,
        )
        self.assertEqual(findings.errors, [])

    def test_rejects_dots_in_other_namespaces(self):
        findings = Findings()
        check_gateway(
            "root", gateway(placement={"selector": ["provider/ibm.cloud"]}), findings
        )
        self.assertTrue(any("is invalid for namespace" in e for e in findings.errors))

    def test_rejects_uppercase_values(self):
        findings = Findings()
        check_gateway(
            "root", gateway(placement={"selector": ["provider/IBM"]}), findings
        )
        self.assertTrue(any("is invalid for namespace" in e for e in findings.errors))

    def test_accepts_every_valid_namespace(self):
        selector = [
            "provider/ibm",
            "network/public",
            "region/us-east",
            "runtime/slim",
            "environment/prod",
            "gateway-version/v1",
        ]
        findings = Findings()
        check_gateway("root", gateway(placement={"selector": selector}), findings)
        self.assertEqual(findings.errors, [])


class TestCheckGatewayName(unittest.TestCase):
    def test_accepts_the_expected_name(self):
        findings = Findings()
        check_gateway_name(".hypershell", [gateway()], findings)
        self.assertEqual(findings.errors, [])

    def test_rejects_an_unexpected_name(self):
        # Each instance gets its own name so a wrong-context apply is visible.
        doc = gateway()
        doc["metadata"]["name"] = "rosa-agent-staging"
        findings = Findings()
        check_gateway_name(".hypershell", [doc], findings)
        self.assertTrue(any("expected" in e for e in findings.errors))

    def test_rejects_zero_gateways(self):
        findings = Findings()
        check_gateway_name(".hypershell", [], findings)
        self.assertTrue(any("rendered 0" in e for e in findings.errors))

    def test_rejects_more_than_one_gateway(self):
        findings = Findings()
        check_gateway_name(".hypershell", [gateway(), gateway()], findings)
        self.assertTrue(any("rendered 2" in e for e in findings.errors))

    def test_each_overlay_expects_its_own_name(self):
        doc = gateway()
        doc["metadata"]["name"] = "rosa-agent-integration"
        findings = Findings()
        check_gateway_name(".hypershell/overlays/integration", [doc], findings)
        self.assertEqual(findings.errors, [])


class TestFindings(unittest.TestCase):
    def test_collects_every_problem_rather_than_stopping_at_the_first(self):
        doc = gateway(cluster_id="01JABC", tls_modes="edge")
        doc["metadata"]["description"] = "nope"
        findings = Findings()
        check_resource("root", doc, 0, findings)
        self.assertGreaterEqual(len(findings.errors), 3)

    def test_report_is_zero_when_clean(self):
        # report() prints its own summary; swallow it so a passing test run
        # does not look like the validator was invoked for real.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(Findings().report(), 0)

    def test_report_is_nonzero_when_problems_were_found(self):
        findings = Findings()
        findings.error("root", "something is wrong")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(findings.report(), 1)


def _minimal_policy_src():
    """Minimal policies/default.yaml-shaped dict for converter and drift tests.

    Shape matches the real file: snake_case keys, network_policies is a map,
    each rule has an explicit name field, and version is present.
    """
    return {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_only": [],
            "read_write": ["/tmp"],
        },
        "network_policies": {
            "test_rule": {
                "name": "test-rule",
                "endpoints": [
                    {
                        "host": "example.com",
                        "port": 443,
                        "access": "read-only",
                        "protocol": "rest",
                        "enforcement": "enforce",
                    }
                ],
                "binaries": [{"path": "/usr/bin/curl"}],
            }
        },
    }


def _policy_src_to_yaml_bytes(src: dict) -> bytes:
    import yaml  # validate.py already requires pyyaml
    return yaml.dump(src).encode()


class TestPolicyYamlToJson(unittest.TestCase):
    def test_converts_minimal_source(self):
        # The converter is a direct YAML→JSON serialisation: snake_case,
        # network_policies map, version preserved.
        out = json.loads(_policy_yaml_to_json(_minimal_policy_src()))
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["filesystem_policy"]["include_workdir"], True)
        self.assertEqual(out["filesystem_policy"]["read_write"], ["/tmp"])
        rule = out["network_policies"]["test_rule"]
        self.assertEqual(rule["name"], "test-rule")
        self.assertEqual(rule["endpoints"][0]["host"], "example.com")
        self.assertEqual(rule["endpoints"][0]["port"], 443)
        self.assertEqual(rule["binaries"][0]["path"], "/usr/bin/curl")

    def test_preserves_version_key(self):
        # policies/default.yaml carries a top-level version: 1 marker.
        # The converter must accept it and preserve it in the JSON output
        # (openshellctl/pkg/policyyaml/types.go requires version).
        out = json.loads(_policy_yaml_to_json(_minimal_policy_src()))
        self.assertEqual(out["version"], 1)


class TestCheckSandboxTemplateSpecFields(unittest.TestCase):
    def _template(self, **spec):
        base = {
            "image": "quay.io/example/img:latest",
            "name_prefix": "test-",
            "policy": "{}",
        }
        base.update(spec)
        return {
            "apiVersion": "hypershell.redhat.io/v1",
            "kind": "SandboxTemplate",
            "metadata": {"name": "test"},
            "spec": base,
        }

    def test_accepts_all_valid_spec_fields(self):
        # KIND_SPEC_FIELDS has no SandboxTemplate entry yet, so no spec field
        # is rejected -- check_spec_fields is a no-op for this kind.
        findings = Findings()
        check_spec_fields("root", "SandboxTemplate", self._template(), findings)
        self.assertEqual(findings.errors, [])

    def test_policy_must_be_json_string_not_mapping(self):
        findings = Findings()
        check_json_string_fields(
            "root", "SandboxTemplate", self._template(policy={"network": []}), findings
        )
        self.assertTrue(any("must be a JSON-encoded string" in e for e in findings.errors))

    def test_policy_json_must_be_valid(self):
        findings = Findings()
        check_json_string_fields(
            "root", "SandboxTemplate", self._template(policy="{bad json}"), findings
        )
        self.assertTrue(any("not valid JSON" in e for e in findings.errors))

    def test_accepts_valid_policy_json_string(self):
        findings = Findings()
        check_json_string_fields("root", "SandboxTemplate", self._template(), findings)
        self.assertEqual(findings.errors, [])

    def test_check_resource_accepts_full_template(self):
        findings = Findings()
        check_resource("root", self._template(), 0, findings)
        self.assertEqual(findings.errors, [])


class TestCheckSandboxTemplatePolicyDrift(unittest.TestCase):
    def _write_yaml(self, d: dict, path: Path) -> None:
        import yaml
        path.write_text(yaml.dump(d))

    def _write_template(self, policy_json: str, path: Path) -> None:
        import yaml
        doc = {
            "apiVersion": "hypershell.redhat.io/v1",
            "kind": "SandboxTemplate",
            "metadata": {"name": "rosa-agent"},
            "spec": {
                "name_prefix": "rosa-agent-",
                "image": "quay.io/example/img:latest",
                "policy": policy_json,
            },
        }
        path.write_text(yaml.dump(doc))

    def test_passes_when_policy_matches(self):
        src = _minimal_policy_src()
        expected_json = _policy_yaml_to_json(src)
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "sandbox-template.yaml"
            pp = Path(d) / "default.yaml"
            self._write_template(expected_json, tp)
            self._write_yaml(src, pp)
            findings = Findings()
            check_sandbox_template_policy_drift(findings, _template_path=tp, _policy_path=pp)
        self.assertEqual(findings.errors, [])

    def test_errors_when_policy_has_drifted(self):
        src = _minimal_policy_src()
        expected_json = _policy_yaml_to_json(src)
        # Modify the embedded JSON to simulate drift.
        embedded = json.loads(expected_json)
        embedded.setdefault("network", [{}])
        embedded["network"].append({"name": "extra-block"})
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "sandbox-template.yaml"
            pp = Path(d) / "default.yaml"
            self._write_template(json.dumps(embedded), tp)
            self._write_yaml(src, pp)
            findings = Findings()
            check_sandbox_template_policy_drift(findings, _template_path=tp, _policy_path=pp)
        self.assertTrue(any("drifted" in e for e in findings.errors))

    def test_errors_when_template_file_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "nonexistent-template.yaml"
            pp = Path(d) / "default.yaml"
            pp.write_text("")
            findings = Findings()
            check_sandbox_template_policy_drift(findings, _template_path=tp, _policy_path=pp)
        self.assertTrue(any("not found" in e for e in findings.errors))

    def test_errors_when_policy_file_is_missing(self):
        src = _minimal_policy_src()
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "sandbox-template.yaml"
            pp = Path(d) / "nonexistent.yaml"
            self._write_template("{}", tp)
            findings = Findings()
            check_sandbox_template_policy_drift(findings, _template_path=tp, _policy_path=pp)
        self.assertTrue(any("not found" in e for e in findings.errors))

    def test_errors_when_policy_field_is_not_a_string(self):
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "sandbox-template.yaml"
            pp = Path(d) / "default.yaml"
            import yaml
            tp.write_text(yaml.dump({
                "apiVersion": "hypershell.redhat.io/v1",
                "kind": "SandboxTemplate",
                "metadata": {"name": "test"},
                "spec": {"policy": {"network": []}},  # a mapping, not a string
            }))
            self._write_yaml(_minimal_policy_src(), pp)
            findings = Findings()
            check_sandbox_template_policy_drift(findings, _template_path=tp, _policy_path=pp)
        self.assertTrue(any("not a string" in e for e in findings.errors))

    def test_errors_when_embedded_json_is_invalid(self):
        # If the generator writes corrupted JSON (e.g. a backslash-mangled
        # replacement), the drift check must report a finding rather than
        # crashing with a raw JSONDecodeError traceback.
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "sandbox-template.yaml"
            pp = Path(d) / "default.yaml"
            self._write_template("{not valid json}", tp)
            self._write_yaml(_minimal_policy_src(), pp)
            findings = Findings()
            check_sandbox_template_policy_drift(findings, _template_path=tp, _policy_path=pp)
        self.assertTrue(any("not valid JSON" in e for e in findings.errors))

    def test_errors_when_source_yaml_has_unknown_key(self):
        # If policies/default.yaml gains a section the converter doesn't
        # handle, the drift check must fail rather than silently passing.
        src = dict(_minimal_policy_src())
        src["process"] = {"allowed": ["/usr/bin/curl"]}
        expected_json = json.dumps({"network": []})
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "sandbox-template.yaml"
            pp = Path(d) / "default.yaml"
            self._write_template(expected_json, tp)
            self._write_yaml(src, pp)
            findings = Findings()
            check_sandbox_template_policy_drift(findings, _template_path=tp, _policy_path=pp)
        self.assertTrue(any("failed to convert" in e or "drifted" in e for e in findings.errors))

    def test_real_repo_files_pass_drift_check(self):
        # Regression guard: run the drift check against the actual repo files.
        # This catches issues like an unknown top-level key in policies/default.yaml
        # (e.g. `version: 1`) that unit fixtures miss because they use minimal dicts.
        # If this test fails, the drift guard would also fail `make check-hypershell`.
        repo_root = Path(__file__).resolve().parents[2]
        template_path = repo_root / ".hypershell/base/platform/sandbox-template.yaml"
        policy_path = repo_root / "policies/default.yaml"
        if not template_path.exists() or not policy_path.exists():
            self.skipTest("real repo files not present")
        findings = Findings()
        check_sandbox_template_policy_drift(
            findings, _template_path=template_path, _policy_path=policy_path
        )
        self.assertEqual(
            findings.errors,
            [],
            "Drift guard failed on real repo files — either policies/default.yaml "
            "has an unhandled key or the embedded policy JSON has drifted.",
        )


class TestGenerateHypershell(unittest.TestCase):
    """Tests for hack/generate_hypershell.py.

    The generator writes policy JSON into a single-quoted YAML scalar.
    Two characters require special handling:
      - Apostrophe: must be doubled to '' or the YAML becomes unparseable.
      - Backslash: re.subn interprets \\n, \\1, etc. in a string replacement;
        the generator must use a callable so backslashes pass through verbatim.
    """

    def _run_generate(self, policy_src: dict) -> str:
        """Run the generator against a temp policy file and template; return
        the spec.policy value that ends up in the template."""
        import sys, importlib, types
        import yaml

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            policy_path = d / "default.yaml"
            policy_path.write_text(yaml.dump(policy_src))

            # Build a minimal template file with the right spec.policy line.
            template_path = d / "sandbox-template.yaml"
            template_path.write_text(
                "apiVersion: hypershell.redhat.io/v1\n"
                "kind: SandboxTemplate\n"
                "metadata:\n"
                "  name: rosa-agent\n"
                "spec:\n"
                "  name_prefix: rosa-agent-\n"
                "  image: quay.io/example/img:latest\n"
                "  policy: '{}'\n"
            )

            # Import the generator with overridden path constants.
            import importlib.util, types
            spec = importlib.util.spec_from_file_location(
                "generate_hypershell",
                Path(__file__).resolve().parents[1] / "generate_hypershell.py",
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Override after exec_module: executing the module sets the
            # constants to the real repo paths; assignments before exec_module
            # are overwritten by the module's own top-level code.
            mod.TEMPLATE_PATH = template_path
            mod.POLICY_PATH = policy_path

            rc = mod.main()
            self.assertEqual(rc, 0, "generator exited non-zero")

            # Parse via yaml.safe_load — the same path kustomize and the drift
            # check use. A regex + manual un-escape would pass even if the
            # apostrophe fix were absent, because it never exercises YAML
            # parsing (the real failure mode: bare ' breaks yaml.safe_load).
            import yaml
            doc = yaml.safe_load(template_path.read_text())
            return doc["spec"]["policy"]

    def test_round_trips_apostrophe(self):
        # An apostrophe in an endpoint host (or any string value) must survive
        # the single-quoted YAML embedding and be re-parseable as valid JSON.
        src = {
            "version": 1,
            "network_policies": {
                "test": {
                    "name": "it's-a-test",
                    "endpoints": [{"host": "example.com", "port": 443,
                                   "access": "read-only", "protocol": "rest",
                                   "enforcement": "enforce"}],
                }
            },
        }
        policy_json = self._run_generate(src)
        parsed = json.loads(policy_json)
        self.assertEqual(
            parsed["network_policies"]["test"]["name"], "it's-a-test"
        )

    def test_round_trips_backslash(self):
        # A backslash in a JSON string value (e.g. a regex path like \\d+)
        # must not be mangled by re.subn's replacement-string processing.
        src = {
            "version": 1,
            "network_policies": {
                "test": {
                    "name": "back\\slash",
                    "endpoints": [{"host": "example.com", "port": 443,
                                   "access": "read-only", "protocol": "rest",
                                   "enforcement": "enforce"}],
                }
            },
        }
        policy_json = self._run_generate(src)
        parsed = json.loads(policy_json)
        self.assertEqual(
            parsed["network_policies"]["test"]["name"], "back\\slash"
        )


if __name__ == "__main__":
    unittest.main()
