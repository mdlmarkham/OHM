"""Hosted OHM MCP gateway using FastMCP.

Exposes the same tool surface as the local ``ohm-mcp`` sidecar over HTTP/SSE
and Streamable HTTP transports. Each API key maps to a tenant profile that
determines which OHM instance to call, which tools are allowed, and whether
writes are permitted.

Usage:
    OHM_GATEWAY_PROFILES=/etc/ohm/gateway-profiles.json \
        python -m ohm.mcp.gateway --host 0.0.0.0 --port 8080

Profile JSON (array of profiles):
    [
      {
        "api_key": "ohm-gw-...",
        "ohm_url": "http://127.0.0.1:8710",
        "ohm_token": "ohm-cu-...",
        "agent_id": "ci-runner-1",
        "tenant_id": "devops",
        "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_observe"],
        "read_only": false,
        "high_blast_radius": ["ohm_delete"]
      }
    ]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError as e:  # pragma: no cover
    raise ImportError("ohm-gateway requires httpx: pip install 'ohm[gateway]'") from e

try:
    from fastmcp import FastMCP
    from fastmcp.dependencies import CurrentContext, CurrentHeaders
    from fastmcp.server.context import Context
except ImportError as e:  # pragma: no cover
    raise ImportError("ohm-gateway requires fastmcp: pip install 'ohm[gateway]'") from e

from ohm.mcp.config import WRITE_TOOLS
from ohm.mcp.encoding import DEFAULT_FORMAT, encode_payload, requested_format
from ohm.mcp.tools import all_tools as _all_tools

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayProfile:
    """Resolved tenant profile for one API key."""

    api_key: str
    ohm_url: str
    ohm_token: str
    agent_id: str
    tenant_id: str | None = None
    allowed_tools: list[str] = field(default_factory=lambda: ["*"])
    read_only: bool = False
    high_blast_radius: list[str] = field(default_factory=list)
    audit_path: str | None = None
    rate_limit: str | None = None

    def is_tool_allowed(self, name: str) -> bool:
        if self.read_only and name in WRITE_TOOLS:
            return False
        allowed = self.allowed_tools
        if allowed == ["*"] or "*" in allowed:
            return True
        return name in allowed

    def is_high_blast_radius(self, name: str) -> bool:
        return name in self.high_blast_radius


def _load_profiles() -> dict[str, GatewayProfile]:
    """Load tenant profiles from env var or default config path."""
    profiles_path = os.environ.get("OHM_GATEWAY_PROFILES")
    inline = os.environ.get("OHM_GATEWAY_PROFILE")
    default_path = os.path.expanduser("~/.ohm/gateway-profiles.json")

    raw: list[dict[str, Any]] = []
    if inline:
        raw = json.loads(inline)
    elif profiles_path:
        raw = json.loads(open(profiles_path).read())
    elif os.path.exists(default_path):
        raw = json.loads(open(default_path).read())
    else:
        logger.warning(
            "No gateway profiles found. Set OHM_GATEWAY_PROFILES or OHM_GATEWAY_PROFILE."
        )
        return {}

    if isinstance(raw, dict):
        raw = [raw]

    result: dict[str, GatewayProfile] = {}
    for item in raw:
        key = item.get("api_key")
        if not key:
            continue
        result[key] = GatewayProfile(
            api_key=key,
            ohm_url=item["ohm_url"].rstrip("/"),
            ohm_token=item["ohm_token"],
            agent_id=item.get("agent_id", "unknown"),
            tenant_id=item.get("tenant_id"),
            allowed_tools=item.get("allowed_tools", ["*"]),
            read_only=item.get("read_only", False),
            high_blast_radius=item.get("high_blast_radius", []),
            audit_path=item.get("audit_path"),
            rate_limit=item.get("rate_limit"),
        )
    return result


# module-level cache; profiles are small and rarely change at runtime
_PROFILES: dict[str, GatewayProfile] | None = None


def _profiles() -> dict[str, GatewayProfile]:
    global _PROFILES
    if _PROFILES is None:
        _PROFILES = _load_profiles()
    return _PROFILES


def _resolve_profile(headers: dict[str, str]) -> GatewayProfile | None:
    auth = headers.get("authorization", headers.get("Authorization", ""))
    if not auth.startswith("Bearer "):
        return None
    key = auth.split(" ", 1)[1]
    return _profiles().get(key)


def _audit(profile: GatewayProfile | None, tool: str, *, status: str, latency_ms: float, size: int) -> None:
    """Append an audit record if an audit path is configured."""
    if profile is None or not profile.audit_path:
        return
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "agent_id": profile.agent_id,
        "tenant_id": profile.tenant_id,
        "tool": tool,
        "status": status,
        "latency_ms": round(latency_ms, 2),
        "response_size": size,
        "key_hash": "***",
    }
    try:
        with open(profile.audit_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        logger.exception("gateway audit write failed")


def _ohm_headers(profile: GatewayProfile) -> dict[str, str]:
    headers: dict[str, str] = {"Authorization": f"Bearer {profile.ohm_token}"}
    if profile.tenant_id:
        headers["X-Tenant-ID"] = profile.tenant_id
    if profile.agent_id:
        headers["X-Ohm-Agent"] = profile.agent_id
    return headers


async def _forward(profile: GatewayProfile, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
    """Forward a single request to the tenant's OHM daemon."""
    url = f"{profile.ohm_url}{path}"
    headers = _ohm_headers(profile)
    async with httpx.AsyncClient(timeout=120) as client:
        if method == "GET":
            r = await client.get(url, headers=headers, follow_redirects=False)
        else:
            r = await client.post(url, headers=headers, json=body or {}, follow_redirects=False)
        r.raise_for_status()
        return r.json()


