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

from ohm.mcp.agora_nudges import generate_agora_nudges
from ohm.mcp.config import WRITE_TOOLS
from ohm.mcp.conversation_state import auto_update_from_tool, get_store, resolve_thread_id
from ohm.mcp.deliberation import handle_deliberation_action
from ohm.mcp.encoding import encode_payload, requested_format
from ohm.mcp.gateway_helpers import _strip_nulls, _deduplicate_nudges
from ohm.mcp.tools import all_tools as _all_tools

logger = logging.getLogger(__name__)

# OHM-759: Long-analysis tools that support background task execution
_LONG_ANALYSIS_TOOLS = {"ohm_inference", "ohm_discover", "ohm_voi", "ohm_refute"}


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
        logger.warning("No gateway profiles found. Set OHM_GATEWAY_PROFILES or OHM_GATEWAY_PROFILE.")
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


# OHM-770: Session-scoped profile cache for clients that drop
# Authorization after the initial initialize request (e.g. Copilot Studio).
_SESSION_PROFILES: dict[str, GatewayProfile] = {}


def _resolve_profile(headers: dict[str, str]) -> GatewayProfile | None:
    # 1. Check session cache first (OHM-770)
    session_id = headers.get("mcp-session-id", headers.get("Mcp-Session-Id", ""))
    if session_id and session_id in _SESSION_PROFILES:
        return _SESSION_PROFILES[session_id]

    # 2. Bearer token lookup (existing logic)
    auth = headers.get("authorization", headers.get("Authorization", ""))
    if auth.startswith("Bearer "):
        key = auth.split(" ", 1)[1].strip()
        profile = _profiles().get(key)
        if profile and session_id:
            _SESSION_PROFILES[session_id] = profile
        if profile:
            return profile
        return None

    # 3. OHM-771: Allow unauthenticated access in single-profile dev mode
    all_profiles = _profiles()
    if len(all_profiles) == 1 and os.environ.get("OHM_GATEWAY_ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true", "yes"):
        return next(iter(all_profiles.values()))

    return None


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
    """Build a FastMCP tool handler bound to a specific tool name.

    OHM-760: Handlers return structured data (dict) by default so
    FastMCP populates structuredContent and generates outputSchema
    automatically. When the caller requests TOON format (via the
    ``format`` argument or Accept header), the handler returns a
    TOON-encoded string instead — a text fallback for clients that
    prefer the compact representation.
    """

    async def _handler(
        *args: Any,
        ctx: Context = CurrentContext(),
        headers: dict[str, str] = CurrentHeaders(),
        **kwargs: Any,
    ) -> Any:
        from ohm.mcp.dispatch import build_request

        start = time.time()

        # Response format is always accepted as a tool argument even if the
        # request is rejected before reaching OHM. The default is TOON when
        # python-toon is installed; callers should pass format=json for text.
        fmt = requested_format(kwargs)
        kwargs.pop("format", None)
        use_text = fmt == "toon"

        # OHM-789: Resolve thread_id for conversation-state tracking.
        # thread_id is resolved before profile so it's available even
        # for auth-failed responses (though we skip conversation extras
        # for those since profile is None).
        thread_id = resolve_thread_id(kwargs, headers, None)

        def _respond(data: Any) -> Any:
            """Encode response — dict for structured content, str for TOON.

            OHM-787: Successful tool responses are wrapped in an
            {ok, data, ohm_context} envelope. Error responses stay
            flat for backward compatibility.
            OHM-789: ohm_context enriched with conversation-state extras.
            """
            if isinstance(data, dict) and "error" in data:
                # Error responses: no envelope, keep flat
                if use_text:
                    return encode_payload(data, fmt)
                return _strip_nulls(data)

            # Extract nudges from data if present (they move to ohm_context)
            nudges = None
            if isinstance(data, dict) and "nudges" in data:
                nudges = data.pop("nudges", None)

            # Build ohm_context
            ohm_context: dict[str, Any] = {}
            if nudges:
                ohm_context["nudges"] = nudges
            if profile:
                ohm_context["agent_state"] = {"agent_id": profile.agent_id}
                if profile.tenant_id:
                    ohm_context["agent_state"]["tenant_id"] = profile.tenant_id

            # OHM-789: Add conversation-state extras to ohm_context
            conv_extras = get_store().get_ohm_context_extras(thread_id)
            ohm_context.update(conv_extras)

            # Wrap in envelope
            envelope: dict[str, Any] = {"ok": True, "data": data}
            if ohm_context:
                envelope["ohm_context"] = ohm_context

            if use_text:
                return encode_payload(envelope, fmt)
            return _strip_nulls(envelope)

        profile = _resolve_profile(headers)
        if profile is None:
            result = _respond({"error": "auth_failed", "message": "Invalid or missing API key"})
            _audit(None, tool_name, status="auth_failed", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result

        if not profile.is_tool_allowed(tool_name):
            reason = "read_only" if profile.read_only and tool_name in WRITE_TOOLS else "not_allowed"
            result = _respond({"error": "tool_blocked", "message": f"Tool '{tool_name}' is not allowed for this API key"})
            _audit(profile, tool_name, status=reason, latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result

        # OHM-789: ohm_conversation is handled locally in the gateway (no
        # daemon forward) — conversation state lives in gateway memory.
        if tool_name == "ohm_conversation":
            action = kwargs.get("action", "get")
            if action == "get":
                result_data: dict[str, Any] = get_store().get_state(thread_id) or {"thread_id": thread_id, "message": "No conversation state yet"}
            elif action == "evict":
                get_store().evict(thread_id)
                result_data = {"thread_id": thread_id, "evicted": True}
            elif action == "answer_question":
                qt = kwargs.get("question_text", "")
                found = get_store().answer_question(thread_id, qt)
                result_data = {"thread_id": thread_id, "answered": found}
            else:
                updates = kwargs.get("updates", {})
                updates.setdefault("thread_id", thread_id)
                result_data = get_store().update_state(thread_id, updates)
            result = _respond(result_data)
            _audit(profile, tool_name, status="ok", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result

        # OHM-793: ohm_deliberation is handled locally in the gateway.
        if tool_name == "ohm_deliberation":
            action = kwargs.get("action", "get")
            result_data = handle_deliberation_action(thread_id, action, kwargs, profile.agent_id)
            result = _respond(result_data)
            _audit(profile, tool_name, status="ok", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result

        try:
            method, path, body = build_request(tool_name, kwargs, profile.agent_id)
        except NotImplementedError as e:
            result = _respond({"error": "not_implemented", "message": str(e)})
            _audit(profile, tool_name, status="not_implemented", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result
        except (KeyError, ValueError) as e:
            result = _respond({"error": "invalid_arguments", "message": str(e)})
            _audit(profile, tool_name, status="invalid_arguments", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result

        if profile.is_high_blast_radius(tool_name):
            # OHM-761: Use FastMCP elicitation for in-band approval when
            # the client hasn't pre-approved via the X-OHM-Approve header.
            # Falls back to the header-based error for non-interactive clients.
            approval = headers.get("x-ohm-approve", "")
            if approval != tool_name:
                # Try elicitation first (interactive clients)
                approved = False
                if ctx:
                    try:
                        resp = await ctx.elicit(
                            f"Confirm execution of high-blast-radius tool '{tool_name}'?",
                            result_schema={"type": "boolean"},
                        )
                        approved = bool(resp)
                    except Exception:
                        pass  # Non-interactive client or elicit unavailable
                if not approved:
                    result = _respond(
                        {
                            "error": "approval_required",
                            "message": (f"Tool '{tool_name}' requires approval. Either respond to the elicitation prompt or resend with X-OHM-Approve: {tool_name} header."),
                        }
                    )
                    _audit(profile, tool_name, status="approval_required", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
                    return result

        try:
            # OHM-759: Report progress for long-analysis tools
            if tool_name in _LONG_ANALYSIS_TOOLS and ctx:
                try:
                    await ctx.report_progress(0, 1, f"Forwarding {tool_name} to ohmd...")
                except Exception:
                    pass
            data = await _forward(profile, method, path, body)

            # OHM-780: Process belief_statement from write tools.
            # Parse, compare to graph posterior (if available), inject nudge.
            belief_statement = kwargs.get("belief_statement")
            if belief_statement and tool_name in WRITE_TOOLS:
                try:
                    from ohm.mcp.belief import parse_belief_statement, compare_belief_to_posterior

                    parsed = parse_belief_statement(belief_statement)
                    if parsed and parsed.get("target"):
                        # Best-effort: try to get graph posterior via /belief
                        try:
                            belief_data = await _forward(
                                profile,
                                "GET",
                                f"/belief?target={parsed['target']}",
                                None,
                            )
                            graph_p = belief_data.get("posterior", {}).get("P(bad)", 0.0)
                            comparison = compare_belief_to_posterior(
                                parsed["claimed_probability"],
                                {"P(bad)": graph_p},
                                parsed.get("state", "bad"),
                            )
                            if comparison["severity"] >= 2:
                                nudges = data.get("nudges", [])
                                nudges.append(
                                    {
                                        "type": "belief_statement_suggestion",
                                        "message": (f"Your stated belief P({parsed['state']})={parsed['claimed_probability']:.2f} diverges from the graph posterior by {comparison['divergence']:.2f} (graph: {graph_p:.2f}). Consider revising."),
                                        "severity": "soft",
                                    }
                                )
                                data["nudges"] = nudges
                        except Exception:
                            pass
                except Exception:
                    pass

            if tool_name in _LONG_ANALYSIS_TOOLS and ctx:
                try:
                    await ctx.report_progress(1, 1, "Done")
                except Exception:
                    pass
            # OHM-760: strip nulls from ALL responses (not just writes) for
            # clean structured content. Previously only applied to write tools.
            # OHM-747-3 / OHM-764: deduplicate nudges per session.
            session_key = profile.agent_id
            try:
                if ctx and hasattr(ctx, "request_id"):
                    session_key = f"{profile.agent_id}:{ctx.request_id}"
            except Exception:
                pass
            # OHM-789: Auto-update conversation state from tool call before
            # dedup (so nudge_history captures all nudges from the daemon).
            auto_update_from_tool(thread_id, tool_name, kwargs, profile.agent_id, data)
            # OHM-791: Generate agora-aware nudges from conversation context.
            try:
                conv_state = get_store().get_state(thread_id)
                agora_nudges = generate_agora_nudges(
                    thread_id=thread_id,
                    tool_name=tool_name,
                    kwargs=kwargs,
                    agent_id=profile.agent_id,
                    response_data=data,
                    conversation_state=conv_state,
                )
                if agora_nudges:
                    existing = data.get("nudges", [])
                    existing.extend(agora_nudges)
                    data["nudges"] = existing
            except Exception:
                logger.debug("agora nudge generation failed", exc_info=True)
            data = _deduplicate_nudges(session_key, data)
            result = _respond(data)
            _audit(profile, tool_name, status="ok", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result
        except httpx.HTTPStatusError as e:
            body_text = e.response.text
            try:
                detail = json.loads(body_text)
            except Exception:
                detail = body_text
            # OHM-761: Surface orphan edge targets with an actionable message
            if e.response.status_code in (404, 422) and tool_name == "ohm_create_edge":
                msg = str(detail)
                if "not found" in msg.lower() or "does not exist" in msg.lower():
                    detail = {
                        "error": "orphan_edge_target",
                        "message": (f"One or both edge endpoints don't exist yet. Create the node(s) first, or use ohm_batch to create nodes and edges atomically. Server detail: {msg}"),
                        "hint": "Use ohm_create_node first, or ohm_batch with nodes + edges in one call.",
                    }
            payload = {"error": e.response.status_code, "detail": detail}
            result = _respond(payload)
            _audit(profile, tool_name, status=f"http_{e.response.status_code}", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result
        except Exception as e:
            payload = {"error": "gateway_error", "message": f"{type(e).__name__}: {e}"}
            result = _respond(payload)
            _audit(profile, tool_name, status="gateway_error", latency_ms=(time.time() - start) * 1000, size=len(str(result)))
            return result

    _handler.__name__ = f"handle_{tool_name}"
    return _handler


# OHM-757: FastMCP native auth — GatewayTokenVerifier maps API keys
# to AccessToken objects with the GatewayProfile as claims.
# The inline _resolve_profile in the handler is the fallback path;
# when FastMCP auth is active, the profile comes from the auth context.
mcp = FastMCP("ohm-gateway", tasks=True)


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
            "allow_unauthenticated": os.environ.get("OHM_GATEWAY_ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true", "yes"),
        }
    )


def _tool_annotations(tool_name: str):
    """Derive MCP tool annotations from WRITE_TOOLS / high_blast_radius.

    OHM-763: Surface tool safety to MCP clients so they can cheaply tell
    reads from writes from destructive ops.

    - Not in WRITE_TOOLS → readOnlyHint: True
    - In WRITE_TOOLS → readOnlyHint: False, idempotentHint: True
      (most OHM writes are upserts/idempotent via dedup)
    - high_blast_radius tools → destructiveHint: True
    """
    try:
        from fastmcp.tools.annotations import ToolAnnotations
    except ImportError:
        return None

    is_write = tool_name in WRITE_TOOLS
    is_destructive = tool_name in {"ohm_delete"}  # only delete-style ops

    return ToolAnnotations(
        readOnlyHint=not is_write,
        destructiveHint=is_destructive,
        idempotentHint=is_write,  # OHM writes use upsert/dedup semantics
        openWorldHint=True,  # OHM agents interact with external systems
    )


def _register_tools() -> None:
    """Register all OHM tools that make sense in a remote gateway."""
    from fastmcp.tools.function_tool import FunctionTool
    from fastmcp.utilities.tasks import TaskConfig

    for tool in _all_tools():
        if tool.name in ("ohm_list_instances", "ohm_list_profiles", "ohm_select_profile"):
            # Local-only: the gateway resolves profiles per HTTP request.
            continue
        handler = _build_tool_handler(tool.name)
        kwargs: dict[str, Any] = dict(
            name=tool.name,
            description=tool.description,
            parameters=tool.inputSchema,
            fn=handler,
            annotations=_tool_annotations(tool.name),
        )
        if tool.name in _LONG_ANALYSIS_TOOLS:
            kwargs["task_config"] = TaskConfig(mode="optional")
        ft = FunctionTool(**kwargs)
        mcp.add_tool(ft)


async def main_async(host: str = "0.0.0.0", port: int = 8080, transport: str = "sse") -> None:
    """Run the gateway's HTTP server."""
    if not _profiles():
        logger.error("No gateway profiles configured; refusing to start")
        sys.exit(1)
    _register_tools()

    # OHM-758: Register middleware for cross-cutting concerns
    from ohm.mcp.middleware import FormatMiddleware, AuditMiddleware

    # Determine audit path from first profile that has one
    audit_path = None
    for p in _profiles().values():
        if p.audit_path:
            audit_path = p.audit_path
            break
    mcp.add_middleware(AuditMiddleware(audit_path=audit_path))
    mcp.add_middleware(FormatMiddleware(session_key="default"))

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
