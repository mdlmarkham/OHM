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
                "full percentiles, prior vs posterior surprise, evidence movers, "
                "belief calibration, and what to observe next "
                "(value-of-information ranking). This is the deep-dive path — for ambient "
                "belief context on read tools, use include_belief=true instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Node ID to query"},
                    "evidence": {"type": "string", "description": "Evidence assignments, e.g. 'node_a:1,node_b:0.7'"},
                    "layers": {"type": "string", "description": "Comma-separated causal layers (default: L3)"},
                    "edge_types": {"type": "string", "description": "Comma-separated edge types for Bayesian network (default: inference edge types)"},
                    "leak": {"type": "number", "description": "Leak probability for noisy-OR (default: 0.15)", "default": 0.15},
                    "include_evidence_movers": {"type": "boolean", "description": "Include which observations moved belief, by how much (default: true)", "default": True},
                    "include_prior": {"type": "boolean", "description": "Include prior distribution and KL surprise (default: true)", "default": True},
                    "belief_statement": {"type": "string", "description": "Optional agent-stated belief to calibrate, e.g. 'P(bad)=0.5' or 'likely'"},
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
            name="ohm_decision_recommend",
            description="Get the recommendation for a decision node: current best action, action alternatives, confidence, key assumptions, and utility scale.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Decision node ID"},
                },
                "required": ["node_id"],
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
        Tool(
            name="ohm_pert",
            description=(
                "Get PERT (Program Evaluation and Review Technique) estimates for a target node. "
                "Returns optimistic (p05), most-likely (p50), and pessimistic (p95) estimates "
                "based on the node's edges and observations. Use this when you need a range, "
                "not a point estimate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target node ID"},
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'", "enum": ["json", "toon"], "default": "json"},
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="ohm_monte_carlo",
            description=("Run a Monte Carlo simulation on the causal graph starting from a target node. Returns outcome distribution, worst-case probability, and confidence intervals. Use this for risk assessment and scenario analysis."),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target node ID to simulate from"},
                    "n_simulations": {"type": "integer", "description": "Number of Monte Carlo iterations (default 1000)", "default": 1000},
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'", "enum": ["json", "toon"], "default": "json"},
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="ohm_markov",
            description=("Run Markov chain analysis on the causal graph. Returns absorbing-state probabilities and expected steps to absorption. Use this for forecasting where the system is heading over time."),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target node ID for Markov analysis"},
                    "analysis": {"type": "string", "description": "Analysis type: 'absorbing' (probability of reaching each state) or 'expected_steps' (time to reach absorbing states)", "enum": ["absorbing", "expected_steps"], "default": "absorbing"},
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'", "enum": ["json", "toon"], "default": "json"},
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="ohm_game",
            description=("Run game-theoretic analysis on decision nodes. Returns Nash equilibria, dominant strategies, and payoff analysis. Use this when agents have competing interests and you need to find stable outcomes."),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Decision node ID to analyze"},
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'", "enum": ["json", "toon"], "default": "json"},
                },
                "required": ["target"],
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
                    "belief_statement": {
                        "type": "string",
                        "description": ("Optional: your belief about the target node's state, e.g. 'P(target=bad) = 0.3'. OHM compares this to its graph posterior and logs the comparison for calibration scoring."),
                    },
                },
                "required": ["from_node", "to_node", "edge_type"],
            },
        ),
        Tool(
            name="ohm_observe",
            description="Add an observation to a node - a data point with value, uncertainty (sigma), and source. Observations accumulate and can shift node confidence.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Node to observe"},
                    "obs_type": {"type": "string", "description": "Observation type (measurement, assessment, anomaly, support, health_check, experiment_result, sentiment, pattern, challenge)"},
                    "value": {"type": "number", "description": "Observed value"},
                    "sigma": {"type": "number", "description": "Uncertainty/standard deviation"},
                    "source": {"type": "string", "description": "Source of observation"},
                    "notes": {"type": "string", "description": "Additional notes"},
                    "idempotency_key": {"type": "string", "description": "Optional: dedup key for retry-safe emission (e.g. 'run_42:health_check'). Repeat writes with the same key are no-ops."},
                    "belief_statement": {
                        "type": "string",
                        "description": "Optional: your belief about the observed node's state, e.g. 'P(node=bad) = 0.3'. Compared to graph posterior for calibration.",
                    },
                },
                "required": ["node_id", "obs_type", "value"],
            },
        ),
        Tool(
            name="ohm_observe_batch",
            description=(
                "Bulk-emit observations against existing nodes. Wraps the /observations "
                "HTTP endpoint — up to 1000 observations per call. Pass an idempotency_key "
                "on each observation for retry-safe emission (duplicate keys are no-ops). "
                "Use this for CI runs, pipeline orchestrators, health monitors, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "observations": {
                        "type": "array",
                        "description": "Array of observation objects",
                        "items": {
                            "type": "object",
                            "properties": {
                                "node_id": {"type": "string", "description": "Node to observe"},
                                "obs_type": {"type": "string", "description": "Observation type"},
                                "value": {"type": "number", "description": "Observed value"},
                                "sigma": {"type": "number", "description": "Uncertainty"},
                                "source": {"type": "string", "description": "Source of observation"},
                                "notes": {"type": "string", "description": "Additional notes"},
                                "idempotency_key": {"type": "string", "description": "Dedup key for retry-safe emission"},
                                "source_url": {"type": "string", "description": "Source URL"},
                                "scale": {"type": "string", "description": "Measurement scale: probability, count, currency, percent, unknown"},
                            },
                            "required": ["node_id", "obs_type", "value"],
                        },
                        "maxItems": 1000,
                    },
                },
                "required": ["observations"],
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
                    "belief_statement": {
                        "type": "string",
                        "description": "Optional: your belief about the challenged edge's target, e.g. 'P(target=bad) = 0.3'. Compared to graph posterior for calibration.",
                    },
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
                    "belief_statement": {
                        "type": "string",
                        "description": "Optional: your belief about the supported edge's target, e.g. 'P(target=bad) = 0.3'. Compared to graph posterior for calibration.",
                    },
                },
                "required": ["edge_id", "reason"],
            },
        ),
        Tool(
            name="ohm_batch",
            description=(
                "Batch-create nodes and edges in a single all-or-nothing transaction. "
                "Max 500 combined items. Each node item needs id+label (at minimum); "
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
                                "visibility": {"type": "string", "description": "Visibility scope: team (default), private, public", "default": "team"},
                            },
                            "required": ["id", "label"],
                        },
                        "maxItems": 500,
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
                                "condition": {"type": "string", "description": "Optional condition under which the edge holds"},
                            },
                            "required": ["from", "to", "type"],
                        },
                        "maxItems": 500,
                    },
                    "belief_statement": {
                        "type": "string",
                        "description": "Optional: your belief about the primary target of this batch, e.g. 'P(target=bad) = 0.3'. Compared to graph posterior for calibration.",
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
            name="ohm_backend_status",
            description=(
                "Get OHM backend metadata for introspection: store type (local_duckdb, "
                "ducklake_catalog, or memory), db path, schema version, pending migrations, "
                "graph size (nodes, edges, observations, fragments), storage bytes, write mode, "
                "tenant id, agent profile, DuckLake sync status, and daemon uptime. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_storage_efficiency",
            description=(
                "Get OHM storage health signals: soft-deleted row estimate (nodes + edges), "
                "fragment ratio (L0 fragment nodes / active nodes), orphan rate, embedding "
                "coverage, and a heuristic compaction recommendation. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_source_reliability",
            description=(
                "Get per-agent source reliability metrics computed from "
                "historical outcomes: P(accurate), false_positive_rate, "
                "outcome count, and last outcome timestamp. When agent_id "
                "is omitted, returns the calling agent's own reliability. "
                "When agent_id names a different agent, the result is "
                "anonymized by default (pseudonymised agent_id) — named peer "
                "scores require tenant opt-in. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent to evaluate. Omit for the calling agent's own reliability."},
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_my_calibration",
            description=(
                "Get the calling agent's calibration profile: Brier score, "
                "overconfidence rate, novelty score, loop risk, and prediction "
                "count. Always reflects the caller — no agent_id parameter. "
                "Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
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
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                },
                "required": [],
            },
        ),
        # ── Conversation state tier (OHM-789) ──
        Tool(
            name="ohm_conversation",
            description=(
                "Query or update the per-thread conversation state: active topics, "
                "open deliberations, nudge history, agent contributions, and pending "
                "Socratic questions. Pass thread_id to scope; pass updates to modify. "
                "With no arguments, returns the current thread's state."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "thread_id": {"type": "string", "description": "Thread/conversation ID. Defaults to the MCP session ID."},
                    "action": {
                        "type": "string",
                        "enum": ["get", "update", "evict", "answer_question"],
                        "description": "Action: 'get' (default) returns state, 'update' merges fields, 'evict' clears thread, 'answer_question' marks a pending question answered.",
                    },
                    "updates": {
                        "type": "object",
                        "description": "Fields to merge when action=update: participants, topics, deliberations, nudge_history, agent_contributions, pending_questions, thread_entropy, needs_attention.",
                    },
                    "question_text": {
                        "type": "string",
                        "description": "Question text to mark as answered (action=answer_question).",
                    },
                },
                "required": [],
            },
        ),
        # ── Deliberation lifecycle tier (OHM-793) ──
        Tool(
            name="ohm_deliberation",
            description=(
                "Deliberation lifecycle: propose a claim, challenge it, submit evidence, "
                "check if decision thresholds are met, or resolve. Lifecycle: "
                "PROPOSE → CHALLENGE → EVIDENCE → SYNTHESIZE → DECIDE → RESOLVE. "
                "Uses conversation state for tracking; thresholds scale with blast radius."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "action": {
                        "type": "string",
                        "enum": ["propose", "challenge", "evidence", "check", "resolve", "get"],
                        "description": "Deliberation action to perform.",
                    },
                    "node_id": {"type": "string", "description": "Target node ID for the deliberation."},
                    "target": {"type": "string", "description": "Alternative to node_id."},
                    "claim_text": {"type": "string", "description": "Claim text (action=propose)."},
                    "confidence": {"type": "number", "description": "Confidence level 0-1 (action=propose/challenge)."},
                    "reason": {"type": "string", "description": "Challenge reason (action=challenge)."},
                    "evidence_type": {"type": "string", "description": "Evidence type (action=evidence)."},
                    "evidence_summary": {"type": "string", "description": "Evidence summary (action=evidence)."},
                    "blast_radius": {"type": "string", "enum": ["normal", "high"], "description": "Blast radius for threshold scaling (action=check)."},
                    "outcome": {"type": "string", "description": "Resolution outcome (action=resolve)."},
                    "notes": {"type": "string", "description": "Resolution notes (action=resolve)."},
                    "belief_data": {"type": "object", "description": "Belief data for threshold checking (action=check)."},
                },
                "required": ["action"],
            },
        ),
        # ── Instance bootstrap tier (OHM-797) ──
        Tool(
            name="ohm_bootstrap",
            description=(
                "Guided bootstrap interview for a fresh OHM instance with no domain configured. "
                "Admin-only. Call with action=get to see the current step prompt, action=answer "
                "to submit an answer, action=abandon to clear corrupted state, or "
                "action=from_template to load a named domain template directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "action": {
                        "type": "string",
                        "enum": ["get", "answer", "abandon", "from_template"],
                        "description": "Bootstrap action: get current step, submit answer, abandon, or load template.",
                        "default": "get",
                    },
                    "answer": {"type": "string", "description": "Answer text (action=answer)."},
                    "template_name": {"type": "string", "description": "Domain template name (action=from_template, e.g. 'ohm', 'topo', 'beef_herd')."},
                    "templates_dir": {"type": "string", "description": "Optional: operator-supplied template directory path."},
                },
                "required": [],
            },
        ),
        # ── Agent onboarding tier (OHM-798) ──
        Tool(
            name="ohm_onboard",
            description=(
                "Guided agent onboarding to the OHM agora. 6-step flow: capability discovery, "
                "identity registration, vocabulary orientation, calibration baseline (practice), "
                "practice deliberation, and first real contribution. Practice steps do NOT "
                "count toward real calibration. Call with action=get to see current step."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "action": {
                        "type": "string",
                        "enum": ["get", "calibration_prediction", "practice_deliberation"],
                        "description": "Onboarding action: get current step, record practice prediction, or record practice deliberation.",
                        "default": "get",
                    },
                    "target_node": {"type": "string", "description": "Target node for practice predictions/deliberations."},
                    "predicted_probability": {"type": "number", "description": "Predicted probability for calibration baseline (action=calibration_prediction)."},
                    "deliberation_action": {"type": "string", "description": "Practice deliberation action (action=practice_deliberation)."},
                },
                "required": [],
            },
        ),
        # ── Prospect lifecycle tier (OHM-844) ──
        Tool(
            name="ohm_prospect_create",
            description=(
                "Create a prospect node — a governed plan of action with lifecycle "
                "accountability. Starts in 'proposed' status. Returns the created node."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "label": {"type": "string", "description": "Human-readable prospect description."},
                    "authority": {"type": "string", "description": "Agent authorized to transition this prospect (plain field check)."},
                    "parent_scenario_id": {"type": "string", "description": "Optional scenario this prospect derives from."},
                    "planned_start": {"type": "string", "description": "ISO 8601 planned start date."},
                    "planned_end": {"type": "string", "description": "ISO 8601 planned end date."},
                    "horizon_label": {"type": "string", "description": "Human-readable horizon (e.g. 'Q3 2026')."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for scope filtering."},
                    "content": {"type": "string", "description": "Optional description or rationale."},
                    "connects_to": {"type": "array", "items": {"type": "string"}, "description": "Node IDs to cross-link (ADR-018)."},
                    "confidence": {"type": "number", "description": "Initial confidence 0-1 (default 1.0).", "default": 1.0},
                },
                "required": ["label"],
            },
        ),
        Tool(
            name="ohm_prospect_transition",
            description=(
                "Transition a prospect to a new lifecycle status. Validates legal "
                "transitions and checks authority (agent must match prospect's authority "
                "field). Creates an assessment observation logging the transition."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "prospect_id": {"type": "string", "description": "Prospect node ID."},
                    "new_status": {"type": "string", "enum": ["committed", "active", "completed", "failed", "superseded"], "description": "Target lifecycle status."},
                    "reason": {"type": "string", "description": "Optional explanation for the transition."},
                },
                "required": ["prospect_id", "new_status"],
            },
        ),
        Tool(
            name="ohm_prospect_list",
            description=(
                "List prospects with optional status, tag, and creator filters. "
                "Includes expectation counts via aggregate query."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "status": {"type": "string", "enum": ["proposed", "committed", "active", "completed", "failed", "superseded"], "description": "Filter by lifecycle status."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "AND-semantics tag filter (all must be present)."},
                    "created_by": {"type": "string", "description": "Filter by creating agent."},
                    "limit": {"type": "integer", "description": "Max results (default 20).", "default": 20},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_prospect_detail",
            description=(
                "Get full prospect detail: the prospect node, all CONTAINS children "
                "(e.g. expectations), and the latest observation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "prospect_id": {"type": "string", "description": "Prospect node ID."},
                },
                "required": ["prospect_id"],
            },
        ),
        # ── Monte Carlo prospect simulation (OHM-843) ──────────────────────────
        # Distinct from ohm_pert (single-node three-point estimate) and
        # ohm_monte_carlo (graph cascade/failure propagation). This tool
        # aggregates multi-expectation prospect outcomes using Beta-PERT
        # sampling per expectation, with sensitivity ranking and VoI
        # cross-validation.
        Tool(
            name="ohm_simulate",
            description=(
                "Run Monte Carlo simulation over a prospect's expectation nodes. "
                "Samples from Beta-PERT distributions per expectation (p10/p50/p90), "
                "computes per-expectation statistics, sensitivity rankings, and "
                "cross-validates against compute_voi's ranking via Spearman rank "
                "correlation. Persists result as an experiment_result observation. "
                "Distinct from ohm_pert (single-node estimate) and ohm_monte_carlo "
                "(graph cascade/failure propagation)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "prospect_id": {"type": "string", "description": "The prospect node to simulate."},
                    "n_iterations": {"type": "integer", "description": "Number of Monte Carlo iterations (default 5000).", "default": 5000},
                    "seed": {"type": "integer", "description": "Random seed for reproducibility."},
                },
                "required": ["prospect_id"],
            },
        ),
        # ── Skill maintenance loop (OHM-854) ──────────────────────────────
        Tool(
            name="ohm_skill_maintenance",
            description=(
                "Run one skill maintenance round. Detects signals (low nudge "
                "acceptance rates), generates candidate skill edits, evaluates "
                "them via Fisher's exact test, and promotes or demotes. Use "
                "dry_run=true to preview without modifying skills."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "dry_run": {"type": "boolean", "description": "If true, generate and evaluate candidates but don't promote.", "default": False},
                },
                "required": [],
            },
        ),
        # ── Temporal Planning (OHM-937) ───────────────────────────────────
        Tool(
            name="ohm_plan_create",
            description="Create a new temporal plan (OHM-937). Plans track multi-event initiatives with a type, horizon, and status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "plan_type": {"type": "string", "description": "Plan type (e.g. 'operational', 'strategic', 'maintenance')."},
                    "plan_id": {"type": "string", "description": "Optional explicit plan ID (auto-generated if omitted)."},
                    "node_id": {"type": "string", "description": "Node this plan is linked to."},
                    "label": {"type": "string", "description": "Human-readable label."},
                    "start_ts": {"type": "string", "description": "ISO 8601 start timestamp."},
                    "end_ts": {"type": "string", "description": "ISO 8601 end timestamp."},
                    "horizon": {"type": "string", "description": "Planning horizon (e.g. '30d', '90d')."},
                    "status": {"type": "string", "description": "Plan status.", "default": "active"},
                    "metadata": {"type": "object", "description": "Arbitrary JSON metadata."},
                },
                "required": ["plan_type"],
            },
        ),
        Tool(
            name="ohm_event_create",
            description="Create a temporal event linked to a plan (OHM-937). Events represent milestones or incidents within a plan's timeline.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "node_id": {"type": "string", "description": "Node this event is associated with."},
                    "event_class": {"type": "string", "description": "Event class (e.g. 'incident', 'milestone', 'assessment')."},
                    "start_ts": {"type": "string", "description": "ISO 8601 start timestamp."},
                    "end_ts": {"type": "string", "description": "ISO 8601 end timestamp."},
                    "title": {"type": "string", "description": "Event title."},
                    "plan_id": {"type": "string", "description": "Plan this event belongs to."},
                    "horizon": {"type": "string", "description": "Event horizon."},
                    "operating_state": {"type": "string", "description": "Operating state during event."},
                    "description": {"type": "string", "description": "Free-text description."},
                    "confidence": {"type": "number", "description": "Confidence score 0-1."},
                    "authority": {"type": "string", "description": "Authority source."},
                    "metadata": {"type": "object", "description": "Arbitrary JSON metadata."},
                },
                "required": ["node_id", "event_class", "start_ts"],
            },
        ),
        Tool(
            name="ohm_event_link",
            description="Create a directed link between two events (OHM-937). Used to build temporal dependency graphs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "from_event_id": {"type": "string", "description": "Source event ID."},
                    "to_event_id": {"type": "string", "description": "Target event ID."},
                    "edge_type": {"type": "string", "description": "Edge type (e.g. 'CAUSES', 'FOLLOWS', 'BLOCKS')."},
                    "layer": {"type": "string", "description": "Layer (default 'L1')."},
                    "confidence": {"type": "number", "description": "Confidence 0-1 (default 1.0)."},
                    "metadata": {"type": "object", "description": "Arbitrary JSON metadata."},
                },
                "required": ["from_event_id", "to_event_id", "edge_type"],
            },
        ),
        Tool(
            name="ohm_report_create",
            description="Create a temporal report (OHM-937). Reports summarise findings, recommendations, and confidence adjustments for a plan or node.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "report_type": {"type": "string", "description": "Report type (e.g. 'status', 'incident', 'assessment')."},
                    "report_id": {"type": "string", "description": "Optional explicit report ID."},
                    "node_id": {"type": "string", "description": "Node this report is about."},
                    "plan_id": {"type": "string", "description": "Plan this report belongs to."},
                    "title": {"type": "string", "description": "Report title."},
                    "summary": {"type": "string", "description": "Executive summary."},
                    "findings": {"type": "object", "description": "Structured findings."},
                    "recommendations": {"type": "object", "description": "Structured recommendations."},
                    "confidence_adjustments": {"type": "object", "description": "Confidence adjustments by edge."},
                    "status": {"type": "string", "description": "Report status.", "default": "draft"},
                    "metadata": {"type": "object", "description": "Arbitrary JSON metadata."},
                },
                "required": ["report_type"],
            },
        ),
        Tool(
            name="ohm_report_finalize",
            description="Finalize a report and apply its confidence adjustments (OHM-937).",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "report_id": {"type": "string", "description": "Report to finalize."},
                    "confidence_adjustments": {"type": "object", "description": "Override confidence adjustments."},
                },
                "required": ["report_id"],
            },
        ),
        Tool(
            name="ohm_run_create",
            description="Create a data-product run record (OHM-937). Runs track execution of a computation or pipeline with inputs and status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "run_type": {"type": "string", "description": "Run type (e.g. 'cascade', 'calibration', 'ingestion')."},
                    "run_id": {"type": "string", "description": "Optional explicit run ID."},
                    "report_id": {"type": "string", "description": "Report this run produced."},
                    "node_id": {"type": "string", "description": "Node this run is associated with."},
                    "inputs": {"type": "object", "description": "Input parameters."},
                    "status": {"type": "string", "description": "Run status.", "default": "pending"},
                    "metadata": {"type": "object", "description": "Arbitrary JSON metadata."},
                },
                "required": ["run_type"],
            },
        ),
        Tool(
            name="ohm_run_complete",
            description="Mark a run as completed and record outputs (OHM-937).",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "run_id": {"type": "string", "description": "Run to complete."},
                    "status": {"type": "string", "description": "Final status (default 'completed')."},
                    "outputs": {"type": "object", "description": "Output data."},
                    "error": {"type": "string", "description": "Error message if failed."},
                    "duration_ms": {"type": "integer", "description": "Execution duration in milliseconds."},
                },
                "required": ["run_id"],
            },
        ),
        Tool(
            name="ohm_rul_register",
            description="Register a Remaining Useful Life (RUL) assessment for an equipment node (OHM-937).",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "equipment_node_id": {"type": "string", "description": "Equipment node ID."},
                    "rul_days": {"type": "number", "description": "Estimated remaining useful life in days."},
                    "risk_class": {"type": "string", "description": "Risk classification."},
                    "model_version": {"type": "string", "description": "Model version used."},
                    "site_id": {"type": "string", "description": "Site identifier."},
                    "node_path": {"type": "string", "description": "Node path in hierarchy."},
                    "metadata": {"type": "object", "description": "Arbitrary JSON metadata."},
                },
                "required": ["equipment_node_id", "rul_days", "risk_class"],
            },
        ),
        Tool(
            name="ohm_scenario_run",
            description="Run a counterfactual scenario analysis with optional persistence (OHM-937). Chains query_compare_scenarios (when compare=true) or query_counterfactual_cascade, and optionally persists a scenario node.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "node_id": {"type": "string", "description": "Target node for scenario analysis."},
                    "failure_probability": {"type": "number", "description": "Failure probability (default 1.0).", "default": 1.0},
                    "max_depth": {"type": "integer", "description": "Max cascade depth (default 10).", "default": 10},
                    "edge_overrides": {"type": "object", "description": "Edge probability overrides."},
                    "node_interventions": {"type": "object", "description": "Node-level interventions."},
                    "disabled_edges": {"type": "array", "items": {"type": "string"}, "description": "Edge IDs to disable."},
                    "disabled_nodes": {"type": "array", "items": {"type": "string"}, "description": "Node IDs to disable."},
                    "compare": {"type": "boolean", "description": "Use compare_scenarios (default true).", "default": True},
                    "persist": {"type": "boolean", "description": "Persist scenario as a node (default false).", "default": False},
                    "label": {"type": "string", "description": "Label for persisted scenario node."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for persisted scenario node."},
                },
                "required": ["node_id"],
            },
        ),
        Tool(
            name="ohm_scenarios",
            description="List persisted scenario nodes, optionally filtered by target node (OHM-937).",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "target_node_id": {"type": "string", "description": "Filter by target node via SCENARIO_FOR edge."},
                    "limit": {"type": "integer", "description": "Max results (default 50).", "default": 50},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_verifiable_claims",
            description="List verifiable claims that are overdue for outcome recording (OHM-937).",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "agent": {"type": "string", "description": "Filter by source agent."},
                    "days_threshold": {"type": "integer", "description": "Days since creation (default 14).", "default": 14},
                    "confidence_threshold": {"type": "number", "description": "Min confidence (default 0.85).", "default": 0.85},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_record_verification_outcome",
            description="Record a verification outcome for a CAUSES/PREDICTS edge (OHM-937). Renamed from ohm_record_outcome to avoid ambiguity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "edge_id": {"type": "string", "description": "Edge to record outcome for."},
                    "outcome": {"type": "boolean", "description": "True if claim was validated, false if falsified."},
                    "reason": {"type": "string", "description": "Explanation of the outcome."},
                },
                "required": ["edge_id", "outcome"],
            },
        ),
        Tool(
            name="ohm_drifts",
            description="List drift observations — plan-vs-actual deviations (OHM-937).",
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "description": "Response encoding: 'json' (default) or 'toon'.", "enum": ["json", "toon"], "default": "json"},
                    "plan_id": {"type": "string", "description": "Filter by plan node ID."},
                    "drift_type": {"type": "string", "description": "Filter by drift type."},
                    "severity": {"type": "string", "description": "Filter by severity level."},
                    "limit": {"type": "integer", "description": "Max results (default 50).", "default": 50},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_reconcile",
            description="Run plans-vs-actuals reconciliation: compare planned temporal artifacts against actual events and surface drift as observations with DRIFT_FROM edges (OHM-940).",
            inputSchema={
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string", "description": "Plan ID to reconcile. If omitted, all active plans are checked."},
                    "horizon": {"type": "string", "description": "Optional horizon filter."},
                    "dry_run": {"type": "boolean", "description": "If true, return drift records without writing observations or edges.", "default": False},
                    "tolerance": {"type": "object", "description": "Optional tolerance overrides: timing_seconds, value, duration_seconds."},
                    "created_by": {"type": "string", "description": "Agent name for drift attribution (defaults to caller)."},
                },
                "required": [],
            },
        ),
        Tool(
            name="ohm_drift_explain",
            description="Explain a drift observation: run Value-of-Information analysis to show which assumptions/edges would change the plan outcome (OHM-940).",
            inputSchema={
                "type": "object",
                "properties": {
                    "drift_id": {"type": "string", "description": "Drift observation ID to explain."},
                    "top": {"type": "integer", "description": "Number of VoI candidates to return (default 10).", "default": 10},
                },
                "required": ["drift_id"],
            },
        ),
    ]
