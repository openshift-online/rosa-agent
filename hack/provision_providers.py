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
    import yaml as _yaml

    def _load_yaml(path):
        with open(path) as f:
            return _yaml.safe_load(f)

except ImportError:
    _yaml = None

    def _load_yaml(path):
        """Minimal YAML parser for the providers.yaml subset.

        Handles: mappings, sequences (- items), quoted and unquoted scalars,
        booleans, nulls, and comments. Does NOT handle: anchors, aliases,
        flow collections, multi-line scalars, or complex keys.
        """
        with open(path) as f:
            lines = f.readlines()
        return _parse_yaml_lines(lines)


def _parse_yaml_lines(lines):
    """Parse a list of YAML lines into a Python object."""
    root, _ = _parse_block(lines, 0, -1)
    return root


def _parse_block(lines, start, parent_indent):
    """Parse a YAML block starting at line `start` with indent > parent_indent."""
    result = None
    i = start
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip("\n").rstrip("\r")

        # Skip blank lines and comments
        if not stripped or stripped.lstrip().startswith("#"):
            i += 1
            continue

        indent = len(stripped) - len(stripped.lstrip())

        # Back to parent level or above — stop
        if indent <= parent_indent:
            break

        content = stripped.lstrip()

        # Sequence item
        if content.startswith("- "):
            if result is None:
                result = []
            item_text = content[2:]
            # Is the item a mapping key?
            if ":" in item_text and not item_text.startswith('"'):
                key_match = re.match(r"([A-Za-z_][A-Za-z0-9_./-]*)\s*:\s*(.*)", item_text)
                if key_match:
                    item = {}
                    key = key_match.group(1)
                    val_text = key_match.group(2).strip()
                    # First key on the same line as the dash
                    item_indent = indent + 2
                    if val_text:
                        item[key] = _parse_scalar(val_text)
                        i += 1
                    else:
                        # Value is a nested block
                        sub, i = _parse_block(lines, i + 1, indent + 2 + len(key))
                        if sub is None:
                            sub, i = _parse_block(lines, i, item_indent)
                        item[key] = sub
                    # Read remaining keys at item_indent
                    while i < len(lines):
                        raw2 = lines[i]
                        s2 = raw2.rstrip("\n").rstrip("\r")
                        if not s2 or s2.lstrip().startswith("#"):
                            i += 1
                            continue
                        ind2 = len(s2) - len(s2.lstrip())
                        if ind2 < item_indent:
                            break
                        if ind2 == item_indent:
                            c2 = s2.lstrip()
                            km2 = re.match(r"([A-Za-z_][A-Za-z0-9_./-]*)\s*:\s*(.*)", c2)
                            if km2:
                                k2 = km2.group(1)
                                v2 = km2.group(2).strip()
                                if v2:
                                    item[k2] = _parse_scalar(v2)
                                    i += 1
                                else:
                                    sub2, i = _parse_block(lines, i + 1, ind2)
                                    item[k2] = sub2
                            else:
                                break
                        elif ind2 > item_indent:
                            # Sub-block of the last key
                            i += 1
                        else:
                            break
                    result.append(item)
                    continue
            # Simple scalar list item
            result.append(_parse_scalar(item_text.strip()))
            i += 1
            continue

        # Mapping key
        key_match = re.match(r"([A-Za-z_][A-Za-z0-9_./-]*)\s*:\s*(.*)", content)
        if key_match:
            if result is None:
                result = {}
            key = key_match.group(1)
            val_text = key_match.group(2).strip()
            if val_text:
                result[key] = _parse_scalar(val_text)
                i += 1
            else:
                sub, i = _parse_block(lines, i + 1, indent)
                result[key] = sub
            continue

        i += 1

    return result, i


def _parse_scalar(text):
    """Parse a YAML scalar value."""
    if not text:
        return None
    # Remove trailing comments (not inside quotes)
    if not text.startswith('"') and not text.startswith("'"):
        comment_pos = text.find(" #")
        if comment_pos >= 0:
            text = text[:comment_pos].rstrip()

    # Quoted strings
    if (text.startswith('"') and text.endswith('"')) or \
       (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    # Booleans
    if text.lower() in ("true", "yes", "on"):
        return True
    if text.lower() in ("false", "no", "off"):
        return False
    # Empty flow collections
    if text == "[]":
        return []
    if text == "{}":
        return {}
    # Null
    if text.lower() in ("null", "~", ""):
        return None
    # Integers
    try:
        return int(text)
    except ValueError:
        pass
    # Floats
    try:
        return float(text)
    except ValueError:
        pass
    return text


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
        elif name in seen_names:
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
            "profile", "update", profile_id, "--file",
            "{tmp_file_with_resource_version}",
        ]
        if global_flag:
            update_cmd.append("--global")
        update_cmd.append("{resource_version}")
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
        elif "literal" in cred:
            cmd.extend(["--credential", f"{key}={cred['literal']}"])

    if not provider_exists:
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
