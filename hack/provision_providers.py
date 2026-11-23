#!/usr/bin/env python3
"""Data-driven provider provisioning for rosa-agent gateways.

Reads provider-profiles/providers.yaml, validates it, builds openshell/
openshellctl argv sequences, and executes them idempotently. Secrets are
passed via environment variables and never appear in argv or log output.

Ticket: MCS-1484
"""

import json
import os
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path):
    if yaml is None:
        sys.exit(
            "ERROR: PyYAML is required.\n"
            "  Fedora/RHEL: sudo dnf install -y python3-pyyaml\n"
            "  venv:        python3 -m pip install pyyaml"
        )
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Descriptor loading and validation
# ---------------------------------------------------------------------------

KNOWN_REFRESH_STRATEGIES = frozenset([
    "oauth2-client-credentials",
    "google-service-account-jwt",
])


def load_descriptor(path):
    """Load and return the providers.yaml descriptor as a dict."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Descriptor not found: {path}")
    return _load_yaml(path)


def validate_descriptor(desc, base_dir="."):
    """Validate a loaded descriptor dict. Returns a list of error strings."""
    errors = []

    if not isinstance(desc, dict):
        return ["Descriptor must be a YAML mapping"]

    vault = desc.get("vault")
    if not isinstance(vault, dict):
        errors.append("Missing or invalid 'vault' section")
    else:
        if not vault.get("kv_mount"):
            errors.append("vault.kv_mount is required")
        if not vault.get("kv_path"):
            errors.append("vault.kv_path is required")

    providers = desc.get("providers")
    if not isinstance(providers, list) or len(providers) == 0:
        errors.append("'providers' must be a non-empty list")
        return errors

    seen_names = set()
    for idx, prov in enumerate(providers):
        prefix = f"providers[{idx}] ({prov.get('name', '?')})"

        name = prov.get("name")
        if not name:
            errors.append(f"{prefix}: 'name' is required")
        else:
            if not name.startswith("rosa-agent-"):
                errors.append(
                    f"{prefix}: name must start with 'rosa-agent-', got '{name}'"
                )
            if name in seen_names:
                errors.append(f"{prefix}: duplicate provider name '{name}'")
            else:
                seen_names.add(name)

        if not prov.get("profile"):
            errors.append(f"{prefix}: 'profile' is required")

        profile_file = prov.get("profile_file")
        if not profile_file:
            errors.append(f"{prefix}: 'profile_file' is required")
        else:
            full_path = os.path.join(base_dir, profile_file)
            if not os.path.isfile(full_path):
                errors.append(
                    f"{prefix}: profile file not found: {profile_file}"
                )

        # Validate credentials
        creds = prov.get("credentials", [])
        if not isinstance(creds, list):
            errors.append(f"{prefix}: 'credentials' must be a list")
            creds = []

        for ci, cred in enumerate(creds):
            cprefix = f"{prefix}.credentials[{ci}]"
            if not cred.get("key"):
                errors.append(f"{cprefix}: 'key' is required")

            has_vault = bool(cred.get("vault_field"))
            has_literal = "literal" in cred and cred["literal"] is not None

            if has_vault and has_literal:
                errors.append(
                    f"{cprefix}: credential has both 'vault_field' and "
                    f"'literal'; must have exactly one"
                )
            elif not has_vault and not has_literal:
                errors.append(
                    f"{cprefix}: credential must have either 'vault_field' "
                    f"or 'literal'"
                )

            if cred.get("json_path") and not has_vault:
                errors.append(
                    f"{cprefix}: 'json_path' requires 'vault_field'"
                )

            if has_literal:
                val = str(cred["literal"])
                if len(val) > 64:
                    errors.append(
                        f"{cprefix}: literal value exceeds 64 characters "
                        f"({len(val)})"
                    )

        # Validate config keys
        for ci, cfg in enumerate(prov.get("config", [])):
            cfg_key = cfg.get("key", "")
            if cfg_key and not re.match(r'^[A-Z0-9_]+$', cfg_key):
                errors.append(
                    f"{prefix}.config[{ci}]: key '{cfg_key}' does not match "
                    f"pattern [A-Z0-9_]+"
                )

        # Validate refresh
        refresh = prov.get("refresh")
        if refresh is not None:
            if not isinstance(refresh, dict):
                errors.append(f"{prefix}: 'refresh' must be a mapping")
            else:
                strategy = refresh.get("strategy", "")
                if strategy not in KNOWN_REFRESH_STRATEGIES:
                    errors.append(
                        f"{prefix}: unknown refresh strategy '{strategy}'; "
                        f"known: {', '.join(sorted(KNOWN_REFRESH_STRATEGIES))}"
                    )
                if not refresh.get("credential_key"):
                    errors.append(
                        f"{prefix}: refresh.credential_key is required"
                    )

                for mi, mat in enumerate(refresh.get("material", [])):
                    mprefix = f"{prefix}.refresh.material[{mi}]"
                    if not mat.get("key"):
                        errors.append(f"{mprefix}: 'key' is required")
                    has_vf = bool(mat.get("vault_field"))
                    has_lit = "literal" in mat and mat["literal"] is not None
                    if has_vf and has_lit:
                        errors.append(
                            f"{mprefix}: has both 'vault_field' and 'literal'"
                        )
                    elif not has_vf and not has_lit:
                        errors.append(
                            f"{mprefix}: must have 'vault_field' or 'literal'"
                        )
                    if mat.get("json_path") and not has_vf:
                        errors.append(
                            f"{mprefix}: 'json_path' requires 'vault_field'"
                        )
                    if has_lit:
                        val = str(mat["literal"])
                        if len(val) > 64:
                            errors.append(
                                f"{mprefix}: literal value exceeds 64 characters "
                                f"({len(val)})"
                            )
                    if mat.get("secret") and has_lit:
                        errors.append(
                            f"{mprefix}: secret material must not use 'literal' "
                            f"(use vault_field instead)"
                        )

    return errors


# ---------------------------------------------------------------------------
# Argv builders — pure functions, no I/O
# ---------------------------------------------------------------------------

def build_profile_argv(prov, gateway, profile_exists=False):
    """Build argv sequences for profile lint + import/update.

    Returns a list of argv lists (each is a command to run sequentially).
    When profile_exists is True, the update command includes a
    {resource_version} placeholder that the runner splices in at execution
    time from a fresh profile export.
    """
    profile_file = prov["profile_file"]
    profile_id = prov["profile"]
    global_flag = prov.get("global_profile", False)

    lint_cmd = [
        "openshell", "-g", gateway,
        "profile", "lint", "--file", profile_file,
    ]

    if profile_exists:
        update_cmd = [
            "openshell", "-g", gateway,
            "profile", "update", profile_id,
            "--file", "{tmp_file}",
        ]
        if global_flag:
            update_cmd.append("--global")
        return [lint_cmd, update_cmd]
    else:
        import_cmd = [
            "openshell", "-g", gateway,
            "profile", "import", "--file", profile_file,
        ]
        if global_flag:
            import_cmd.append("--global")
        return [lint_cmd, import_cmd]


def _vault_read_command(vault, field, json_path=None):
    """Build a vault kv get command string for an env_plan entry."""
    cmd = (
        f"vault kv get -mount={vault['kv_mount']} "
        f"-field={field} {vault['kv_path']}"
    )
    if json_path:
        cmd = (
            f"vault kv get -mount={vault['kv_mount']} -format=json "
            f"{vault['kv_path']} | python3 -c "
            f"\"import sys,json; print(json.load(sys.stdin)"
            f"['data']['data']['{field}']{_python_json_path(json_path)})\""
        )
    return cmd


def _python_json_path(jp):
    """Convert a simple json_path like '.private_key' to Python dict access."""
    parts = jp.lstrip(".").split(".")
    return "".join(f"['{p}']" for p in parts if p)


def build_provider_argv(prov, vault, gateway, provider_exists=False):
    """Build argv sequences for provider create/update.

    Returns (argv_list, env_plan) where env_plan maps env var names to
    dicts describing how to populate them at runtime:
      {"command": "vault kv get ...", "json_path": ".key"} for Vault-sourced
      {"source": "token_file"} for gateway token pinning
    """
    name = prov["name"]
    profile_id = prov["profile"]
    global_flag = prov.get("global_profile", False)
    creds = prov.get("credentials", [])
    config = prov.get("config", [])
    env_plan = {}

    if provider_exists:
        cmd = ["openshellctl", "-g", gateway, "provider", "update", name]
    else:
        cmd = [
            "openshellctl", "-g", gateway, "provider", "create",
            "--name", name, "--type", profile_id,
        ]
        if global_flag:
            cmd.append("--global-profile")

    for cred in creds:
        key = cred["key"]
        if cred.get("vault_field"):
            cmd.extend(["--credential", key])
            env_plan[key] = {
                "command": _vault_read_command(vault, cred["vault_field"],
                                              cred.get("json_path")),
            }
            if cred.get("json_path"):
                env_plan[key]["json_path"] = cred["json_path"]
        elif "literal" in cred and not provider_exists:
            cmd.extend(["--credential", f"{key}={cred['literal']}"])

    for cfg in config:
        key = cfg["key"]
        if "literal" in cfg:
            cmd.extend(["--config", f"{key}={cfg['literal']}"])

    if prov.get("pin_gateway_token"):
        env_plan["OPENSHELL_TOKEN"] = {"source": "token_file"}

    return [cmd], env_plan


def build_refresh_argv(prov, vault, gateway):
    """Build argv sequences for refresh configure + optional rotate.

    Returns None if no refresh config, otherwise
    (argv_list, env_plan, do_rotate).
    """
    refresh = prov.get("refresh")
    if not refresh:
        return None

    name = prov["name"]
    cred_key = refresh["credential_key"]
    strategy = refresh["strategy"]
    materials = refresh.get("material", [])
    do_rotate = bool(refresh.get("rotate_after_configure"))
    env_plan = {}

    configure_cmd = [
        "openshell", "-g", gateway,
        "provider", "refresh", "configure", name,
        "--credential-key", cred_key,
        "--strategy", strategy,
    ]

    for mat in materials:
        key = mat["key"]
        is_secret = mat.get("secret", False)

        if is_secret:
            env_var = f"_REFRESH_{key.upper()}"
            configure_cmd.extend(["--secret-material-env", f"{key}={env_var}"])

            if mat.get("vault_field"):
                env_plan[env_var] = {
                    "command": _vault_read_command(
                        vault, mat["vault_field"], mat.get("json_path")
                    ),
                }
                if mat.get("json_path"):
                    env_plan[env_var]["json_path"] = mat["json_path"]
        else:
            if "literal" in mat:
                configure_cmd.extend(["--material", f"{key}={mat['literal']}"])
            elif mat.get("vault_field"):
                env_var = f"_REFRESH_{key.upper()}"
                configure_cmd.extend(["--material", f"{key}=${{{env_var}}}"])
                env_plan[env_var] = {
                    "command": _vault_read_command(vault, mat["vault_field"]),
                }

    argv_list = [configure_cmd]

    if do_rotate:
        rotate_cmd = [
            "openshell", "-g", gateway,
            "provider", "refresh", "rotate", name,
            "--credential-key", cred_key,
        ]
        argv_list.append(rotate_cmd)

    return argv_list, env_plan, do_rotate


def _substitute_env_refs(arg, env):
    """Replace ${VAR} references in an argv string with resolved env values."""
    def _replace(m):
        return env.get(m.group(1), m.group(0))
    return re.sub(r'\$\{([^}]+)\}', _replace, arg)


# ---------------------------------------------------------------------------
# Dry-run formatting
# ---------------------------------------------------------------------------

def format_dry_run(desc, gateway, only=None):
    """Format all argv sequences as a human-readable dry-run plan.

    Shows both the "new" (import/create) and "existing" (update) paths.
    Secrets appear as $ENVVAR_NAME.
    """
    vault = desc["vault"]
    lines = []

    for prov in desc["providers"]:
        if only and prov["name"] != only:
            continue

        lines.append(f"=== {prov['name']} (profile: {prov['profile']}) ===")
        lines.append("")

        for exists_label, exists_flag in [("new", False), ("existing", True)]:
            lines.append(f"  --- profile {exists_label} ---")
            profile_argv = build_profile_argv(prov, gateway, profile_exists=exists_flag)
            for cmd in profile_argv:
                lines.append(f"    {_format_cmd(cmd)}")

            lines.append(f"  --- provider {exists_label} ---")
            prov_argv, env_plan = build_provider_argv(
                prov, vault, gateway, provider_exists=exists_flag
            )
            if env_plan:
                lines.append("    env:")
                for var, plan in sorted(env_plan.items()):
                    if "command" in plan:
                        lines.append(f"      ${var}=$({plan['command']})")
                    elif plan.get("source") == "token_file":
                        lines.append(f"      ${var}=$(read from gateway token file)")
            for cmd in prov_argv:
                lines.append(f"    {_format_cmd(cmd)}")

            lines.append("")

        refresh_result = build_refresh_argv(prov, vault, gateway)
        if refresh_result:
            argv_list, env_plan, do_rotate = refresh_result
            lines.append("  --- refresh ---")
            if env_plan:
                lines.append("    env:")
                for var, plan in sorted(env_plan.items()):
                    if "command" in plan:
                        lines.append(f"      ${var}=$({plan['command']})")
            for cmd in argv_list:
                lines.append(f"    {_format_cmd(cmd)}")
            lines.append("")

        lines.append("")

    return "\n".join(lines)


def _format_cmd(argv):
    """Format an argv list as a shell-like command string."""
    parts = []
    for arg in argv:
        if " " in arg or "'" in arg or '"' in arg or "$" in arg:
            parts.append(f'"{arg}"')
        else:
            parts.append(arg)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Execution runner — the I/O shell
# ---------------------------------------------------------------------------

class ProvisionResult:
    """Result of provisioning one provider."""

    def __init__(self, name, success=True, error=None):
        self.name = name
        self.success = success
        self.error = error

    def __repr__(self):
        status = "OK" if self.success else f"FAILED: {self.error}"
        return f"ProvisionResult({self.name}: {status})"


class DefaultRunner:
    """Executes subprocesses."""

    def run(self, argv, env=None, check=True):
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        return subprocess.run(
            argv, env=merged_env, check=check,
            capture_output=True, text=True,
        )


class DefaultVaultReader:
    """Reads secrets from Vault via subprocess."""

    def read(self, mount, path, field, json_path=None):
        if json_path:
            result = subprocess.run(
                ["vault", "kv", "get", f"-mount={mount}", "-format=json", path],
                capture_output=True, text=True, check=True,
            )
            data = json.loads(result.stdout)
            val = data["data"]["data"][field]
            for part in json_path.lstrip(".").split("."):
                if part:
                    val = val[part]
            return val
        else:
            result = subprocess.run(
                ["vault", "kv", "get", f"-mount={mount}", f"-field={field}", path],
                capture_output=True, text=True, check=True,
            )
            return result.stdout.strip()


_NOT_FOUND_PATTERNS = [
    "profile not found",
    "provider not found",
    "no such profile",
    "no such provider",
    "resource not found",
]


def _check_exists(runner, argv):
    """Run a get/export command; return (exists: bool, output: str)."""
    try:
        result = runner.run(argv, check=False)
        if result.returncode != 0:
            stderr = getattr(result, "stderr", "")
            stdout = getattr(result, "stdout", "")
            combined = f"{stdout} {stderr}".lower()
            if any(p in combined for p in _NOT_FOUND_PATTERNS):
                return False, ""
            raise RuntimeError(
                f"Unexpected error (exit {result.returncode}): "
                f"{stderr or stdout}"
            )
        return True, getattr(result, "stdout", "")
    except Exception as e:
        msg = str(e).lower()
        if any(p in msg for p in _NOT_FOUND_PATTERNS):
            return False, ""
        raise


def _read_gateway_token(gateway, config_dir=None):
    """Read the cached OIDC token for OPENSHELL_TOKEN pinning."""
    if config_dir:
        base = config_dir
    else:
        base = os.environ.get(
            "XDG_CONFIG_HOME",
            os.path.join(os.path.expanduser("~"), ".config"),
        )
    token_path = os.path.join(
        base, "openshell", "gateways", gateway, "oidc_token.json"
    )
    if not os.path.isfile(token_path):
        return None
    with open(token_path) as f:
        data = json.load(f)
    return data.get("access_token")



def _resolve_env_for_credentials(prov, vault, vault_reader):
    """Resolve Vault-sourced credentials into env vars."""
    env = {}
    for cred in prov.get("credentials", []):
        if cred.get("vault_field"):
            val = vault_reader.read(
                vault["kv_mount"], vault["kv_path"],
                cred["vault_field"], cred.get("json_path"),
            )
            env[cred["key"]] = val
    return env


def _resolve_env_for_refresh(refresh, vault, vault_reader):
    """Resolve Vault-sourced refresh material into env vars."""
    env = {}
    if not refresh:
        return env
    for mat in refresh.get("material", []):
        if mat.get("vault_field"):
            env_var = f"_REFRESH_{mat['key'].upper()}"
            val = vault_reader.read(
                vault["kv_mount"], vault["kv_path"],
                mat["vault_field"], mat.get("json_path"),
            )
            env[env_var] = val
    return env


def _extract_resource_version(export_output):
    """Extract resource_version from profile export output."""
    for line in export_output.splitlines():
        if line.startswith("resource_version:"):
            return line.split(":", 1)[1].strip()
    return None


def _make_tmp_profile(profile_path, resource_version):
    """Create a temp file with resource_version appended. Returns its path.

    NamedTemporaryFile creates files with 0600 permissions by default.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        tmp_file = f.name
        if os.path.isfile(profile_path):
            with open(profile_path) as src:
                content = src.read()
                f.write(content)
                if content and not content.endswith("\n"):
                    f.write("\n")
        f.write(f"resource_version: {resource_version}\n")
    return tmp_file


