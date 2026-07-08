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
    all_tools = [
        # ── Read tier ──
        Tool(
            name="ohm_stats",
            description="Get OHM knowledge graph statistics: total nodes, edges, agents, observations, challenge ratio, edge types by layer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_search",
            description="Search OHM nodes by text query. Returns matching nodes with labels, content, types. Use type filter to narrow results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "q": {"type": "string", "description": "Search query text"},
                    "type": {"type": "string", "description": "Optional node type filter (concept, pattern, source, etc.)"},
                    "created_by": {"type": "string", "description": "Optional agent name filter"},
                    "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                },
                "required": ["q"],
            },
        ),
        Tool(
            name="ohm_get_node",
            description="Get a single OHM node by ID. Returns full node details including content, confidence, tags, observations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "node_id": {"type": "string", "description": "Node ID"},
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="ohm_neighborhood",
            description="Get the neighborhood around a node — edges connected to it within specified depth. Returns edge records with types, confidence, layers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "node_id": {"type": "string", "description": "Center node ID"},
                    "depth": {"type": "integer", "description": "Traversal depth (default 1)", "default": 1},
                    "layer": {"type": "string", "description": "Filter by layer (L1, L2, L3, L4)"},
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="ohm_listen",
            description="Get recent changes to the knowledge graph. Like a change feed — shows what nodes/edges were created, updated, or deleted. Use for morning briefings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "since": {"type": "string", "description": "ISO timestamp for changes since (default: 24h ago)"},
                    "agent": {"type": "string", "description": "Filter changes by agent name"},
                    "enrich": {"type": "boolean", "description": "Include change data (default true)", "default": True},
                    "limit": {"type": "integer", "description": "Max records (default 50)", "default": 50},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_confidence",
            description="Get the confidence audit trail for an edge — original confidence, challenges, supports, and current adjusted confidence.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "edge_id": {"type": "string", "description": "Edge ID to audit"},
                },
                "required": ["edge_id"],
            },
        ),
        Tool(
            name="ohm_path",
            description="Find shortest path between two nodes in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "from_id": {"type": "string", "description": "Source node ID"},
                    "to_id": {"type": "string", "description": "Target node ID"},
                },
                "required": ["from_id", "to_id"],
            },
        ),
        Tool(
            name="ohm_agents",
            description="List registered agents and their current state — focus areas, patterns, services, last sync.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
        # ── Write tier ──
        Tool(
            name="ohm_create_node",
            description="Create a new node in the OHM knowledge graph. Returns 409 Conflict if node ID already exists (use create_only=false for upsert).",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Unique node ID"},
                    "label": {"type": "string", "description": "Human-readable label"},
                    "node_type": {"type": "string", "description": "Node type (concept, pattern, source, etc.)", "default": "concept"},
                    "content": {"type": "string", "description": "Node content/description"},
                    "confidence": {"type": "number", "description": "Confidence 0.0-1.0 (default 0.5)"},
                    "provenance": {"type": "string", "description": "Where this knowledge came from"},
                    "tags": {"type": "string", "description": 'JSON array of tags, e.g. \'["economics","pattern"]\''},
                    "visibility": {"type": "string", "description": "Visibility: team (default), private, public", "default": "team"},
                    "create_only": {"type": "boolean", "description": "If true, reject on duplicate ID. If false, upsert (default true)", "default": True},
                },
                "required": ["id", "label"],
            },
        ),
        Tool(
            name="ohm_create_edge",
            description=(
                "Create an edge between two nodes. Edge types: APPLIES_TO, CAUSES, CHALLENGED_BY, "
                "COLLABORATES_WITH, CONTAINS, CORRELATES_WITH, DEFERS_TO, DELEGATED_TO, DEPENDS_ON, "
                "DERIVES_FROM, ENABLES, EXPLAINS, FEEDS, FLOWS_TO, GOALS, INFLUENCES, INTERESTED_IN, "
                "INVESTIGATED_BY, NEGATES, NOTIFIES, PART_OF, PLANS, PREDICTS, REFERENCES, REFINES, "
                "RELATED_TO, RESOLVED_BY, RISKS, SERVES, SUPPORTS, THREATENS, THREAT_CLUSTER, "
                "TRIGGERS_INCIDENT, TRUSTS, USES, VALUES, and more."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_node": {"type": "string", "description": "Source node ID"},
                    "to_node": {"type": "string", "description": "Target node ID"},
                    "edge_type": {"type": "string", "description": "Edge type (APPLIES_TO, CAUSES, REFINES, CHALLENGED_BY, SUPPORTS, etc.)"},
                    "layer": {"type": "string", "description": "Layer: L1 (structure), L2 (flow), L3 (knowledge), L4 (prospects)", "default": "L3"},
                    "confidence": {"type": "number", "description": "Confidence 0.0-1.0 (default 0.5)"},
                    "provenance": {"type": "string", "description": "Where this edge came from"},
                    "condition": {"type": "string", "description": "Optional condition/context for the edge"},
                },
                "required": ["from_node", "to_node", "edge_type"],
            },
        ),
        Tool(
            name="ohm_observe",
            description="Add an observation to a node — a data point with value, uncertainty (sigma), and source. Observations accumulate and can shift node confidence.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Node to observe"},
                    "obs_type": {"type": "string", "description": "Observation type (measurement, assessment, anomaly, support, health_check, experiment_result, sentiment, pattern, challenge)"},
                    "value": {"type": "number", "description": "Observed value"},
                    "sigma": {"type": "number", "description": "Uncertainty/standard deviation"},
                    "source": {"type": "string", "description": "Source of observation"},
                    "notes": {"type": "string", "description": "Additional notes"},
                },
                "required": ["node_id", "obs_type", "value"],
            },
        ),
        Tool(
            name="ohm_challenge",
            description="Challenge an existing edge — express disagreement with reasoning. Creates a CHALLENGED_BY edge. This is first-class disagreement in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "edge_id": {"type": "string", "description": "Edge to challenge"},
                    "reason": {"type": "string", "description": "Why you disagree"},
                    "confidence": {"type": "number", "description": "Your confidence in the challenge (0.0-1.0)", "default": 0.5},
                },
                "required": ["edge_id", "reason"],
            },
        ),
        Tool(
            name="ohm_support",
            description="Support an existing edge — express agreement with reasoning. Creates a SUPPORTS edge.",
            inputSchema={
                "type": "object",
                "properties": {
                    "edge_id": {"type": "string", "description": "Edge to support"},
                    "reason": {"type": "string", "description": "Why you agree"},
                    "confidence": {"type": "number", "description": "Your confidence in the support (0.0-1.0)", "default": 0.7},
                },
                "required": ["edge_id", "reason"],
            },
        ),
        # ── Update tier ──
        Tool(
            name="ohm_update_state",
            description="Update agent state — focus areas, active patterns, available services.",
            inputSchema={
                "type": "object",
                "properties": {
                    "focus": {"type": "string", "description": "Current focus area"},
                    "patterns": {"type": "string", "description": "JSON array of active patterns"},
                    "services": {"type": "string", "description": "JSON array of available services"},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_list_nodes",
            description="List nodes with optional filtering. Supports type, label, created_by filters and pagination.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "type": {"type": "string", "description": "Filter by node type"},
                    "label_contains": {"type": "string", "description": "Filter by label content (ILIKE)"},
                    "created_by": {"type": "string", "description": "Filter by creating agent"},
                    "limit": {"type": "integer", "description": "Max results (default 100)", "default": 100},
                    "offset": {"type": "integer", "description": "Pagination offset (default 0)", "default": 0},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_domain_onboarding",
            description="Get the OHM domain schema for this tenant: node types, edge types, layers, and domain tables. Call this when connecting to a new OHM instance to understand the active domain configuration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
    ]
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


def cli_main():
    """Synchronous entry point for the OHM MCP server.

    Supports --config <path> to load a JSON config file (OHM-yzyk.1.2).
    Env vars continue to work as overrides/fallbacks for backward compatibility.
    """
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="OHM MCP Server")
    parser.add_argument("--config", default=None, help="Path to JSON config file")
    args = parser.parse_args()

    if args.config:
        _load_config_file(args.config)

    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
