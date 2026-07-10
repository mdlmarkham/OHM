"""FastMCP native auth provider for the OHM gateway (OHM-757).

Replaces the hand-rolled _resolve_profile / Bearer parsing with a
FastMCP TokenVerifier that maps gateway API keys to AccessToken
objects carrying the GatewayProfile as claims.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastmcp.server.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)


class GatewayTokenVerifier(TokenVerifier):
    """Verify OHM gateway API keys via FastMCP native auth (OHM-757).

    Maps each gateway API key to an AccessToken whose ``claims`` dict
    carries the full GatewayProfile (ohm_url, ohm_token, agent_id,
    tenant_id, allowed_tools, read_only, high_blast_radius, audit_path,
    rate_limit). Tool handlers retrieve the profile from the auth
    context instead of parsing the Authorization header manually.
    """

    def __init__(self) -> None:
        super().__init__()
        # Lazy-load profiles to avoid circular import
        self._profiles_cache: dict[str, dict[str, Any]] | None = None

    def _profiles(self) -> dict[str, dict[str, Any]]:
        """Return gateway profiles as plain dicts keyed by API key."""
        if self._profiles_cache is None:
            from ohm.mcp.gateway import _profiles as _get_profiles

            self._profiles_cache = {}
            for key, profile in _get_profiles().items():
                self._profiles_cache[key] = asdict(profile)
        return self._profiles_cache

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token and return an AccessToken with profile claims.

        Returns None if the token doesn't match any configured profile,
        causing FastMCP to reject the request with 401 Unauthorized.
        """
        profile = self._profiles().get(token)
        if profile is None:
            return None

        # Determine scopes from allowed_tools and read_only
        scopes: list[str] = []
        if profile.get("read_only"):
            scopes.append("read")
        else:
            scopes.append("read")
            scopes.append("write")

        return AccessToken(
            token=token,
            client_id=profile.get("agent_id", "unknown"),
            scopes=scopes,
            claims={
                "gateway_profile": profile,
                "tenant_id": profile.get("tenant_id"),
                "agent_id": profile.get("agent_id"),
            },
        )