def _format_error(e):
    """Format an exception for ProvisionResult, including stderr if available."""
    stderr = getattr(e, "stderr", None)
    if stderr:
        return f"{e}: {stderr.strip()}"
    return str(e)


def provision_one_provider(prov, vault, gateway, runner=None, vault_reader=None,
                            base_dir=".", config_dir=None):
    """Provision a single provider: profile + provider + optional refresh.

    Returns a ProvisionResult.
    """
    if runner is None:
        runner = DefaultRunner()
    if vault_reader is None:
        vault_reader = DefaultVaultReader()

    name = prov["name"]

    try:
        # 1. Check if profile exists
        profile_exists, export_output = _check_exists(
            runner,
            ["openshell", "-g", gateway, "profile", "export", prov["profile"],
             *(["--global"] if prov.get("global_profile") else [])],
        )

        # 2. Build and execute profile commands
        profile_cmds = build_profile_argv(prov, gateway, profile_exists=profile_exists)
        tmp_file = None
        try:
            if profile_exists:
                rv = _extract_resource_version(export_output)
                if not rv:
                    raise RuntimeError(
                        "Could not extract resource_version from profile export"
                    )
                profile_path = os.path.join(base_dir, prov["profile_file"])
                tmp_file = _make_tmp_profile(profile_path, rv)

            for cmd in profile_cmds:
                resolved = [
                    tmp_file if arg == "{tmp_file}" else arg
                    for arg in cmd
                ]
                runner.run(resolved)
        finally:
            if tmp_file and os.path.exists(tmp_file):
                os.unlink(tmp_file)

        # 3. Check if provider exists
        provider_exists, _ = _check_exists(
            runner,
            ["openshellctl", "-g", gateway, "provider", "get", name],
        )

        # 4. Resolve credentials env
        cred_env = _resolve_env_for_credentials(prov, vault, vault_reader)

        if prov.get("pin_gateway_token"):
            token = _read_gateway_token(gateway, config_dir=config_dir)
            if token:
                cred_env["OPENSHELL_TOKEN"] = token

        # 5. Build and execute provider commands
        prov_argv, _ = build_provider_argv(
            prov, vault, gateway, provider_exists=provider_exists
        )
        for cmd in prov_argv:
            runner.run(cmd, env=cred_env)

        # 6. Refresh configure + rotate
        refresh = prov.get("refresh")
        if refresh:
            refresh_env = _resolve_env_for_refresh(refresh, vault, vault_reader)
            refresh_result = build_refresh_argv(prov, vault, gateway)
            if refresh_result:
                argv_list, _, do_rotate = refresh_result
                for cmd in argv_list:
                    resolved = [
                        _substitute_env_refs(arg, refresh_env) for arg in cmd
                    ]
                    try:
                        runner.run(resolved, env=refresh_env)
                    except Exception as configure_err:
                        if "already configured" in str(configure_err).lower():
                            pass
                        else:
                            raise

        return ProvisionResult(name, success=True)

    except Exception as e:
        return ProvisionResult(name, success=False, error=_format_error(e))


