"""Integration tests for OHM MCP agent profiles (OHM-yzyk.3).

These tests verify that a single local ``ohm-mcp`` sidecar can carry
multiple instance/tenant profiles and switch between them at runtime.
"""

import json

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

from tests.test_mcp_e2e import _provision_tenant, ohmd  # noqa: F401


def _load_multi_profile_config(base_url: str, token_a: str, token_b: str, tmp_path) -> None:
    from ohm.mcp.config import load_config_file

    cfg = {
        "profiles": [
            {
                "name": "alpha",
                "ohm_url": base_url,
                "token": token_a,
                "agent_id": "test-mcp-profiles",
                "tenant_id": "alpha",
                "token_type": "customer",
                "allowed_tools": ["*"],
                "read_only": False,
            },
            {
                "name": "beta",
                "ohm_url": base_url,
                "token": token_b,
                "agent_id": "test-mcp-profiles",
                "tenant_id": "beta",
                "token_type": "customer",
                "allowed_tools": ["ohm_stats", "ohm_get_node"],
                "read_only": True,
            },
        ],
        "active_profile": "alpha",
    }
    cfg_path = tmp_path / "mcp-profiles-multi.json"
    cfg_path.write_text(json.dumps(cfg))
    load_config_file(str(cfg_path))


async def test_profile_switch_changes_tenant(ohmd, tmp_path):
    """ohm_select_profile makes subsequent calls hit the selected tenant."""
    base_url, admin_token, _db_path = ohmd
    token_alpha = _provision_tenant(base_url, admin_token, "profile_alpha", "ohm")
    token_beta = _provision_tenant(base_url, admin_token, "profile_beta", "ohm")

    _load_multi_profile_config(base_url, token_alpha, token_beta, tmp_path)

    from ohm.mcp.server import call_tool

    # Seed a node in each tenant
    result_a = await call_tool(
        "ohm_create_node",
        {"id": "alpha_node", "label": "Alpha", "node_type": "concept", "format": "json"},
    )
    assert not result_a.isError, f"alpha create failed: {result_a.content}"

    await call_tool("ohm_select_profile", {"name": "beta", "format": "json"})

    # Beta is read-only, so create should be blocked.
    result_blocked = await call_tool(
        "ohm_create_node",
        {"id": "beta_node_blocked", "label": "Beta", "node_type": "concept", "format": "json"},
    )
    assert result_blocked.isError
    text = result_blocked.content[0].text
    payload = json.loads(text)
    assert payload.get("error") in ("tool_blocked", "tool_not_allowed"), payload

    # Beta allows stats.
    result_stats = await call_tool("ohm_stats", {"format": "json"})
    assert not result_stats.isError, f"beta stats failed: {result_stats.content}"


async def test_profile_list_and_default(ohmd, tmp_path):
    """ohm_list_profiles returns names and the active profile."""
    base_url, admin_token, _db_path = ohmd
    token_alpha = _provision_tenant(base_url, admin_token, "profile_list_alpha", "ohm")
    token_beta = _provision_tenant(base_url, admin_token, "profile_list_beta", "ohm")
    _load_multi_profile_config(base_url, token_alpha, token_beta, tmp_path)

    from ohm.mcp.server import call_tool

    result = await call_tool("ohm_list_profiles", {"format": "json"})
    assert not result.isError, f"list_profiles failed: {result.content}"
    payload = json.loads(result.content[0].text)
    assert set(payload["profiles"]) == {"alpha", "beta"}
    assert payload["active"] == "alpha"


async def test_select_unknown_profile_errors(ohmd, tmp_path):
    """Selecting a non-existent profile returns an error."""
    base_url, admin_token, _db_path = ohmd
    token_alpha = _provision_tenant(base_url, admin_token, "profile_unknown_alpha", "ohm")
    token_beta = _provision_tenant(base_url, admin_token, "profile_unknown_beta", "ohm")
    _load_multi_profile_config(base_url, token_alpha, token_beta, tmp_path)

    from ohm.mcp.server import call_tool

    result = await call_tool("ohm_select_profile", {"name": "gamma", "format": "json"})
    assert result.isError
    payload = json.loads(result.content[0].text)
    assert payload["error"] == "profile_not_found"
    assert "alpha" in payload["available"] and "beta" in payload["available"]
