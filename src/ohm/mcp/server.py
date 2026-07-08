"""OHM MCP Server — expose OHM knowledge graph tools to OpenClaw agents.

Provides tiered access to OHM via MCP tools:
- Read tier: search, get_node, neighborhood, listen, stats, confidence, domain_onboarding
- Write tier: create_node, create_edge, observe, challenge, support
- Admin tier: update_node, update_edge, delete (via upsert/delete endpoints)

Configuration via environment variables:
- OHM_URL: OHM daemon URL (default: http://127.0.0.1:8710)
- OHM_TOKEN: Agent authentication token
- OHM_AGENT: Agent name (default: mcp)
- OHM_TENANT_ID: Tenant ID for multi-tenant ohmd (sent as X-Tenant-ID header)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

# ---------------------------------------------------------------------------
# Config — delegate to ohm.mcp.config (OHM-yzyk.1.2)
# ---------------------------------------------------------------------------

from ohm.mcp.encoding import (
    DEFAULT_FORMAT,
    encode_payload,
    requested_format,
)
from ohm.mcp.tools import all_tools as _all_tools
from ohm.mcp.config import config as _config, load_config_file as _load_config_file, is_tool_allowed as _is_tool_allowed, make_headers, validate_domain_config as _validate_domain_config, WRITE_TOOLS as _WRITE_TOOLS

# Backward-compat properties
OHM_URL = _config["ohm_url"]
OHM_TOKEN = _config["token"]
OHM_AGENT = _config["agent_id"]
OHM_TENANT_ID = _config["tenant_id"]

# Ensure stdout can handle Unicode (MCP stdio transport)
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return make_headers()


_ALL_TOOL_NAMES: list[str] = []


async def _ohm_get(path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as client:
        # Only pass params to httpx when there are actual parameters; passing
        # an empty dict causes httpx to strip an existing query string.
        r = await client.get(f"{_config['ohm_url']}{path}", headers=_headers(), params=params if params else None)
        r.raise_for_status()
        return r.json()


async def _ohm_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{_config['ohm_url']}{path}", headers=_headers(), json=body)
        r.raise_for_status()
        return r.json()


async def _ohm_delete(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(f"{_config['ohm_url']}{path}", headers=_headers())
        r.raise_for_status()
        return r.json()


def _text(data: Any, fmt: str = DEFAULT_FORMAT) -> list[TextContent]:
    """Format response as MCP text content.

    If the agent requested TOON, encode the payload as TOON text to reduce
    token usage; otherwise use pretty-printed JSON.
    """
    return [TextContent(type="text", text=encode_payload(data, fmt))]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = Server("ohm")


@mcp.list_tools()
async def list_tools() -> list[Tool]:
    all_tools = _all_tools()
    # OHM-yzyk.1.2: filter tools by allowed_tools and read_only
    _ALL_TOOL_NAMES.clear()
    _ALL_TOOL_NAMES.extend(t.name for t in all_tools)
    return [t for t in all_tools if _is_tool_allowed(t.name)]


@mcp.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    try:
        # OHM-yzyk.1.2: enforce allowed_tools and read_only before contacting OHM
        if not _is_tool_allowed(name):
            if _config["read_only"] and name in _WRITE_TOOLS:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "tool_blocked",
                                    "message": f"Tool '{name}' is a write-tier tool and read_only is enabled.",
                                },
                                indent=2,
                            ),
                        )
                    ],
                    isError=True,
                )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "tool_not_allowed",
                                "message": f"Tool '{name}' is not in the allowed_tools list for this MCP sidecar.",
                            },
                            indent=2,
                        ),
                    )
                ],
                isError=True,
            )

        # Response format negotiation: JSON remains default; TOON reduces tokens
        # for read-heavy results. The 'format' argument is consumed here so it
        # is not forwarded to OHM.
        fmt = requested_format(arguments)

        from ohm.mcp.dispatch import build_request

        # Local-only tool: reads the gateway host's instance registry.
        if name == "ohm_list_instances":
            registry_path = Path.home() / ".ohm" / "registry.json"
            if not registry_path.exists():
                data = {
                    "instances": [],
                    "message": "No registry found. Run 'ohm instances discover' to scan for OHM instances.",
                }
                return CallToolResult(content=_text(data, fmt))
            try:
                registry = json.loads(registry_path.read_text())
                instances = registry.get("instances", [])
                data = {"instances": instances, "count": len(instances)}
                return CallToolResult(content=_text(data, fmt))
            except Exception as e:
                data = {
                    "instances": [],
                    "error": f"Failed to read registry: {e}",
                }
                return CallToolResult(content=_text(data, fmt))

        method, path, body = build_request(name, arguments, OHM_AGENT)
        if method == "GET":
            data = await _ohm_get(path)
        else:
            data = await _ohm_post(path, body or {})
        return CallToolResult(content=_text(data, fmt))

    except httpx.HTTPStatusError as e:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"error": e.response.status_code, "detail": e.response.text}, indent=2))],
            isError=True,
        )
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")],
            isError=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _check_domain_config() -> None:
    """Validate the configured domain_config against ohmd /schema (OHM-yzyk.1.3)."""
    expected = _config.get("domain_config")
    if not expected:
        return
    try:
        schema = await _ohm_get("/schema")
    except Exception as exc:
        import logging

        logging.warning("Could not fetch /schema for domain validation: %s", exc)
        return
    if not _validate_domain_config(expected, schema):
        import sys

        actual = schema.get("schema", "<unknown>")
        print(
            f"Domain config mismatch: sidecar expects '{expected}' but daemon reports '{actual}'",
            file=sys.stderr,
        )
        sys.exit(1)


async def main():
    """Run the OHM MCP server via stdio transport."""
    await _check_domain_config()
    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream, mcp.create_initialization_options())


async def _run_verify() -> dict[str, Any]:
    """Diagnostics mode for ohm-mcp --verify.

    Connects to OHM, checks /health and /schema, lists tools, and attempts a
    harmless write probe if a write tool is allowed.
    """
    import httpx

    result: dict[str, Any] = {
        "config": {k: v for k, v in _config.items() if k not in ("token", "domain_config") and v is not None},
        "headers": _headers(),
        "health": None,
        "schema": None,
        "tools": [],
        "allowed_tools": _config.get("allowed_tools", ["*"]),
        "read_only": _config.get("read_only", False),
        "write_probe": None,
        "errors": [],
    }
    # Scrub sensitive header values for the printed report
    result["headers"] = {k: ("***" if k == "Authorization" else v) for k, v in result["headers"].items()}

    async with httpx.AsyncClient(timeout=10) as client:
        url = _config["ohm_url"]
        try:
            r = await client.get(f"{url}/health", headers=make_headers())
            r.raise_for_status()
            result["health"] = r.json()
        except Exception as e:
            result["errors"].append(f"/health failed: {e}")
            return result

        try:
            r = await client.get(f"{url}/schema", headers=make_headers())
            r.raise_for_status()
            result["schema"] = {
                "name": r.json().get("data", {}).get("schema", "?"),
            }
        except Exception as e:
            result["errors"].append(f"/schema failed: {e}")

    # Compute effective tool list using the same logic as list_tools()
    tools = await list_tools()
    result["tools"] = [t.name for t in tools]

    # Harmless write probe: try creating a node with a deterministic probe id
    if not _config.get("read_only", False) and _is_tool_allowed("ohm_create_node"):
        probe_id = f"mcp-probe-{_config['agent_id']}"
        body = {
            "label": "MCP verify probe",
            "node_type": "concept",
            "id": probe_id,
            "tags": ["mcp-verify"],
        }
        try:
            r = await _ohm_post("/node", body)
            result["write_probe"] = {"ok": True, "node_id": r.get("data", {}).get("id", r.get("id"))}
            try:
                await _ohm_delete(f"/node/{probe_id}")
                result["write_probe"]["cleanup"] = "deleted"
            except Exception as e:
                result["write_probe"]["cleanup"] = f"failed: {e}"
        except Exception as e:
            result["write_probe"] = {"ok": False, "error": str(e)}
    else:
        result["write_probe"] = {"ok": None, "reason": "read_only or ohm_create_node not allowed"}

    return result


async def _dump_tools() -> dict[str, Any]:
    """Print the raw list of tool names and whether each is allowed."""
    tools = await list_tools()
    return {
        "read_only": _config.get("read_only", False),
        "allowed_tools": _config.get("allowed_tools", ["*"]),
        "tools": [{"name": t.name, "allowed": _is_tool_allowed(t.name)} for t in tools],
        "write_tools_blocked": sorted(_WRITE_TOOLS),
    }


def cli_main():
    """Synchronous entry point for the OHM MCP server.

    Supports --config <path> to load a JSON config file (OHM-yzyk.1.2).
    Env vars continue to work as overrides/fallbacks for backward compatibility.
    """
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="OHM MCP Server")
    parser.add_argument("--config", default=None, help="Path to JSON config file")
    parser.add_argument("--verify", action="store_true", help="Run diagnostics and exit (no MCP stdio transport)")
    parser.add_argument("--dump-tools", action="store_true", help="Print tool names and allowed status, then exit")
    args = parser.parse_args()

    if args.config:
        _load_config_file(args.config)

    if args.dump_tools:
        print(json.dumps(asyncio.run(_dump_tools()), indent=2))
        return

    if args.verify:
        report = asyncio.run(_run_verify())
        print(json.dumps(report, indent=2, default=str))
        return

    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
