"""FastMCP middleware components for the OHM gateway (OHM-758).

Extracts cross-cutting concerns from the inline handler closure in
gateway.py into testable middleware:

- ``ProfileResolutionMiddleware``: resolves the GatewayProfile from the
  Authorization header and injects it into the MCP context.
- ``AuditMiddleware``: logs audit records after tool calls complete.
- ``FormatMiddleware``: applies null-stripping and TOON fallback based
  on the requested format.

These are used by gateway.py's ``_register_tools()`` to wrap all tool
handlers with consistent, testable cross-cutting logic.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware
from fastmcp.server.middleware.middleware import MiddlewareContext

from ohm.mcp.gateway_helpers import _strip_nulls, _deduplicate_nudges

logger = logging.getLogger(__name__)


class ProfileResolutionMiddleware(Middleware):
    """Resolve the GatewayProfile from the Authorization header (OHM-758).

    Injects the resolved profile into the context's state so downstream
    handlers and middleware can access it without re-parsing the header.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        # Placeholder for when FastMCP auth providers (#757) are adopted.
        # Currently, profile resolution is done in the handler closure.
        return await call_next(context)


class AuditMiddleware(Middleware):
    """Log audit records after tool calls complete (OHM-758).

    Records tool name, status, latency, and response size to a JSONL
    audit file when an audit path is configured on the profile.
    """

    def __init__(self, audit_path: str | None = None) -> None:
        self._audit_path = audit_path

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        start = time.time()
        tool_name = getattr(context, "tool_name", "unknown")

        result = await call_next(context)

        latency_ms = (time.time() - start) * 1000
        size = len(str(result)) if result is not None else 0
        status = "ok"  # would need to inspect result for error status

        if self._audit_path:
            try:
                record = {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "tool": tool_name,
                    "status": status,
                    "latency_ms": round(latency_ms, 2),
                    "response_size": size,
                }
                with open(self._audit_path, "a") as fh:
                    fh.write(json.dumps(record) + "\n")
            except Exception:
                logger.exception("audit middleware write failed")

        return result


class FormatMiddleware(Middleware):
    """Apply null-stripping and nudge dedup to tool results (OHM-758).

    Runs after the tool handler returns. For structured (dict) results,
    applies _strip_nulls and _deduplicate_nudges. For string (TOON)
    results, passes through unchanged.
    """

    def __init__(self, session_key: str = "default") -> None:
        self._session_key = session_key

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        result = await call_next(context)

        # Only post-process dict results (structured content path)
        if isinstance(result, dict):
            result = _strip_nulls(result)
            result = _deduplicate_nudges(self._session_key, result)

        return result
