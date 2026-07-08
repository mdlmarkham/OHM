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
        r = await client.get(f"{_config['ohm_url']}{path}", headers=_headers(), params=params or {})
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

        if name == "ohm_stats":
            data = await _ohm_get("/stats")
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_search":
            params = {"q": arguments["q"]}
            if arguments.get("type"):
                params["type"] = arguments["type"]
            if arguments.get("created_by"):
                params["created_by"] = arguments["created_by"]
            if arguments.get("limit"):
                params["limit"] = str(arguments["limit"])
            data = await _ohm_get("/search", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_get_node":
            data = await _ohm_get(f"/node/{arguments['node_id']}")
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_neighborhood":
            params = {}
            if arguments.get("depth"):
                params["depth"] = str(arguments["depth"])
            if arguments.get("layer"):
                params["layer"] = arguments["layer"]
            data = await _ohm_get(f"/neighborhood/{arguments['node_id']}", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_listen":
            params = {"enrich": str(arguments.get("enrich", True)).lower(), "limit": str(arguments.get("limit", 50))}
            if arguments.get("since"):
                params["since"] = arguments["since"]
            if arguments.get("agent"):
                params["agent"] = arguments["agent"]
            data = await _ohm_get("/listen", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_confidence":
            data = await _ohm_get(f"/confidence/{arguments['edge_id']}")
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_path":
            data = await _ohm_get(f"/path/{arguments['from_id']}/{arguments['to_id']}")
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_agents":
            data = await _ohm_get("/agents")
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_inference":
            params = {"target": arguments["target"]}
            if arguments.get("evidence"):
                params["evidence"] = arguments["evidence"]
            if arguments.get("layers"):
                params["layers"] = arguments["layers"]
            if arguments.get("leak") is not None:
                params["leak"] = str(arguments["leak"])
            data = await _ohm_get("/inference", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_intervene":
            params = {
                "target": arguments["target"],
                "state": str(arguments["state"]),
            }
            if arguments.get("query"):
                params["query"] = arguments["query"]
            if arguments.get("layers"):
                params["layers"] = arguments["layers"]
            if arguments.get("leak") is not None:
                params["leak"] = str(arguments["leak"])
            data = await _ohm_get("/intervene", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_voi":
            params = {"decision": arguments["decision"]}
            if arguments.get("top") is not None:
                params["top"] = str(arguments["top"])
            if arguments.get("layers"):
                params["layers"] = arguments["layers"]
            if arguments.get("leak") is not None:
                params["leak"] = str(arguments["leak"])
            data = await _ohm_get("/voi", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_refute":
            params = {
                "cause": arguments["cause"],
                "effect": arguments["effect"],
            }
            if arguments.get("n_samples") is not None:
                params["n_samples"] = str(arguments["n_samples"])
            if arguments.get("methods"):
                params["methods"] = arguments["methods"]
            data = await _ohm_get("/refute", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_discover":
            params = {}
            if arguments.get("nodes"):
                params["nodes"] = arguments["nodes"]
            if arguments.get("method"):
                params["method"] = arguments["method"]
            if arguments.get("alpha") is not None:
                params["alpha"] = str(arguments["alpha"])
            if arguments.get("min_observations") is not None:
                params["min_observations"] = str(arguments["min_observations"])
            data = await _ohm_get("/discover", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_create_node":
            body: dict[str, Any] = {
                "id": arguments["id"],
                "label": arguments["label"],
                "node_type": arguments.get("node_type", "concept"),
                "confidence": arguments.get("confidence", 0.5),
                "visibility": arguments.get("visibility", "team"),
                "provenance": arguments.get("provenance", OHM_AGENT),
            }
            if arguments.get("content"):
                body["content"] = arguments["content"]
            if arguments.get("tags"):
                tags_str = arguments["tags"]
                body["tags"] = json.loads(tags_str) if isinstance(tags_str, str) else tags_str
            url = "/node"
            if not arguments.get("create_only", True):
                url += "?create_only=false"
            data = await _ohm_post(url, body)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_create_edge":
            body = {
                "from": arguments["from_node"],
                "to": arguments["to_node"],
                "type": arguments["edge_type"],
                "layer": arguments.get("layer", "L3"),
                "confidence": arguments.get("confidence", 0.5),
                "provenance": arguments.get("provenance", OHM_AGENT),
            }
            if arguments.get("condition"):
                body["condition"] = arguments["condition"]
            data = await _ohm_post("/edge", body)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_observe":
            body = {
                "node_id": arguments["node_id"],
                "obs_type": arguments["obs_type"],
                "value": arguments["value"],
                "sigma": arguments.get("sigma", 1.0),
                "source": arguments.get("source", OHM_AGENT),
            }
            if arguments.get("notes"):
                body["notes"] = arguments["notes"]
            data = await _ohm_post(f"/observe/{arguments['node_id']}", body)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_challenge":
            body = {
                "reason": arguments["reason"],
                "confidence": arguments.get("confidence", 0.5),
            }
            data = await _ohm_post(f"/challenge/{arguments['edge_id']}", body)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_support":
            body = {
                "reason": arguments["reason"],
                "confidence": arguments.get("confidence", 0.7),
            }
            data = await _ohm_post(f"/support/{arguments['edge_id']}", body)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_update_state":
            body = {"agent": OHM_AGENT}
            if arguments.get("focus"):
                body["focus"] = arguments["focus"]
            if arguments.get("patterns"):
                body["patterns"] = json.loads(arguments["patterns"]) if isinstance(arguments["patterns"], str) else arguments["patterns"]
            if arguments.get("services"):
                body["services"] = json.loads(arguments["services"]) if isinstance(arguments["services"], str) else arguments["services"]
            data = await _ohm_post("/state", body)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_list_nodes":
            params = {}
            if arguments.get("type"):
                params["type"] = arguments["type"]
            if arguments.get("label_contains"):
                params["label_contains"] = arguments["label_contains"]
            if arguments.get("created_by"):
                params["created_by"] = arguments["created_by"]
            params["limit"] = str(arguments.get("limit", 100))
            params["offset"] = str(arguments.get("offset", 0))
            data = await _ohm_get("/nodes", params)
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_domain_onboarding":
            data = await _ohm_get("/schema")
            return CallToolResult(content=_text(data, fmt))

        elif name == "ohm_list_instances":
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

        else:
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], isError=True)

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
