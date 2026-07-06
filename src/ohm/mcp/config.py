"""MCP server config logic — importable without the mcp package (OHM-yzyk.1.2).

This module contains the config loading, tool filtering, and header
construction logic for the OHM MCP server. It is kept separate from
``server.py`` so it can be tested without the ``mcp`` package installed.

OHM-yzyk.1.1: token_type field controls whether X-Tenant-ID is sent.
Customer API keys are already tenant-scoped (the key selects the tenant),
so sending X-Tenant-ID is unnecessary and ignored after OHM-tss4.19.
Admin agent tokens need X-Tenant-ID to select the tenant.
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
WRITE_TOOLS = frozenset({
    "ohm_create_node",
    "ohm_create_edge",
    "ohm_observe",
    "ohm_challenge",
    "ohm_support",
    "ohm_update_state",
})


def load_config_file(path: str) -> None:
    """Load config from a JSON file, overriding env-var defaults (OHM-yzyk.1.2).

    Env vars still take priority over config file values for backward compat.
    """
    with open(path) as f:
        data = json.loads(f.read())
    for key in ("ohm_url", "token", "agent_id", "tenant_id", "token_type",
                "domain_config", "allowed_tools", "read_only", "log_path",
                "temp_path", "transport"):
        if key in data:
            config[key] = data[key]
    # Env vars override config file for backward compat
    if os.environ.get("OHM_URL"):
        config["ohm_url"] = os.environ["OHM_URL"]
    if os.environ.get("OHM_TOKEN"):
        config["token"] = os.environ["OHM_TOKEN"]
    if os.environ.get("OHM_AGENT"):
        config["agent_id"] = os.environ["OHM_AGENT"]
    if os.environ.get("OHM_TENANT_ID"):
        config["tenant_id"] = os.environ["OHM_TENANT_ID"]


def is_tool_allowed(tool_name: str) -> bool:
    """Check if a tool is permitted by allowed_tools and read_only (OHM-yzyk.1.2)."""
    if config["read_only"] and tool_name in WRITE_TOOLS:
        return False
    allowed = config.get("allowed_tools", ["*"])
    if allowed == ["*"]:
        return True
    return tool_name in allowed


def _should_send_tenant_header() -> bool:
    """Decide whether to send X-Tenant-ID (OHM-yzyk.1.1).

    - Admin agent tokens (token_type="agent"): send X-Tenant-ID if tenant_id is set.
    - Customer API keys (token_type="customer"): never send X-Tenant-ID.
      The key itself is tenant-scoped; X-Tenant-ID is ignored after OHM-tss4.19.
    """
    if not config.get("tenant_id"):
        return False
    token_type = config.get("token_type", "agent")
    return token_type == "agent"


def make_headers() -> dict[str, str]:
    """Build HTTP headers from current config (OHM-yzyk.1.1).

    X-Tenant-ID is only sent for admin agent tokens with a tenant_id set.
    Customer API keys never send X-Tenant-ID — the key selects the tenant.
    """
    h: dict[str, str] = {"Content-Type": "application/json"}
    if config["token"]:
        h["Authorization"] = f"Bearer {config['token']}"
    if _should_send_tenant_header():
        h["X-Tenant-ID"] = config["tenant_id"]
    h["X-OHM-Agent"] = config["agent_id"]
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
    # The daemon returns schema name like "devsecops" or "topo"
    expected_base = expected.replace(".json", "")
    return actual == expected_base or actual == expected