def _build_tool_handler(tool_name: str):
    """Build a FastMCP tool handler bound to a specific tool name."""

    async def _handler(
        *args: Any,
        ctx: Context = CurrentContext(),
        headers: dict[str, str] = CurrentHeaders(),
        **kwargs: Any,
    ) -> str:
        from ohm.mcp.dispatch import build_request

        start = time.time()

        # Response format is always accepted as a tool argument even if the
        # request is rejected before reaching OHM. The default is TOON when
        # python-toon is installed; callers should pass format=json for text.
        fmt = requested_format(kwargs)
        kwargs.pop("format", None)

        profile = _resolve_profile(headers)
        if profile is None:
            text = encode_payload(
                {"error": "auth_failed", "message": "Invalid or missing API key"},
                fmt,
            )
            _audit(None, tool_name, status="auth_failed", latency_ms=(time.time() - start) * 1000, size=len(text))
            return text

        if not profile.is_tool_allowed(tool_name):
            reason = "read_only" if profile.read_only and tool_name in WRITE_TOOLS else "not_allowed"
            text = encode_payload(
                {"error": "tool_blocked", "message": f"Tool '{tool_name}' is not allowed for this API key"},
                fmt,
            )
            _audit(profile, tool_name, status=reason, latency_ms=(time.time() - start) * 1000, size=len(text))
            return text

        try:
            method, path, body = build_request(tool_name, kwargs, profile.agent_id)
        except NotImplementedError as e:
            text = encode_payload({"error": "not_implemented", "message": str(e)}, fmt)
            _audit(profile, tool_name, status="not_implemented", latency_ms=(time.time() - start) * 1000, size=len(text))
            return text

        if profile.is_high_blast_radius(tool_name):
            # High-blast-radius tools require an explicit approval claim.
            approval = headers.get("x-ohm-approve", "")
            if approval != tool_name:
                text = encode_payload(
                    {
                        "error": "approval_required",
                        "message": f"Tool '{tool_name}' requires X-OHM-Approve: {tool_name} header",
                    },
                    fmt,
                )
                _audit(profile, tool_name, status="approval_required", latency_ms=(time.time() - start) * 1000, size=len(text))
                return text

        try:
            data = await _forward(profile, method, path, body)
            text = encode_payload(data, fmt)
            _audit(profile, tool_name, status="ok", latency_ms=(time.time() - start) * 1000, size=len(text))
            return text
        except httpx.HTTPStatusError as e:
            body_text = e.response.text
            try:
                detail = json.loads(body_text)
            except Exception:
                detail = body_text
            payload = {"error": e.response.status_code, "detail": detail}
            text = encode_payload(payload, fmt)
            _audit(profile, tool_name, status=f"http_{e.response.status_code}", latency_ms=(time.time() - start) * 1000, size=len(text))
            return text
        except Exception as e:
            payload = {"error": "gateway_error", "message": f"{type(e).__name__}: {e}"}
            text = encode_payload(payload, fmt)
            _audit(profile, tool_name, status="gateway_error", latency_ms=(time.time() - start) * 1000, size=len(text))
            return text

    _handler.__name__ = f"handle_{tool_name}"
    return _handler


mcp = FastMCP("ohm-gateway")


@mcp.custom_route("/health", methods=["GET"])
async def _health_route(request) -> Any:
    """Gateway health check including OHM backend reachability."""
    from starlette.responses import JSONResponse

    profiles = _profiles()
    backend_ok = False
    if profiles:
        profile = next(iter(profiles.values()))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{profile.ohm_url}/health",
                    headers={"Authorization": f"Bearer {profile.ohm_token}"},
                )
                backend_ok = r.status_code == 200
        except Exception as exc:
            logger.debug("backend health check failed: %s", exc)
    return JSONResponse(
        {
            "status": "ok",
            "profiles": len(profiles),
            "backend_reachable": backend_ok,
        }
    )


def _register_tools() -> None:
    """Register all OHM tools that make sense in a remote gateway."""
    from fastmcp.tools.function_tool import FunctionTool

    for tool in _all_tools():
        if tool.name == "ohm_list_instances":
            # Local-only: the gateway has no access to the client's registry.
            continue
        handler = _build_tool_handler(tool.name)
        ft = FunctionTool(
            name=tool.name,
            description=tool.description,
            parameters=tool.inputSchema,
            fn=handler,
        )
        mcp.add_tool(ft)


async def main_async(host: str = "0.0.0.0", port: int = 8080, transport: str = "sse") -> None:
    """Run the gateway's HTTP server."""
    if not _profiles():
        logger.error("No gateway profiles configured; refusing to start")
        sys.exit(1)
    _register_tools()
    app = mcp.http_app(transport=transport)  # type: ignore[arg-type]
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="OHM MCP Gateway")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--transport", choices=["sse", "streamable-http"], default="sse")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    import asyncio

    asyncio.run(main_async(args.host, args.port, args.transport))


if __name__ == "__main__":
    cli_main()