def provision_all(desc, gateway, runner=None, vault_reader=None,
                   base_dir=".", config_dir=None, only=None):
    """Provision all (or one) provider(s). Returns a list of ProvisionResult."""
    results = []
    for prov in desc["providers"]:
        if only and prov["name"] != only:
            continue
        result = provision_one_provider(
            prov, desc["vault"], gateway,
            runner=runner, vault_reader=vault_reader,
            base_dir=base_dir, config_dir=config_dir,
        )
        results.append(result)
    return results


def format_summary(results):
    """Print a summary and return an exit code (0 = all OK, 1 = any failed)."""
    failed = [r for r in results if not r.success]
    succeeded = [r for r in results if r.success]

    for r in succeeded:
        print(f"  OK: {r.name}")
    for r in failed:
        print(f"  FAILED: {r.name}: {r.error}")

    print(f"\n{len(succeeded)}/{len(results)} providers succeeded.")
    if failed:
        print(f"{len(failed)} provider(s) failed:")
        for r in failed:
            print(f"  - {r.name}")
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry point for make targets."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Provision rosa-agent providers on a gateway."
    )
    parser.add_argument(
        "--descriptor", default="provider-profiles/providers.yaml",
        help="Path to providers.yaml descriptor",
    )
    parser.add_argument(
        "--gateway", default="rosa-agent",
        help="Gateway registration name",
    )
    parser.add_argument(
        "--only",
        help="Provision only this provider name",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show planned commands without executing",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Validate the descriptor and exit",
    )
    parser.add_argument(
        "--base-dir", default=".",
        help="Base directory for resolving profile_file paths",
    )

    args = parser.parse_args(argv)

    desc = load_descriptor(args.descriptor)

    errors = validate_descriptor(desc, base_dir=args.base_dir)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    if args.check:
        print("providers.yaml: OK")
        return 0

    if args.dry_run:
        print(format_dry_run(desc, args.gateway, only=args.only))
        return 0

    results = provision_all(
        desc, args.gateway,
        base_dir=args.base_dir,
        only=args.only,
    )
    return format_summary(results)


if __name__ == "__main__":
    sys.exit(main())
