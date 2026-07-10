"""MCP server config logic — importable without the mcp package (OHM-yzyk.1.2).

This module contains the config loading, tool filtering, and header
construction logic for the OHM MCP server. It is kept separate from
``server.py`` so it can be tested without the ``mcp`` package installed.

OHM-yzyk.1.1: token_type field controls whether X-Tenant-ID is sent.
Customer API keys are already tenant-scoped (the key selects the tenant),
so sending X-Tenant-ID is unnecessary and ignored after OHM-tss4.19.
Admin agent tokens need X-Tenant-ID to select the tenant.

OHM-yzyk.3: Agent profiles — a single sidecar can switch between multiple
OHM instances/tenants at runtime via ``ohm_select_profile``. The config
may contain either the legacy flat keys (one implicit default profile) or
a ``profiles`` array with per-profile credentials and policy.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Mutable config — defaults from env vars, overridable by --config file
config: dict[str, Any] = {
    "ohm_url": os.environ.get("OHM_URL", "http://127.0.0.1:8710"),
    "token": os.environ.get("OHM_TOKEN", ""),
    "agent_id": os.environ.get("OHM_AGENT", "mcp"),
    "tenant_id": os.environ.get("OHM_TENANT_ID", ""),
    "token_type": "agent",  # "agent" (sends X-Tenant-ID) or "customer" (key is tenant-scoped)
    "domain_config": None,
    "allowed_tools": ["*"],
    "read_only": False,
}

# Write-tier tools that read_only mode blocks
WRITE_TOOLS = frozenset(
    {
        "ohm_create_node",
        "ohm_create_edge",
        "ohm_batch",
        "ohm_observe",
        "ohm_challenge",
        "ohm_support",
        "ohm_update_state",
    }
)


def _profile_defaults() -> dict[str, Any]:
    """Return a profile dict seeded from the legacy flat config keys."""
    return {
        "name": "default",
        "ohm_url": config.get("ohm_url", "http://127.0.0.1:8710"),
        "token": config.get("token", ""),
        "agent_id": config.get("agent_id", "mcp"),
        "tenant_id": config.get("tenant_id", ""),
        "token_type": config.get("token_type", "agent"),
        "domain_config": config.get("domain_config"),
        "allowed_tools": config.get("allowed_tools", ["*"]),
        "read_only": config.get("read_only", False),
    }


def _normalize_profiles() -> None:
    """Ensure ``config`` always has a ``profiles`` list and an active profile.

    If the loaded config does not contain ``profiles``, synthesize a single
    implicit profile from the legacy flat keys and name it ``default``.
    """
    profiles = config.get("profiles")
    config["_profiles_explicit"] = bool(profiles)
    if not profiles:
        profiles = [_profile_defaults()]
        config["profiles"] = profiles
    # Ensure every profile has a name.
    for i, p in enumerate(profiles):
        if "name" not in p or not p["name"]:
            p["name"] = f"profile_{i}"
    # Resolve active profile name.
    active = config.get("active_profile")
    names = [p.get("name") for p in profiles]
    if not active or active not in names:
        config["active_profile"] = names[0]


def load_config_file(path: str) -> None:
    """Load config from a JSON file, overriding env-var defaults (OHM-yzyk.1.2).

    Env vars still take priority over config file values for backward compat.
    """
    with open(path) as f:
        data = json.loads(f.read())
    # Replace profiles wholesale if the file defines them; otherwise clear any
    # previously loaded profiles so _normalize_profiles() builds a fresh implicit
    # default profile from the new flat keys.
    if "profiles" in data:
        config["profiles"] = data["profiles"]
    else:
        config.pop("profiles", None)
    for key in (
        "ohm_url",
        "token",
        "agent_id",
        "tenant_id",
        "token_type",
        "domain_config",
        "allowed_tools",
        "read_only",
        "log_path",
        "temp_path",
        "transport",
        "active_profile",
    ):
        if key in data:
            config[key] = data[key]
    # Env vars override config file for backward compat
    if os.environ.get("OHM_URL"):
        config["ohm_url"] = os.environ.get("OHM_URL")
    if os.environ.get("OHM_TOKEN"):
        config["token"] = os.environ.get("OHM_TOKEN")
    if os.environ.get("OHM_AGENT"):
        config["agent_id"] = os.environ.get("OHM_AGENT")
    if os.environ.get("OHM_TENANT_ID"):
        config["tenant_id"] = os.environ.get("OHM_TENANT_ID")
    _normalize_profiles()


def get_profiles() -> list[dict[str, Any]]:
    """Return all configured profiles."""
    if "profiles" not in config:
        _normalize_profiles()
    return list(config["profiles"])


def get_active_profile() -> dict[str, Any]:
    """Return the currently active profile.

    For the implicit default profile (no explicit ``profiles`` array in the
    config), the active profile is simply the current legacy flat config
    keys. This keeps existing callers — including tests that mutate
    ``config["allowed_tools"]`` directly — working without change.

    For explicit ``profiles`` arrays, the active profile's explicit keys
    take precedence, with any missing fields falling back to the legacy
    defaults.
    """
    if "profiles" not in config:
        _normalize_profiles()
    if not config.get("_profiles_explicit", False):
        return _profile_defaults()
    active_name = config.get("active_profile")
    active: dict[str, Any] | None = None
    for p in config["profiles"]:
        if p.get("name") == active_name:
            active = p
            break
    if active is None:
        active = config["profiles"][0]
    merged = _profile_defaults()
    merged.update(active)
    return merged


def set_active_profile(name: str) -> bool:
    """Activate a profile by name. Returns True on success, False if not found."""
    if "profiles" not in config:
        _normalize_profiles()
    names = {p.get("name") for p in config["profiles"]}
    if name not in names:
        return False
    config["active_profile"] = name
    return True


def is_tool_allowed(tool_name: str, profile: dict[str, Any] | None = None) -> bool:
    """Check if a tool is permitted by allowed_tools and read_only (OHM-yzyk.1.2).

    Semantics for allowed_tools:
    - Missing or ["*"]: all tools allowed.
    - Empty list []: interpreted as deny-all (no tools allowed). Use ["*"] for broad access.
    - Specific list: only those tool names are allowed.
    """
    if profile is None:
        profile = get_active_profile()
    if profile.get("read_only", False) and tool_name in WRITE_TOOLS:
        return False
    allowed = profile.get("allowed_tools", ["*"])
    if not allowed:
        return False
    if allowed == ["*"]:
        return True
    return tool_name in allowed


def _should_send_tenant_header(profile: dict[str, Any] | None = None) -> bool:
    """Decide whether to send X-Tenant-ID (OHM-yzyk.1.1).

    - Admin agent tokens (token_type="agent"): send X-Tenant-ID if tenant_id is set.
    - Customer API keys (token_type="customer"): never send X-Tenant-ID.
      The key itself is tenant-scoped; X-Tenant-ID is ignored after OHM-tss4.19.
    """
    if profile is None:
        profile = get_active_profile()
    if not profile.get("tenant_id"):
        return False
    token_type = profile.get("token_type", "agent")
    return token_type == "agent"


def make_headers(profile: dict[str, Any] | None = None) -> dict[str, str]:
    """Build HTTP headers from the active profile (OHM-yzyk.1.1, OHM-yzyk.3).

    X-Tenant-ID is only sent for admin agent tokens with a tenant_id set.
    Customer API keys never send X-Tenant-ID — the key selects the tenant.
    """
    if profile is None:
        profile = get_active_profile()
    h: dict[str, str] = {"Content-Type": "application/json"}
    if profile.get("token"):
        h["Authorization"] = f"Bearer {profile['token']}"
    if _should_send_tenant_header(profile):
        h["X-Tenant-ID"] = profile["tenant_id"]
    h["X-OHM-Agent"] = profile.get("agent_id", "mcp")
    return h


def validate_domain_config(expected: str | None, actual_schema: dict) -> bool:
    """Check if the configured domain_config matches the daemon's schema (OHM-yzyk.1.2 #4).

    Args:
        expected: The domain_config name from the MCP config (e.g., 'devsecops.json').
        actual_schema: The /schema response from the daemon.

    Returns:
        True if they match or expected is None (no validation needed).
        False if there's a mismatch.
    """
    if not expected:
        return True
    actual = actual_schema.get("schema", "")
    # The daemon returns schema name like "devops" or "topo"
    expected_base = expected.replace(".json", "")
    return actual == expected_base or actual == expected
