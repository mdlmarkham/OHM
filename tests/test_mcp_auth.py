"""Tests for OHM-757: FastMCP native auth provider."""

from __future__ import annotations

import pytest


class TestGatewayTokenVerifier:
    """Test GatewayTokenVerifier maps API keys to AccessToken."""

    @pytest.mark.asyncio
    async def test_valid_token_returns_access_token(self):
        """A valid API key returns an AccessToken with profile claims."""
        from ohm.mcp.auth import GatewayTokenVerifier
        from ohm.mcp.gateway import GatewayProfile

        verifier = GatewayTokenVerifier()
        # Mock the profiles
        verifier._profiles_cache = {
            "ohm-gw-test-key": {
                "api_key": "ohm-gw-test-key",
                "ohm_url": "http://127.0.0.1:8710",
                "ohm_token": "ohm-cu-internal",
                "agent_id": "test-agent",
                "tenant_id": "test-tenant",
                "allowed_tools": ["*"],
                "read_only": False,
                "high_blast_radius": [],
                "audit_path": None,
                "rate_limit": None,
            }
        }

        result = await verifier.verify_token("ohm-gw-test-key")
        assert result is not None
        assert result.token == "ohm-gw-test-key"
        assert result.client_id == "test-agent"
        assert "write" in result.scopes
        assert "read" in result.scopes
        assert result.claims["tenant_id"] == "test-tenant"
        assert result.claims["agent_id"] == "test-agent"
        assert result.claims["gateway_profile"]["ohm_url"] == "http://127.0.0.1:8710"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self):
        """An invalid API key returns None (401)."""
        from ohm.mcp.auth import GatewayTokenVerifier

        verifier = GatewayTokenVerifier()
        verifier._profiles_cache = {}

        result = await verifier.verify_token("invalid-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_read_only_profile_gets_read_scope_only(self):
        """A read-only profile gets only the 'read' scope."""
        from ohm.mcp.auth import GatewayTokenVerifier

        verifier = GatewayTokenVerifier()
        verifier._profiles_cache = {
            "ro-key": {
                "api_key": "ro-key",
                "ohm_url": "http://127.0.0.1:8710",
                "ohm_token": "internal",
                "agent_id": "observer",
                "tenant_id": None,
                "allowed_tools": ["*"],
                "read_only": True,
                "high_blast_radius": [],
                "audit_path": None,
                "rate_limit": None,
            }
        }

        result = await verifier.verify_token("ro-key")
        assert result is not None
        assert "read" in result.scopes
        assert "write" not in result.scopes

    @pytest.mark.asyncio
    async def test_profile_claims_carry_full_profile(self):
        """The full GatewayProfile is carried in claims for downstream use."""
        from ohm.mcp.auth import GatewayTokenVerifier

        verifier = GatewayTokenVerifier()
        verifier._profiles_cache = {
            "full-key": {
                "api_key": "full-key",
                "ohm_url": "http://10.0.0.5:9999",
                "ohm_token": "ohm-cu-secret",
                "agent_id": "metis",
                "tenant_id": "acme_corp",
                "allowed_tools": ["ohm_search", "ohm_get_node"],
                "read_only": False,
                "high_blast_radius": ["ohm_delete"],
                "audit_path": "/var/log/ohm/audit.jsonl",
                "rate_limit": "100/hour",
            }
        }

        result = await verifier.verify_token("full-key")
        assert result is not None
        profile = result.claims["gateway_profile"]
        assert profile["ohm_url"] == "http://10.0.0.5:9999"
        assert profile["tenant_id"] == "acme_corp"
        assert profile["high_blast_radius"] == ["ohm_delete"]
        assert profile["audit_path"] == "/var/log/ohm/audit.jsonl"
