"""Integration tests for the FastMCP-hosted OHM MCP gateway.

These tests require the optional ``gateway`` extras (fastmcp + httpx). They are
marked with ``integration`` and automatically skip when fastmcp is not
installed.
"""

import json
import os
import sys

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

fastmcp = pytest.importorskip("fastmcp", reason="fastmcp required for gateway tests")

from tests.test_mcp_e2e import _provision_tenant, ohmd  # noqa: F401


@pytest.fixture(scope="module")
def gateway_profile():
    """Ensure OHM_GATEWAY_PROFILE is not polluted across tests."""
    original = os.environ.get("OHM_GATEWAY_PROFILE")
    yield
    if original is None:
        os.environ.pop("OHM_GATEWAY_PROFILE", None)
    else:
        os.environ["OHM_GATEWAY_PROFILE"] = original


async def test_gateway_stats_tool_forwards_to_tenant(ohmd, gateway_profile):
    """A gateway tool call resolves the API key and forwards to the tenant daemon."""
    base_url, admin_token, _db_path = ohmd
    tenant_id = "gateway_test"
    domain = "ohm"

    customer_token = _provision_tenant(base_url, admin_token, tenant_id, domain)

    api_key = "gateway-integration-key"
    os.environ["OHM_GATEWAY_PROFILE"] = json.dumps(
        [
            {
                "api_key": api_key,
                "ohm_url": base_url,
                "ohm_token": customer_token,
                "agent_id": "gateway-test",
                "tenant_id": tenant_id,
                "allowed_tools": ["*"],
                "read_only": True,
            }
        ]
    )

    if "ohm.mcp.gateway" in sys.modules:
        del sys.modules["ohm.mcp.gateway"]

    from ohm.mcp.gateway import _register_tools, mcp
    from fastmcp.server.dependencies import CurrentContext, CurrentHeaders

    _register_tools()
    ft = await mcp.get_tool("ohm_stats")
    assert ft is not None

    result = await ft.fn(
        ctx=CurrentContext(),
        headers={"authorization": f"Bearer {api_key}"},
        format="json",
    )
    if isinstance(result, str):
        payload = json.loads(result)
    else:
        payload = result
    assert "error" not in payload, f"gateway returned error payload: {payload}"
    data = payload.get("data", payload)
    assert any(k in data for k in ("total_nodes", "total_edges")), f"unexpected stats: {payload}"


async def test_gateway_unknown_api_key_blocked(ohmd, gateway_profile):
    """Requests with an unknown API key are rejected at the gateway edge."""
    base_url, admin_token, _db_path = ohmd
    tenant_id = "gateway_auth"
    domain = "ohm"
    customer_token = _provision_tenant(base_url, admin_token, tenant_id, domain)

    os.environ["OHM_GATEWAY_PROFILE"] = json.dumps(
        [
            {
                "api_key": "good-key",
                "ohm_url": base_url,
                "ohm_token": customer_token,
                "agent_id": "gateway-test",
                "tenant_id": tenant_id,
                "allowed_tools": ["*"],
                "read_only": True,
            }
        ]
    )

    if "ohm.mcp.gateway" in sys.modules:
        del sys.modules["ohm.mcp.gateway"]

    from ohm.mcp.gateway import _register_tools, mcp
    from fastmcp.server.dependencies import CurrentContext, CurrentHeaders

    _register_tools()
    ft = await mcp.get_tool("ohm_stats")
    result = await ft.fn(
        ctx=CurrentContext(),
        headers={"authorization": "Bearer bad-key"},
        format="json",
    )
    if isinstance(result, str):
        payload = json.loads(result)
    else:
        payload = result
    assert payload.get("error") == "auth_failed"
