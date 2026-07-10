"""Transport-agnostic MCP tool definitions for OHM.

This module holds the Tool schemas shared by the local stdio ohm-mcp sidecar
(src/ohm/mcp/server.py) and the FastMCP ohm-gateway (src/ohm/mcp/gateway.py).
Keeping the tool definitions in one place ensures both transports expose the
same surface and that allowed_tools/read_only gating operate on identical names.
"""

from __future__ import annotations

from mcp.types import Tool


def all_tools() -> list[Tool]:
    """Return the complete, unfiltered list of OHM MCP tools."""
    return [
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
            name="ohm_refute",
            description="Causal refutation tests for a claimed cause-effect pair. Runs multiple robustness checks (random common cause, placebo, subset). Returns pass/fail per test.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cause": {"type": "string", "description": "Cause node ID"},
                    "effect": {"type": "string", "description": "Effect node ID"},
                    "n_samples": {"type": "integer", "description": "Number of bootstrap samples (default 100)", "default": 100},
                    "methods": {"type": "string", "description": "Comma-separated refutation methods to run (default: all)"},
                },
                "required": ["cause", "effect"],
            },
        ),
        Tool(
            name="ohm_belief",
            description=(
                "Get a complete belief summary for a target node: posterior probability, "
                "why the graph believes it (causal drivers), and what to observe next "
                "(value-of-information ranking). This is the deep-dive path — for ambient "
                "belief context on read tools, use include_belief=true instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Node ID to query"},
                    "evidence": {"type": "string", "description": "Evidence assignments, e.g. 'node_a:1,node_b:0.7'"},
                    "layers": {"type": "string", "description": "Comma-separated causal layers (default: L3)"},
                    "leak": {"type": "number", "description": "Leak probability for noisy-OR (default: 0.15)", "default": 0.15},
                    "format": {"type": "string", "description": "Response encoding: 'json' or 'toon'", "enum": ["json", "toon"], "default": "json"},
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="ohm_listen",
            description="Get recent changes to the knowledge graph. Like a change feed — shows what nodes/edges were created, updated, or deleted. Use for morning briefings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                    "since": {
                        "type": "string",
                        "description": (
                            "ISO timestamp for changes since. Omit for the default 24h window. "
                            "Using a very recent timestamp may miss writes due to propagation timing "
                            "— if you need the latest changes, omit this parameter rather than "
                            "passing a near-now timestamp."
                        ),
                    },
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
        # ── Inference / analysis tier ──
        Tool(
            name="ohm_inference",
            description="Run Bayesian inference on a target node given optional evidence. Returns posterior probabilities (good/bad) and supporting metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "target": {"type": "string", "description": "Target node ID to compute P(target | evidence) for"},
                    "evidence": {"type": "string", "description": "Comma-separated evidence assignments, e.g. 'node_a:1,node_b:0.7'"},
                    "layers": {"type": "string", "description": "Comma-separated layer filter, e.g. 'L1,L2,L3'"},
                    "leak": {"type": "number", "description": "Leak probability for unobserved influences (default 0.15)", "default": 0.15},
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="ohm_intervene",
            description="Causal intervention (do-operator): compute P(target | do(intervention_node=state)) given the causal graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "target": {"type": "string", "description": "Target node ID"},
                    "state": {"type": "integer", "description": "Intervention state: 0 (bad) or 1 (good)"},
                    "query": {"type": "string", "description": "Comma-separated list of nodes whose posteriors to return (default: target only)"},
                    "layers": {"type": "string", "description": "Comma-separated layer filter"},
                    "leak": {"type": "number", "description": "Leak probability (default 0.15)", "default": 0.15},
                },
                "required": ["target", "state"],
            },
        ),
        Tool(
            name="ohm_voi",
            description="Value of information ranking: given a decision node (or nodes), rank other nodes by how much observing them would improve the decision.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "decision": {"type": "string", "description": "Comma-separated decision node IDs"},
                    "top": {"type": "integer", "description": "Number of top candidates to return (default 10)", "default": 10},
                    "layers": {"type": "string", "description": "Comma-separated layer filter"},
                    "leak": {"type": "number", "description": "Leak probability (default 0.15)", "default": 0.15},
                },
                "required": ["decision"],
            },
        ),
        Tool(
            name="ohm_refute",
            description="Causal refutation tests for a claimed cause-effect pair. Runs placebo, data subset, and random-variable refutation methods and returns a refutation score.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "cause": {"type": "string", "description": "Cause node ID"},
                    "effect": {"type": "string", "description": "Effect node ID"},
                    "n_samples": {"type": "integer", "description": "Number of samples (default 1000)", "default": 1000},
                    "methods": {"type": "string", "description": "Comma-separated refutation methods (default: use all)"},
                },
                "required": ["cause", "effect"],
            },
        ),
        Tool(
            name="ohm_discover",
            description="Causal structure discovery from observation data (PC/GES algorithm). Returns candidate edges for human review.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "nodes": {"type": "string", "description": "Comma-separated node IDs to restrict discovery to"},
                    "method": {"type": "string", "description": "Algorithm: pc, ges, or both (default pc)", "enum": ["pc", "ges", "both"], "default": "pc"},
                    "alpha": {"type": "number", "description": "Significance threshold (default 0.05)", "default": 0.05},
                    "min_observations": {"type": "integer", "description": "Minimum observations per node (default 5)", "default": 5},
                },
                "required": [],
            },
        ),
        # ── Write tier ──
        Tool(
            name="ohm_create_node",
            description=(
                "Create a new node, or upsert if create_only=false. With create_only=false, "
                "omitted optional fields preserve their existing values (PATCH semantics, not PUT) "
                "— only fields present in the request are updated on an existing node. "
                "Use create_only=true (default) to reject duplicates with 409."
            ),
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
                    "create_only": {
                        "type": "boolean",
                        "description": (
                            "If true (default), reject on duplicate ID with 409. If false, upsert: "
                            "create the node if new, or partially update the existing node — only "
                            "fields present in the request are changed, omitted fields keep their "
                            "existing values (PATCH semantics)."
                        ),
                        "default": True,
                    },
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
        Tool(
            name="ohm_batch",
            description=(
                "Batch-create nodes and edges in a single all-or-nothing transaction. "
                "Max 50 combined items. Each node item needs id+label (at minimum); "
                "each edge item needs from+to+type (at minimum). Use this when you need "
                "to create multiple related nodes and edges atomically."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "nodes": {
                        "type": "array",
                        "description": "Array of node objects to create",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Unique node ID"},
                                "label": {"type": "string", "description": "Human-readable label"},
                                "type": {"type": "string", "description": "Node type (concept, pattern, source, etc.)", "default": "concept"},
                                "content": {"type": "string", "description": "Node content/description"},
                                "confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
                                "provenance": {"type": "string", "description": "Where this knowledge came from"},
                                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                            },
                            "required": ["id", "label"],
                        },
                        "maxItems": 50,
                    },
                    "edges": {
                        "type": "array",
                        "description": "Array of edge objects to create",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string", "description": "Source node ID"},
                                "to": {"type": "string", "description": "Target node ID"},
                                "type": {"type": "string", "description": "Edge type (CAUSES, SUPPORTS, etc.)"},
                                "layer": {"type": "string", "description": "Layer: L1-L4", "default": "L3"},
                                "confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
                                "provenance": {"type": "string", "description": "Where this edge came from"},
                            },
                            "required": ["from", "to", "type"],
                        },
                        "maxItems": 50,
                    },
                },
                "required": [],
            },
        ),
        # ── Update / utility tier ──
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
        Tool(
            name="ohm_list_profiles",
            description="List the OHM instance profiles configured for this sidecar and show which profile is currently active. Each profile may point to a different OHM tenant or instance. Use ohm_select_profile to switch.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_select_profile",
            description="Switch the active OHM profile by name. Subsequent tool calls will use the selected profile's URL, token, tenant, and tool policy. Returns the active profile summary.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Profile name to activate (from ohm_list_profiles)"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="ohm_list_instances",
            description="List discovered OHM instances and their health status from the local registry (~/.ohm/registry.json). Run 'ohm instances discover' first to populate the registry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'. TOON reduces token usage for large result sets.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
    ]
