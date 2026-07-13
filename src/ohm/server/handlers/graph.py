"""Graph handler mixin — node/edge CRUD, search, observations, webhooks, and agent state."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase

import logging
import time
from typing import Any

from ohm.server import suggestions as _suggestions_module

logger = logging.getLogger(__name__)

from ohm.framework.exceptions import NodeNotFoundError, AuthenticationError
from ohm.server import server as _server_module
from ohm.server.nudges import generate_nudges, enrich_response


def _resolve_type_field(body: dict, *aliases: str, default: str | None = None) -> str | None:
    """Resolve a type-like field from an HTTP body, accepting multiple aliases.

    Per OHM-0abu (live daemon review, 2026-06-30): the HTTP body uses the
    generic key ``type`` for node/edge/observation types, which collides with
    natural-language naming — clients reasonably send ``node_type``,
    ``edge_type``, or ``obs_type`` instead. Accept all aliases for backward
    compatibility. The first non-empty value in priority order wins; the
    descriptive name (``node_type``/``edge_type``/``obs_type``) should be
    listed first when called. Empty string is treated as missing.
    """
    for key in aliases:
        value = body.get(key)
        if value is not None and value != "":
            return value
    return default


class GraphHandlerMixin(OhmHandlerBase):
    """Handler mixin for graph CRUD endpoints (OHM-hpxa).

    Methods migrated from server.py: 38 handler methods covering node/edge
    read/write/delete, search, observations, agent registration, webhooks,
    and batch operations.
    """

    _challenge_ratio_cache: float = 0.0
    _challenge_ratio_cache_time: float = 0.0

    def _get_challenge_ratio(self) -> float:
        """Get the current graph challenge ratio, cached for 5 minutes."""
        import time

        now = time.time()
        if now - self._challenge_ratio_cache_time > 300:  # 5-minute cache
            try:
                row = self.current_store.conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type = 'CHALLENGED_BY' AND deleted_at IS NULL").fetchone()
                challenged = row[0] if row else 0
                row2 = self.current_store.conn.execute("SELECT COUNT(*) FROM edges WHERE layer = 'L3' AND deleted_at IS NULL").fetchone()
                total_l3 = row2[0] if row2 else 1
                ratio = challenged / max(total_l3, 1)
                GraphHandlerMixin._challenge_ratio_cache = ratio
                GraphHandlerMixin._challenge_ratio_cache_time = now
            except Exception:
                ratio = GraphHandlerMixin._challenge_ratio_cache
        else:
            ratio = GraphHandlerMixin._challenge_ratio_cache
        return ratio

    def _run_pre_ingest_hooks(
        self,
        agent: str,
        action: str,
        body: dict,
        batch_edges: list[dict] | None = None,
        batch_node_ids: set[str] | None = None,
    ) -> dict | None:
        """Run pre_ingest hooks. Return error dict if any hook rejects, else None.

        When ``batch_edges`` is provided, it is forwarded to hooks so they can
        implement ADR-018 option 2 (accept a node when an edge in the same batch
        references it). ``batch_node_ids`` is the set of node ids being created
        in the same batch, so hooks can verify the edge's counterpart exists or
        is being co-created.
        """
        from ohm.hooks import HookRunner

        runner = HookRunner(self.current_store.conn)
        payload: dict[str, Any] = {"agent": agent, "action": action, "body": body}
        if batch_edges is not None:
            payload["batch_edges"] = batch_edges
        if batch_node_ids is not None:
            payload["batch_node_ids"] = batch_node_ids
        results = runner.run_hooks("pre_ingest", payload)
        for r in results:
            if not r.success:
                return {
                    "error": "hook_rejected",
                    "hook_id": r.hook_id,
                    "exit_code": r.exit_code,
                    "message": r.stderr or "Hook rejected the operation",
                    "timed_out": r.timed_out,
                }
        return None

    def _run_post_ingest_hooks(self, agent: str, action: str, result: dict) -> dict:
        """Run post_ingest hooks. Return hook_decorations dict if any hook provides JSON stdout."""
        import json

        from ohm.hooks import HookRunner

        runner = HookRunner(self.current_store.conn)
        results = runner.run_hooks("post_ingest", {"agent": agent, "action": action, "result": result})
        decorations = {}
        for r in results:
            if r.success and r.stdout.strip():
                try:
                    merge = json.loads(r.stdout.strip())
                    if isinstance(merge, dict):
                        decorations.update(merge)
                except json.JSONDecodeError:
                    pass
            elif not r.success:
                import logging

                logging.getLogger(__name__).warning(
                    "post_ingest hook %s failed (exit_code=%d): %s",
                    r.hook_id,
                    r.exit_code,
                    r.stderr,
                )
        return decorations

    def _get_fragments(self, path: str, qs: dict) -> None:
        """GET /fragments — query L0 fragment nodes (OHM-a5rz.10).

        Filters: ?agent=, ?since=, ?until=, ?q= (text search), ?limit=,
        ?open_questions=true (fragments with is_question=true in metadata).
        Returns fragment nodes with their L0 context edges.
        """
        agent = qs.get("agent", [None])[0]
        since = qs.get("since", [None])[0]
        until = qs.get("until", [None])[0]
        query = qs.get("q", [None])[0]
        open_questions = qs.get("open_questions", [None])[0]
        limit = int(qs.get("limit", [50])[0])

        conditions = ["type = 'fragment'", "deleted_at IS NULL"]
        params: list = []
        if agent:
            conditions.append("created_by = ?")
            params.append(agent)
        if since:
            conditions.append("created_at >= ?::TIMESTAMP")
            params.append(since)
        if until:
            conditions.append("created_at <= ?::TIMESTAMP")
            params.append(until)
        if query:
            conditions.append("(label ILIKE ? OR content ILIKE ?)")
            params.append(f"%{query}%")
            params.append(f"%{query}%")
        if open_questions and open_questions.lower() in ("true", "1", "yes"):
            conditions.append("json_extract(metadata, '$.is_question') = true")
        resonance = qs.get("resonance", [None])[0]
        resonance = resonance and resonance.lower() in ("true", "1", "yes")
        clusters = qs.get("clusters", [None])[0]
        clusters = clusters and clusters.lower() in ("true", "1", "yes")

        params.append(limit)

        where = " AND ".join(conditions)
        nodes = self.current_store.execute(
            f"SELECT * FROM ohm_nodes WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        node_ids = [n["id"] for n in nodes]
        edges: list = []
        if node_ids:
            placeholders = ",".join(["?"] * len(node_ids))
            edges = self.current_store.execute(
                f"SELECT * FROM ohm_edges WHERE layer = 'L0' AND (from_node IN ({placeholders}) OR to_node IN ({placeholders})) AND deleted_at IS NULL",
                node_ids + node_ids,
            )

        response = {"fragments": nodes, "edges": edges, "count": len(nodes)}

        # OHM-a5rz.25: resonance=true adds resonance count per fragment
        if resonance and node_ids:
            placeholders = ",".join(["?"] * len(node_ids))
            resonance_counts = self.current_store.execute(
                f"""SELECT e.from_node AS fragment_id, COUNT(DISTINCT e.to_node) AS resonance_count
                    FROM ohm_edges e
                    WHERE e.edge_type = 'RESONANCE' AND e.deleted_at IS NULL
                      AND e.from_node IN ({placeholders})
                    GROUP BY e.from_node
                """,
                node_ids,
            )
            resonance_map = {r["fragment_id"]: r["resonance_count"] for r in resonance_counts}
            for node in nodes:
                node["resonance_count"] = resonance_map.get(node["id"], 0)
            # Sort by resonance_count descending
            nodes.sort(key=lambda n: n.get("resonance_count", 0), reverse=True)
            response["fragments"] = nodes

        # OHM-a5rz.28: clusters=true returns fragment clusters
        if clusters:
            from ohm.queries import query_fragment_clusters

            cls = query_fragment_clusters(self.current_store.conn)
            response["clusters"] = cls

        self._json_response(200, response)

    def _get_stats(self, path: str, qs: dict) -> None:
        """GET /stats — graph statistics (OHM-a5rz.24: ?include_l0=true adds fragment density)."""
        from ohm.queries import query_stats

        include_l0 = qs.get("include_l0", [None])[0]
        include_l0 = include_l0 and include_l0.lower() in ("true", "1", "yes")

        stats = query_stats(self.current_store.conn, include_l0=include_l0)
        import time

        stats["uptime"] = round(time.time() - _server_module._START_TIME, 1)
        self._json_response(200, stats)

    def _get_status(self, path: str, qs: dict) -> None:
        """GET /status — daemon status."""
        import time

        status = self.current_store.status()
        status["uptime"] = round(time.time() - _server_module._START_TIME, 1)
        status["version"] = "0.2.0"
        status["schema"] = self.schema_config.name
        status["quack"] = self.config.get("quack", False)
        status["multi_tenant"] = self.multi_tenant
        self._json_response(200, status)

    def _get_schema_node_types(self, path: str, qs: dict) -> None:
        """GET /schema/node-types?type=X — per-node-type template + hook constraints."""
        from ohm.queries import node_type_template

        node_type = qs.get("type", [None])[0]
        if not node_type:
            self._json_response(
                400,
                {"error": "validation_error", "message": "?type=<node_type> is required"},
            )
            return
        result = node_type_template(self.current_store.read_conn, node_type=node_type)
        # Enrich with live hook constraints for this schema
        hooks = self.current_store.execute("SELECT command FROM ohm_hooks WHERE event = 'pre_ingest' AND enabled = TRUE")
        hook_map = {
            "source_url_required": "source_url_required",
            "cross_link_check": "cross_link_required",
            "observation_source_required": "observation_source_required",
        }
        result["live_hooks"] = [{"hook": cmd, "constraint": name} for cmd, name in hook_map.items() if any(cmd in h["command"] for h in hooks)]
        from ohm.graph.schema import node_analysis

        result["analysis"] = node_analysis(node_type)
        self._json_response(200, {"ok": True, "data": result})

    def _get_schema(self, path: str, qs: dict) -> None:
        """GET /schema — schema description with usage guidance."""
        schema = self.schema_config
        all_edge_types: set[str] = set()
        for types in schema.layer_edge_types.values():
            all_edge_types.update(types)

        guide = {
            "overview": "OHM is a knowledge graph for multi-agent cognition. Write observations, create nodes and edges, challenge claims, and think in L0 fragments.",
            "writing": {
                "create_node": "POST /node — Create any node type. Required: id, label, type. Optional: content, tags (list), metadata (dict), source_url, confidence, provenance, connects_to (list of existing node ids).",
                "scratch": "POST /scratch -- Write an L0 thinking fragment. Near-zero cost. Required: content. Optional: tags, connects_to, metadata. Auto-detects questions (?). Auto-links semantically. Fragments excluded by default.",
                "create_edge": "POST /edge — Link two nodes. Required: from, to, layer, edge_type. Optional: confidence, provenance, probability.",
                "observe": (
                    "POST /observe/{id} — Record a measurement or observation on a node. "
                    "Required: node_id, obs_type, value. Optional: notes, source, source_url, "
                    "sigma, compression_degree (0-1), compression_type "
                    "(inversion|normative_inversion|retrojection|composite), beneficiary "
                    "(list of agent IDs), revisability (0-1). Also: POST /observations for "
                    "bulk upload, GET /observations for listing. ADR-026: Myth Compression "
                    "Framework fields."
                ),
                "challenge": "POST /challenge — Challenge an L3 interpretation. Required: edge_id. Optional: reason, confidence.",
                "create_skill": "POST /skill — Create a portable skill node. Required: label, trigger. Optional: scope (default personal), required_tools, boundaries, output_format, verification_evidence, connects_to.",
                "create_runbook": "POST /runbook — Create an ordered chain of skills. Required: label, skill_ids (list of existing skill node IDs). Optional: description.",
                "get_runbook_steps": "GET /runbook/{id}/steps — Get ordered skills in a runbook. Returns skill_count and skills array.",
            },
            "reading": {
                "search": "GET /search?q=QUERY — Text search (ILIKE). Returns tip for semantic search when empty. Filters: ?type=, ?created_by=, ?since=, ?until=, ?include_l0=true.",
                "semantic_search": "GET /semantic_search?q=QUERY — Embedding similarity search. Excludes fragments by default. Add &include_l0=true to include L0.",
                "neighborhood": "GET /neighborhood/ID?depth=2 — Get nodes and edges around a node. Filters: ?layer=, ?created_by=AGENT.",
                "stats": "GET /stats — Graph statistics. Excludes fragments by default. Add ?include_l0=true to include L0.",
                "suggest": "GET /suggest?method=shared_tags&min_shared=2 — Find nodes that should be connected based on shared tags.",
                "orphans": "GET /orphans — Find nodes with no edges. Good for finding isolated knowledge.",
                "islands": "GET /islands -- Find disconnected components. Params: min_size (2), max_islands (20), layer, exclude_fragments (true).",
                "welcome": "GET /welcome?agent=NAME -- Orientation packet for new/returning agents. Shows graph overview, your footprint, suggested connections, and recent activity.",
                "orient": "GET /orient?agent=NAME&hours=N -- Context-recovery packet for agents who've lost context. Answers: Where was I? What did I miss? What should I do next? Terse and actionable.",
                "listen": "GET /listen?since=ISO8601 — Change feed. See what agents have added recently. Omit 'since' for the default 24h window; a very recent timestamp may miss writes due to propagation timing.",
            },
            "L0_thinking_layer": {
                "purpose": "Fragments, hunches, questions, raw associations. Unreliable by design (confidence=0.0). Excluded from search/stats/neighborhood by default.",
                "when_to_scratch": "A hunch, a question, a quick observation, a connection you sense but can't articulate yet.",
                "when_not_to_scratch": "A confident concept (use create_node), a verified fact (use create_node with source_url), a known relationship (use create_edge).",
                "l0_edge_types": {
                    "CONTEXT_OF": "Fragment relates to existing concept",
                    "INSPIRED_BY": "Fragment was inspired by another node",
                    "CONTRADICTS_FRAG": "Fragment contradicts another fragment",
                    "REFINES_FRAG": "Fragment refines another fragment",
                    "RESONANCE": "Independent agents noticed the same thing",
                },
                "lifecycle": "Write (scratch) → Auto-link (semantic) → Connect explicitly (link_fragment) → Promote to L1 concept (promote_fragment)",
            },
            "node_type_guide": {
                "concept": "Abstract ideas, patterns, theories — the core of the knowledge graph",
                "source": "External references — articles, books, papers, URLs. MUST have source_url.",
                "event": "Things that happened — incidents, announcements, discoveries",
                "pattern": "Recurring structures — AND-gates, traps, cycles, equilibria",
                "decision": "Choices made — with utility, alternatives, and reasoning",
                "fragment": "L0 thinking — hunches, questions, raw observations. Use /scratch, not /node.",
                "infrastructure": "Physical/virtual hosts — servers, containers, networks",
                "service": "Running software — daemons, APIs, databases, agents",
                "release": "Software versions — deployed or available",
                "technology": "Tools, frameworks, languages, protocols",
                "task": "Action items with status, priority, assignment",
                "skill": "Portable agent capability — trigger, scope, required_tools, boundaries, output_format, verification_evidence. Create via POST /skill.",
                "runbook": "Ordered chain of skill nodes connected by DEPENDS_ON edges. Create via POST /runbook. Query steps via GET /runbook/{id}/steps.",
            },
            "edge_type_guide": {
                "L0": {
                    "CONTEXT_OF": "Fragment relates to existing concept",
                    "INSPIRED_BY": "Fragment inspired by another node",
                    "CONTRADICTS_FRAG": "Fragment contradicts another fragment",
                    "REFINES_FRAG": "Fragment refines another fragment",
                    "RESONANCE": "Independent agents noticed same thing",
                },
                "L1": {
                    "BELONGS_TO": "X belongs to Y (service to host, person to org)",
                    "CONTAINS": "X contains Y (org contains team)",
                    "HAS_COMPONENT": "X has component Y (system has service)",
                    "PART_OF": "X is part of Y (reverse of CONTAINS)",
                    "CAPABLE_OF": "X can do Y (agent capable of skill)",
                },
                "L2": {
                    "REFERENCES": "X references Y (citation, link)",
                    "INFLUENCES": "X influences Y (causal flow, feeds Bayesian inference)",
                    "SERVES": "X serves Y (service serves agent)",
                    "USES": "X uses Y (agent uses tool)",
                    "FEEDS": "X feeds Y (data flow)",
                    "RUNS_ON": "X runs on Y (service runs on host)",
                    "HOSTS": "X hosts Y (host runs service, reverse of RUNS_ON)",
                    "UPSTREAM_OF": "X is upstream of Y (dependency chain)",
                    "TRANSITIONS_TO": "X transitions to Y (version upgrade)",
                },
                "L3": {
                    "CAUSES": "X causes Y (with confidence)",
                    "SUPPORTS": "X supports Y (evidence for)",
                    "CHALLENGED_BY": "X is challenged by Y (evidence against)",
                    "CONTRADICTS": "X contradicts Y (incompatible claims)",
                    "REFINES": "X refines Y (narrowing, clarifying)",
                    "APPLIES_TO": "X applies to Y (pattern to instance)",
                    "TRANSITIONS_TO": "X transitions to Y (state change)",
                },
                "L4": {"DEPENDS_ON": "X depends on Y (infrastructure dependency)", "ENABLES": "X enables Y (prerequisite)", "THREATENS": "X threatens Y (risk)", "RISKS": "X risks Y (uncertainty)", "BLOCKS": "X blocks Y (obstacle)"},
            },
            "cross_link_rule": f"Nodes of type {sorted(schema.must_have_edge_node_types)} MUST have at least one edge when created. Use connects_to or create_edge in the same request.",
            "exempt_from_cross_link": f"Nodes of type {sorted(schema.exempt_cross_link_node_types)} are exempt from the cross-link requirement.",
            "analysis_guide": "See /schema/node-types?type=<type> for per-node-type analysis guidance, or /schema?include_analysis=true for the full map.",
        }

        from ohm.graph.schema import ANALYSIS_GUIDE

        include_analysis = qs.get("include_analysis", ["false"])[0].lower() in ("1", "true", "yes")
        response = {
            "schema": schema.name,
            "node_types": sorted(schema.node_types),
            "edge_types": sorted(all_edge_types),
            "edge_types_by_layer": {k: sorted(v) for k, v in schema.layer_edge_types.items()},
            "layers": schema.layer_descriptions,
            "observation_types": sorted(schema.observation_types),
            "observation_sources": sorted(schema.observation_sources),
            "visibilities": sorted(schema.visibilities),
            "provenances": sorted(schema.provenances),
            "guide": guide,
        }
        if schema.onboarding_node_id:
            response["onboarding_node_id"] = schema.onboarding_node_id
            response["onboarding_hint"] = f"This OHM instance has domain-specific onboarding content. GET /node/{schema.onboarding_node_id} to orient yourself before writing."
        if include_analysis:
            response["analysis"] = ANALYSIS_GUIDE

        self._json_response(
            200,
            response,
        )

    def _get_layers(self, path: str, qs: dict) -> None:
        """GET /layers — layer descriptions."""
        self._json_response(200, self.schema_config.layer_descriptions)

    def _get_templates(self, path: str, qs: dict) -> None:
        """GET /templates?type=<node_type> — usage template for a node type (OHM-461f.1)."""
        from ohm.queries import node_type_template

        node_type = qs.get("type", [""])[0]
        if not node_type:
            self._json_response(
                200,
                {
                    "ok": True,
                    "available_types": ["skill", "runbook"],
                    "usage": "GET /templates?type=skill or GET /templates?type=runbook",
                },
            )
            return
        result = node_type_template(self.current_store.read_conn, node_type=node_type)
        self._json_response(200, {"ok": True, "data": result})

    def _get_queries(self, path: str, qs: dict) -> None:
        """GET /queries?domain=<domain> — useful query patterns for a domain (OHM-461f.1)."""
        domain = qs.get("domain", [""])[0]
        if domain in ("skill", "runbook", "open-skills"):
            from ohm.queries import skill_runbook_query_guide

            result = skill_runbook_query_guide(self.current_store.read_conn)
            self._json_response(200, {"ok": True, "data": result})
            return
        self._json_response(
            200,
            {
                "ok": True,
                "available_domains": ["skill", "runbook", "open-skills"],
                "usage": "GET /queries?domain=skill",
            },
        )

    def _get_plans(self, path: str, qs: dict) -> None:
        """GET /plans — list TOPO plans with optional filters."""
        from ohm.queries import list_plans

        node_id = qs.get("node_id", [None])[0]
        plan_type = qs.get("plan_type", [None])[0]
        status = qs.get("status", [None])[0]
        horizon = qs.get("horizon", [None])[0]
        result = list_plans(
            self.current_store.read_conn,
            node_id=node_id,
            plan_type=plan_type,
            status=status,
            horizon=horizon,
        )
        self._json_response(200, {"ok": True, "data": result, "count": len(result)})

    def _get_reports(self, path: str, qs: dict) -> None:
        """GET /reports — list TOPO reports with optional filters."""
        from ohm.queries import list_reports

        report_type = qs.get("type", [None])[0]
        node_id = qs.get("node_id", [None])[0]
        plan_id = qs.get("plan_id", [None])[0]
        status = qs.get("status", [None])[0]
        result = list_reports(
            self.current_store.read_conn,
            report_type=report_type,
            node_id=node_id,
            plan_id=plan_id,
            status=status,
        )
        self._json_response(200, {"ok": True, "data": result, "count": len(result)})

    def _get_runs(self, path: str, qs: dict) -> None:
        """GET /runs — list TOPO runs with optional filters."""
        from ohm.queries import list_runs

        report_id = qs.get("report_id", [None])[0]
        node_id = qs.get("node_id", [None])[0]
        run_type = qs.get("type", [None])[0]
        status = qs.get("status", [None])[0]
        result = list_runs(
            self.current_store.read_conn,
            report_id=report_id,
            node_id=node_id,
            run_type=run_type,
            status=status,
        )
        self._json_response(200, {"ok": True, "data": result, "count": len(result)})

    def _get_rul(self, path: str, qs: dict) -> None:
        """GET /rul — list RUL assessments with optional filters."""
        from ohm.queries import get_rul_assessments

        equipment_node_id = qs.get("equipment_id", [None])[0]
        risk_class = qs.get("risk_class", [None])[0]
        site_id = qs.get("site_id", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        result = get_rul_assessments(
            self.current_store.read_conn,
            equipment_node_id=equipment_node_id,
            risk_class=risk_class,
            site_id=site_id,
            limit=limit,
        )
        self._json_response(200, {"ok": True, "data": result, "count": len(result)})

    def _get_timeline_rollup(self, path: str, qs: dict) -> None:
        """GET /timeline/<ancestor_id> — roll up events from CONTAINS subtree."""
        from ohm.queries import timeline_rollup

        ancestor_id = path[10:]
        horizon = qs.get("horizon", [None])[0]
        start_after = qs.get("start_after", [None])[0]
        end_before = qs.get("end_before", [None])[0]
        event_class = qs.get("event_class", [None])[0]
        plan_id = qs.get("plan_id", [None])[0]
        include_plans = qs.get("include_plans", ["true"])[0].lower() != "false"
        result = timeline_rollup(
            self.current_store.read_conn,
            ancestor_id,
            horizon=horizon,
            start_after=start_after,
            end_before=end_before,
            event_class=event_class,
            plan_id=plan_id,
            include_plans=include_plans,
        )
        self._json_response(200, {"ok": True, "data": result})

    def _get_report(self, path: str, qs: dict) -> None:
        """GET /report/<id> — fetch a single TOPO report."""
        from ohm.queries import get_report

        report_id = path[8:]
        result = get_report(self.current_store.read_conn, report_id)
        if result is None:
            self._json_response(404, {"ok": False, "error": "Report not found"})
        else:
            self._json_response(200, {"ok": True, "data": result})

    def _get_run(self, path: str, qs: dict) -> None:
        """GET /run/<id> — fetch a single TOPO run."""
        from ohm.queries import get_run

        run_id = path[5:]
        result = get_run(self.current_store.read_conn, run_id)
        if result is None:
            self._json_response(404, {"ok": False, "error": "Run not found"})
        else:
            self._json_response(200, {"ok": True, "data": result})

    def _get_edges(self, path: str, qs: dict) -> None:
        """GET /edges — list edges with filtering.

        Query params:
          from_node, to_node: exact node id filters
          from_type, to_type: node type filters (requires joining ohm_nodes)
          edge_type: exact edge type
          layer: L0/L1/L2/L3/L4
          created_by: agent name
          limit, offset: pagination
        """
        from_node = qs.get("from_node", [None])[0]
        to_node = qs.get("to_node", [None])[0]
        from_type = qs.get("from_type", [None])[0]
        to_type = qs.get("to_type", [None])[0]
        edge_type = qs.get("edge_type", [None])[0]
        layer = qs.get("layer", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])

        conditions = ["e.deleted_at IS NULL"]
        params: list[Any] = []
        joins: list[str] = []

        if from_node:
            conditions.append("e.from_node = ?")
            params.append(from_node)
        if to_node:
            conditions.append("e.to_node = ?")
            params.append(to_node)
        if edge_type:
            conditions.append("e.edge_type = ?")
            params.append(edge_type)
        if layer:
            conditions.append("e.layer = ?")
            params.append(layer)
        if created_by:
            conditions.append("e.created_by = ?")
            params.append(created_by)
        if from_type:
            joins.append("JOIN ohm_nodes nf ON nf.id = e.from_node AND nf.deleted_at IS NULL")
            conditions.append("nf.type = ?")
            params.append(from_type)
        if to_type:
            joins.append("JOIN ohm_nodes nt ON nt.id = e.to_node AND nt.deleted_at IS NULL")
            conditions.append("nt.type = ?")
            params.append(to_type)

        from ohm.server.boundary import apply_read_scope_edge_filters, get_agent_read_scope

        agent = getattr(self, "_current_agent", "ohm")
        scope = get_agent_read_scope(self.current_store.conn, agent)
        if scope is not None:
            scope_joins, scope_conds, scope_params = apply_read_scope_edge_filters(
                self.current_store.conn,
                agent,
                edge_alias="e.",
            )
            joins.extend(scope_joins)
            conditions.extend(scope_conds)
            params.extend(scope_params)

        params.append(limit)
        params.append(offset)
        where_clause = " AND ".join(conditions)
        join_clause = " ".join(joins)
        sql = f"""SELECT e.* FROM ohm_edges e {join_clause}
                  WHERE {where_clause}
                  ORDER BY e.created_at DESC LIMIT ? OFFSET ?"""
        results = self.current_store.execute(sql, params)
        count_sql = f"""SELECT COUNT(*) as cnt FROM ohm_edges e {join_clause} WHERE {where_clause}"""
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0].get("cnt", len(results)) if total_result else len(results)
        self._json_response(
            200,
            {
                "edges": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )

    def _get_node(self, path: str, qs: dict) -> None:
        """GET /node/<id> — fetch a node with effective_layer and constraint_status."""
        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        node = self.current_store.get_node(node_id)
        if node:
            from ohm.server.boundary import enforce_read_scope

            agent = getattr(self, "_current_agent", "ohm")
            enforce_read_scope(
                self.current_store.conn,
                agent,
                node_id=node_id,
                source_tier=node.get("source_tier"),
                created_by=node.get("created_by"),
            )
            # ADR-022: Add effective layer and constraint status
            from ohm.graph.constraints import effective_layer

            eff_layer, constraint_status = effective_layer(self.current_store.conn, node_id)
            node["effective_layer"] = eff_layer
            node["constraint_status"] = constraint_status
            self._json_response(200, node)
        else:
            from ohm.exceptions import NodeNotFoundError

            raise NodeNotFoundError(f"Node {node_id} not found")

    def _get_deep(self, path: str, qs: dict) -> None:
        """GET /deep/<id> — deep content retrieval with connected edges (OHM-7299)."""
        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        try:
            result = self.current_store.deep_content(node_id)
            from ohm.server.boundary import enforce_read_scope, filter_edges_by_read_scope

            agent = getattr(self, "_current_agent", "ohm")
            enforce_read_scope(
                self.current_store.conn,
                agent,
                node_id=node_id,
                source_tier=result.get("source_tier"),
                created_by=result.get("created_by"),
            )
            edges = self.current_store.execute(
                "SELECT * FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL ORDER BY created_at DESC",
                [node_id, node_id],
            )
            edges = filter_edges_by_read_scope(self.current_store.conn, agent, edges)
            result["edges"] = edges
            result["edge_count"] = len(edges)
            self._json_response(200, result)
        except NodeNotFoundError:  # noqa: F821
            raise
        except Exception as e:
            self._json_response(500, {"error": "deep_content_failed", "message": str(e)})

    def _get_edge_suggest_type(self, path: str, qs: dict) -> None:
        """GET /edge/suggest-type?from=<id>&to=<id> — suggest edge type for a pair (OHM-ezt5)."""
        from ohm.exceptions import ValidationError

        from_node_id = qs.get("from", [None])[0]
        to_node_id = qs.get("to", [None])[0]
        if not from_node_id:
            raise ValidationError("?from=<node_id> is required")
        if not to_node_id:
            raise ValidationError("?to=<node_id> is required")
        from ohm.queries import suggest_edge_type

        result = suggest_edge_type(
            self.current_store.read_conn,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
        )
        self._json_response(200, {"ok": True, "data": result})

    def _get_edge(self, path: str, qs: dict) -> None:
        """GET /edge/<id> — fetch an edge."""
        edge_id = path[6:]
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")
        edge = self.current_store.get_edge(edge_id)
        if edge:
            from ohm.server.boundary import enforce_read_scope_for_edge

            agent = getattr(self, "_current_agent", "ohm")
            enforce_read_scope_for_edge(
                self.current_store.conn,
                agent,
                edge,
            )
            self._json_response(200, edge)
        else:
            from ohm.exceptions import EdgeNotFoundError

            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _get_neighborhood(self, path: str, qs: dict) -> None:
        """GET /neighborhood/<id> — node neighborhood.

        Supports ?created_by=AGENT to filter edges by creator.
        Useful for "what did I add to this subgraph?" queries.
        """
        node_id = path[14:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        depth = min(int(qs.get("depth", [3])[0]), 2)  # ADR-023: Cap depth at 2 to prevent OOM on large neighborhoods
        layer = qs.get("layer", [None])[0]
        created_by = qs.get("created_by", [None])[0]

        # OHM-oqyc: enforce read scope on the root node
        from ohm.server.boundary import enforce_read_scope

        agent = getattr(self, "_current_agent", "ohm")
        root_node = self.current_store.get_node(node_id)
        if root_node:
            enforce_read_scope(
                self.current_store.conn,
                agent,
                node_id=node_id,
                source_tier=root_node.get("source_tier"),
                created_by=root_node.get("created_by"),
            )
        else:
            from ohm.exceptions import NodeNotFoundError

            raise NodeNotFoundError(f"Node {node_id} not found")

        from ohm.queries import query_neighborhood

        edges = query_neighborhood(self.current_store.conn, node_id, depth=depth, layer=layer)

        # Filter edges by creator if requested
        if created_by:
            edges = [e for e in edges if e.get("created_by") == created_by]

        node_ids = {node_id}
        for e in edges:
            node_ids.add(e["from_node"])
            node_ids.add(e["to_node"])

        # ADR-015: Add citation_status to L3 edges (Source Citation Architecture)
        # Check if any REFERENCES edges exist in the neighborhood for L3 edge anchoring
        ref_from_nodes = set()
        for e in edges:
            if e.get("edge_type") == "REFERENCES" or e.get("type") == "REFERENCES":
                ref_from_nodes.add(e.get("from_node"))
        for e in edges:
            layer_val = e.get("layer")
            if layer_val == "L3":
                from_node = e.get("from_node", "")
                e["citation_status"] = "verified" if from_node in ref_from_nodes else "unverified"

        placeholders = ", ".join("?" * len(node_ids))
        node_rows = self.current_store.execute(
            f"SELECT id, label, type, created_by, created_at FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            list(node_ids),
        )

        # ADR-022: Add effective_layer for each node in the neighborhood
        # ADR-023: Skip effective_layer computation for large neighborhoods (>500 nodes)
        # to prevent OOM crashes. Include warning when skipped.
        from ohm.graph.constraints import effective_layers
        from ohm.server.boundary import filter_edges_by_read_scope, filter_results_by_read_scope

        LARGE_NEIGHBORHOOD_THRESHOLD = 500

        # OHM-oqyc: enforce read scope on every returned node and edge
        agent = getattr(self, "_current_agent", "ohm")
        node_rows = filter_results_by_read_scope(
            self.current_store.conn,
            agent,
            node_rows,
            id_field="id",
            created_by_field="created_by",
            source_tier_field="source_tier",
        )
        allowed_node_ids = {n["id"] for n in node_rows}
        edges = [e for e in filter_edges_by_read_scope(self.current_store.conn, agent, edges) if e.get("from_node") in allowed_node_ids and e.get("to_node") in allowed_node_ids]

        response = {"nodes": node_rows, "edges": edges}

        if len(node_rows) <= LARGE_NEIGHBORHOOD_THRESHOLD:
            node_ids_list = [n["id"] for n in node_rows]
            eff_layers = effective_layers(self.current_store.conn, node_ids_list)
            for n in node_rows:
                n["effective_layer"] = eff_layers.get(n["id"], "unknown")
        else:
            response["warning"] = f"Neighborhood has {len(node_rows)} nodes; effective_layer computation skipped for performance. Use /constraint-report?batch=true for bulk analysis."
            response["truncated"] = True

        self._json_response(200, response)

    def _get_path(self, path: str, qs: dict) -> None:
        """GET /path/<from>/<to> — shortest path.

        OHM-737 response-code contract:
        - 403 if from or to is itself out of scope (agent can't see that node)
        - 200 [] if both endpoints are visible but no path exists within the
          scoped subgraph (the only route runs through a restricted intermediate)
        """
        parts = path[6:].split("/")
        if len(parts) >= 2:
            from ohm.validation import validate_identifier

            from_node = validate_identifier(parts[0], name="from_node")
            to_node = validate_identifier(parts[1], name="to_node")

            # OHM-737: enforce read scope on endpoints first (403 if invisible)
            from ohm.server.boundary import compute_allowed_nodes, enforce_read_scope

            agent = getattr(self, "_current_agent", "ohm")
            for nid in (from_node, to_node):
                node = self.current_store.get_node(nid)
                if node:
                    enforce_read_scope(
                        self.current_store.conn,
                        agent,
                        node_id=nid,
                        source_tier=node.get("source_tier"),
                        created_by=node.get("created_by"),
                    )
            # Compute allowed-node set for traversal-time scope enforcement
            allowed = compute_allowed_nodes(self.current_store.conn, agent)
            from ohm.queries import query_path

            results = query_path(self.current_store.conn, from_node, to_node, allowed_nodes=allowed)
            self._json_response(200, results)
        else:
            from ohm.exceptions import ValidationError

            raise ValidationError("Path requires /path/from/to")

    def _get_impact(self, path: str, qs: dict) -> None:
        """GET /impact/<id> — impact analysis."""
        node_id = path[8:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        depth = int(qs.get("depth", [5])[0])
        # OHM-737: enforce read scope on seed node before traversal
        from ohm.server.boundary import enforce_read_scope, filter_edges_by_read_scope

        agent = getattr(self, "_current_agent", "ohm")
        node = self.current_store.get_node(node_id)
        if node:
            enforce_read_scope(
                self.current_store.conn,
                agent,
                node_id=node_id,
                source_tier=node.get("source_tier"),
                created_by=node.get("created_by"),
            )
        from ohm.queries import query_impact

        results = query_impact(self.current_store.conn, node_id, depth=depth)
        results = filter_edges_by_read_scope(self.current_store.conn, agent, results)
        self._json_response(200, results)

    def _get_confidence(self, path: str, qs: dict) -> None:
        """GET /confidence/<id> — confidence breakdown."""
        target_id = path[12:]
        from ohm.validation import validate_identifier

        target_id = validate_identifier(target_id, name="target_id")
        from ohm.queries import query_confidence

        # OHM-737: enforce read scope on the target before returning refs
        from ohm.server.boundary import enforce_read_scope, enforce_read_scope_for_edge, filter_edges_by_read_scope

        agent = getattr(self, "_current_agent", "ohm")
        is_node = self.current_store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE id = ?",
            [target_id],
        ).fetchone()
        is_edge = self.current_store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE id = ?",
            [target_id],
        ).fetchone()

        if is_node and is_node[0] > 0:
            node = self.current_store.get_node(target_id)
            if node:
                enforce_read_scope(
                    self.current_store.conn,
                    agent,
                    node_id=target_id,
                    source_tier=node.get("source_tier"),
                    created_by=node.get("created_by"),
                )
            refs_result = self.current_store.conn.execute(
                """SELECT *
                   FROM ohm_edges
                   WHERE to_node = ?
                     AND edge_type IN ('CHALLENGED_BY', 'SUPPORTS', 'REFINES')
                     AND deleted_at IS NULL
                   ORDER BY created_at DESC""",
                [target_id],
            )
            ref_columns = [desc[0] for desc in refs_result.description]
            refs = [dict(zip(ref_columns, row)) for row in refs_result.fetchall()]
            for r in refs:
                r["from"] = r.get("from_node")
                r["to"] = r.get("to_node")
                r["type"] = r.get("edge_type")

            refs = filter_edges_by_read_scope(self.current_store.conn, agent, refs)
            challenges = [r for r in refs if r["edge_type"] == "CHALLENGED_BY"]
            supports = [r for r in refs if r["edge_type"] == "SUPPORTS"]
            refinements = [r for r in refs if r["edge_type"] == "REFINES"]

            self._json_response(
                200,
                {
                    "node_id": target_id,
                    "challenges": challenges,
                    "supports": supports,
                    "refinements": refinements,
                },
            )
        elif is_edge and is_edge[0] > 0:
            edge = self.current_store.get_edge(target_id)
            if edge:
                enforce_read_scope_for_edge(self.current_store.conn, agent, edge)
            results = query_confidence(self.current_store.conn, target_id)
            # Filter the challenge/support/refine edges inside the result
            for key in ("challenges", "supports", "refinements"):
                if isinstance(results.get(key), list):
                    results[key] = filter_edges_by_read_scope(self.current_store.conn, agent, results[key])
            self._json_response(200, results)
        else:
            from ohm.exceptions import NodeNotFoundError

            raise NodeNotFoundError(f"Neither node nor edge found with id: {target_id}")

    def _get_agent(self, path: str, qs: dict) -> None:
        """GET /agent/<name> — agent state."""
        agent_name = path[7:]
        from ohm.validation import validate_identifier

        agent_name = validate_identifier(agent_name, name="agent_name")
        state = self.current_store.get_agent_state(agent_name)
        if state:
            self._json_response(200, state)
        else:
            self._json_response(404, {"error": f"Agent {agent_name} not found"})

    def _get_agents(self, path: str, qs: dict) -> None:
        """GET /agents — list all agent states."""
        results = self.current_store.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name")
        self._json_response(200, results)

    def _get_nodes(self, path: str, qs: dict) -> None:
        """GET /nodes — list nodes with pagination and filtering."""
        node_type = qs.get("type", [None])[0]
        label = qs.get("label", [None])[0]
        label_contains = qs.get("label_contains", [None])[0]
        label_prefix = qs.get("label_prefix", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL"]
        params = []
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        if label:
            conditions.append("label ILIKE ?")
            params.append(f"%{label}%")
        if label_contains:
            conditions.append("label ILIKE ?")
            params.append(f"%{label_contains}%")
        if label_prefix:
            conditions.append("label ILIKE ?")
            params.append(f"{label_prefix}%")
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)

        from ohm.server.boundary import get_agent_read_scope

        agent = getattr(self, "_current_agent", "ohm")
        scope = get_agent_read_scope(self.current_store.conn, agent)
        if scope is not None:
            allowed_tiers = scope.get("source_tier")
            if allowed_tiers is not None:
                placeholders = ",".join(["?"] * len(allowed_tiers))
                conditions.append(f"(source_tier IS NULL OR source_tier IN ({placeholders}))")
                params.extend(allowed_tiers)
            allowed_creators = scope.get("created_by")
            if allowed_creators is not None:
                placeholders = ",".join(["?"] * len(allowed_creators))
                conditions.append(f"created_by IN ({placeholders})")
                params.extend(allowed_creators)

        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0].get("cnt", len(results)) if total_result else len(results)
        self._json_response(
            200,
            {
                "nodes": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )

    def _get_tasks(self, path: str, qs: dict) -> None:
        """GET /tasks — list task nodes with filtering."""
        task_status = qs.get("status", [None])[0]
        assigned_to = qs.get("assigned_to", [None])[0]
        priority = qs.get("priority", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL", "type = 'task'"]
        params = []
        if task_status:
            conditions.append("task_status = ?")
            params.append(task_status)
        if assigned_to:
            conditions.append("assigned_to = ?")
            params.append(assigned_to)
        if priority:
            conditions.append("priority = ?")
            params.append(priority)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END, due_date ASC NULLS LAST, created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0].get("cnt", len(results)) if total_result else len(results)
        self._json_response(
            200,
            {
                "tasks": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )

    def _get_listen(self, path: str, qs: dict) -> None:
        """GET /listen — poll change feed since last sync."""
        from ohm.exceptions import AuthenticationError
        from datetime import datetime, timedelta, timezone

        agent = self._authenticate()
        if agent is None:
            if self.no_auth or not self.tokens:
                agent = "ohm"
            elif self.require_read_auth:
                raise AuthenticationError(  # noqa: F821
                    "Authentication required — provide Bearer token"
                )
            else:
                agent = "ohm"
        since = qs.get("since", [None])[0]
        agent_name = qs.get("agent", [agent or "ohm"])[0]
        enrich = qs.get("enrich", ["false"])[0].lower() == "true"
        if not since:
            state = self.current_store.get_agent_state(agent_name)
            if state and state.get("last_sync"):
                since = state["last_sync"]
                if isinstance(since, datetime):
                    since = since.isoformat()
            else:
                since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        from ohm.queries import query_change_feed

        results = query_change_feed(self.current_store.conn, since=since, agent_name=agent_name, enrich=enrich)
        self._json_response(200, results)

    def _get_search(self, path: str, qs: dict) -> None:
        """GET /search — text search over nodes.

        OHM-a5rz.7: supports ``?since=`` and ``?until=`` ISO 8601 timestamp
        filters to constrain search by ``created_at`` range.

        OHM-a5rz.18: L0 fragments are excluded by default.
        Pass ``?include_l0=true`` to include fragment-type nodes.

        OHM-842: supports ``?tags=`` for AND-semantics tag filtering.
        Pass multiple ``?tags=`` params — all must be present.
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        node_type = qs.get("type", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        since = qs.get("since", [None])[0]
        until = qs.get("until", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
        limit = int(qs.get("limit", [20])[0])
        tags = qs.get("tags", [])
        if not query_text:
            raise ValidationError("Search requires ?q=QUERY")
        conditions = ["deleted_at IS NULL", "(label ILIKE ? OR content ILIKE ?)"]
        params = [f"%{query_text}%", f"%{query_text}%"]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        elif not include_l0:
            # OHM-a5rz.18: exclude L0 fragments from default search results
            conditions.append("type != 'fragment'")
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        if since:
            conditions.append("created_at >= ?::TIMESTAMP")
            params.append(since)
        if until:
            conditions.append("created_at <= ?::TIMESTAMP")
            params.append(until)
        # OHM-842: AND-semantics tag filtering — each tag must be present
        for tag in tags:
            conditions.append("json_contains(tags, ?)")
            params.append(f'"{tag}"')
        # OHM-oqyc: enforce read scope at SQL level
        from ohm.server.boundary import apply_read_scope_filters

        agent = getattr(self, "_current_agent", "ohm")
        scope_conds, scope_params = apply_read_scope_filters(self.current_store.conn, agent)
        conditions.extend(scope_conds)
        params.extend(scope_params)
        params.append(limit)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
        results = self.current_store.execute(sql, params)

        # OHM-tr71.8: Automatic semantic fallback on empty text search
        # When text search returns 0 results, try semantic search automatically.
        # OHM-738: pass node_type through to fallbacks so a typed query can
        # still benefit from semantic/fuzzy matching instead of returning 0.
        # OHM-842: skip fallbacks when tags are specified — fallbacks don't
        # support tag filtering and would bypass the user's explicit constraint.
        if not results and not tags:
            try:
                from ohm.graph.queries import semantic_search

                semantic_results = semantic_search(
                    self.current_store.conn,
                    query=query_text,
                    limit=limit,
                    node_type=node_type,
                    include_l0=include_l0,
                )
                if semantic_results:
                    self._json_response(
                        200,
                        {
                            "results": [
                                {
                                    "id": r.get("node_id", ""),
                                    "label": r.get("label", ""),
                                    "type": r.get("type", ""),
                                    "distance": round(r.get("distance", 1.0), 4),
                                    "match_method": "semantic",
                                }
                                for r in semantic_results
                            ],
                            "count": len(semantic_results),
                            "fallback": "semantic",
                            "tip": f"No exact text matches for '{query_text}'. Showing semantic matches instead. Use /semantic_search?q={query_text} for more options.",
                        },
                    )
                    return
            except (ValueError, ImportError, Exception) as e:
                logger.debug(f"Semantic fallback failed: {e}")

            # OHM-tr71.9: Fuzzy matching fallback — try DuckDB jaro_winkler_similarity
            try:
                from ohm.graph.queries import fuzzy_search as _fuzzy_search

                fuzzy_results = _fuzzy_search(
                    self.current_store.conn,
                    query=query_text,
                    limit=limit,
                    include_l0=include_l0,
                )
                if node_type:
                    fuzzy_results = [r for r in fuzzy_results if r.get("type") == node_type]
                if fuzzy_results:
                    self._json_response(
                        200,
                        {
                            "results": [
                                {
                                    "id": r.get("id", ""),
                                    "label": r.get("label", ""),
                                    "type": r.get("type", ""),
                                    "distance": r.get("distance", 0.0),
                                    "match_method": r.get("match_type", "fuzzy"),
                                }
                                for r in fuzzy_results
                            ],
                            "count": len(fuzzy_results),
                            "fallback": "fuzzy",
                            "tip": f"No exact matches for '{query_text}'. Showing fuzzy label matches instead.",
                        },
                    )
                    return
            except Exception as e:
                logger.debug(f"Fuzzy fallback failed: {e}")

            self._json_response(
                200,
                {
                    "results": [],
                    "count": 0,
                    "tip": f"No results for '{query_text}' via text, semantic, or fuzzy search. Try a different query.",
                },
            )
            return

        self._json_response(200, results)

    def _get_semantic_search(self, path: str, qs: dict) -> None:
        """GET /semantic_search — vector similarity search.

        OHM-a5rz.20: L0 fragments excluded by default. Pass ``?include_l0=true`` to include.
        OHM-xuf4: Pass ``?membership_weight=0.3`` to blend HD Hamming similarity
        alongside cosine similarity. Results then carry ``cosine_similarity``,
        ``hd_similarity``, and ``blended_score`` fields.
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        if not query_text:
            raise ValidationError("Semantic search requires ?q=QUERY")
        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [10])[0])
        min_confidence = qs.get("min_confidence", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
        membership_weight_raw = qs.get("membership_weight", [None])[0]
        membership_weight: float | None = None
        if membership_weight_raw is not None:
            try:
                membership_weight = float(membership_weight_raw)
            except ValueError:
                raise ValidationError("?membership_weight must be a number in [0, 1]")
            if not 0.0 <= membership_weight <= 1.0:
                raise ValidationError("?membership_weight must be in [0, 1]")
        if min_confidence is not None:
            try:
                min_confidence = float(min_confidence)
            except ValueError:
                raise ValidationError("?min_confidence must be a number")
        try:
            from ohm.queries import semantic_search

            results = semantic_search(
                self.current_store.conn,
                query=query_text,
                limit=limit,
                node_type=node_type,
                min_confidence=min_confidence,
                include_l0=include_l0,
                membership_weight=membership_weight,
            )
            # OHM-oqyc: post-filter results by read scope
            from ohm.server.boundary import filter_results_by_read_scope

            agent = getattr(self, "_current_agent", "ohm")
            results = filter_results_by_read_scope(
                self.current_store.conn,
                agent,
                results,
                id_field="node_id",
            )
            self._json_response(200, {"results": results, "count": len(results)})
        except ValueError as e:
            self._json_response(
                503,
                {
                    "error": "service_unavailable",
                    "message": str(e),
                },
            )

    def _get_observations(self, path: str, qs: dict) -> None:
        """GET /observations — list observations with filtering."""
        obs_type = qs.get("type", [None])[0]
        source = qs.get("source", [None])[0]
        node_id = qs.get("node_id", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL"]
        params = []
        if obs_type:
            conditions.append("type = ?")
            params.append(obs_type)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if node_id:
            conditions.append("node_id = ?")
            params.append(node_id)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_observations WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_observations WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0].get("cnt", len(results)) if total_result else len(results)
        self._json_response(200, {"observations": results, "total": total, "limit": limit, "offset": offset})

    def _get_observation(self, path: str, qs: dict) -> None:
        """GET /observation/{id} or /observation/{id}/confidence (OHM-60pd).

        Without the ``/confidence`` suffix: returns the raw observation record.
        With ``/confidence``: returns effective confidence + decay metadata:

            {
              "observation_id": "...",
              "effective_confidence": 0.42,
              "weibull_shape": 1.0,
              "half_life_days": 7.0,
              "decay_function": "weibull",
              "decay_profile": "perishable",
              "age_days": 3.5,
              "evaluated_at": "2026-06-28T..."
            }

        Query params for /confidence:
            at: ISO 8601 timestamp to evaluate at (default: now).
        """
        from datetime import datetime, timezone
        from ohm.graph.decay import confidence_at, decay_profile, default_weibull_shape
        from ohm.exceptions import NodeNotFoundError, ValidationError
        from ohm.validation import validate_timestamp

        prefix = "/observation/"
        if not path.startswith(prefix):
            raise ValidationError("Invalid observation path")
        remainder = path[len(prefix) :]

        if "/" in remainder:
            obs_id, action = remainder.split("/", 1)
        else:
            obs_id, action = remainder, ""

        if not obs_id:
            raise ValidationError("Missing observation id")

        conn = self.current_store.read_conn
        row = conn.execute(
            "SELECT * FROM ohm_observations WHERE id = ? AND deleted_at IS NULL",
            [obs_id],
        ).fetchone()
        if row is None:
            raise NodeNotFoundError(f"Observation {obs_id} not found")
        cols = [d[0] for d in conn.description]
        obs = dict(zip(cols, row))

        if action == "confidence":
            at_str = qs.get("at", [None])[0]
            if at_str:
                at_str = validate_timestamp(at_str)
                t = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            else:
                t = datetime.now(timezone.utc)

            eff = confidence_at(obs, t=t)
            shape = obs.get("weibull_shape")
            if shape is None:
                shape = default_weibull_shape(obs.get("type", "_default"))
            hl = obs.get("half_life_days")
            fn = "weibull" if shape is not None else "exponential"

            # Compute age_days for the response
            anchor = obs.get("valid_from") or obs.get("created_at")
            age_days = None
            if anchor is not None:
                if isinstance(anchor, str):
                    anchor = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (t - anchor).total_seconds() / 86400.0)

            self._json_response(
                200,
                {
                    "observation_id": obs_id,
                    "effective_confidence": round(eff, 6),
                    "weibull_shape": shape,
                    "half_life_days": hl,
                    "decay_function": fn,
                    "decay_profile": decay_profile(hl, shape),
                    "age_days": round(age_days, 4) if age_days is not None else None,
                    "evaluated_at": t.isoformat(),
                },
            )
            return

        if action:
            raise ValidationError(f"Unknown observation action: {action!r}")

        # Enrich with effective_confidence + decay_profile for convenience
        from ohm.graph.decay import confidence_at as _ca, decay_profile as _dp

        obs["effective_confidence"] = round(_ca(obs), 6)
        obs["decay_profile"] = _dp(obs.get("half_life_days"), obs.get("weibull_shape"))
        self._json_response(200, obs)

    def _get_narrative(self, path: str, qs: dict) -> None:
        """GET /narrative/{node_id}?agent=NAME — neighborhood narrative (OHM-q9rt.1).

        Returns a contextualized explanation of WHY an agent should care about
        a node, including reasoning chains, evidence, and a human-readable
        connections summary.
        """
        from ohm.queries import query_neighborhood_narrative

        prefix = "/narrative/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid narrative path")
        node_id = path[len(prefix) :]
        if not node_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing node id")

        agent = qs.get("agent", [None])[0]
        if not agent:
            agent = getattr(self, "_current_agent", None)
            if agent and agent == "ohm":
                agent = None

        depth = int(qs.get("depth", [2])[0])

        # OHM-737: enforce read scope on the seed node before traversal
        from ohm.server.boundary import enforce_read_scope

        scope_agent = getattr(self, "_current_agent", "ohm")
        node = self.current_store.get_node(node_id)
        if node:
            enforce_read_scope(
                self.current_store.conn,
                scope_agent,
                node_id=node_id,
                source_tier=node.get("source_tier"),
                created_by=node.get("created_by"),
            )
        result = query_neighborhood_narrative(
            self.current_store.read_conn,
            node_id,
            agent_name=agent,
            depth=depth,
        )
        self._json_response(200, result)

    def _get_lineage(self, path: str, qs: dict) -> None:
        """GET /lineage/{node_id} — claim lineage (OHM-q9rt.2).

        Explodes a synthesis/pattern/decision node into its supporting
        evidence chain: tree of supporting nodes with observations, source
        leaves, confidence products, and gap detection.
        """
        from ohm.queries import query_claim_lineage

        prefix = "/lineage/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid lineage path")
        node_id = path[len(prefix) :]
        if not node_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing node id")

        max_depth = int(qs.get("depth", [10])[0])

        # OHM-737: enforce read scope on the seed node before traversal
        from ohm.server.boundary import enforce_read_scope

        scope_agent = getattr(self, "_current_agent", "ohm")
        node = self.current_store.get_node(node_id)
        if node:
            enforce_read_scope(
                self.current_store.conn,
                scope_agent,
                node_id=node_id,
                source_tier=node.get("source_tier"),
                created_by=node.get("created_by"),
            )
        result = query_claim_lineage(
            self.current_store.read_conn,
            node_id,
            max_depth=max_depth,
        )
        self._json_response(200, result)

    def _get_contradiction_summary(self, path: str, qs: dict) -> None:
        """GET /contradiction/{node_id} — contradiction summary (OHM-q9rt.3).

        Returns a structured "both sides" view of contradictions involving
        a node: groups of conflicting observations, their agents, effective
        confidence (with decay), existing challenges, and a recommendation.
        """
        from ohm.queries import query_contradiction_summary

        prefix = "/contradiction/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid contradiction path")
        node_id = path[len(prefix) :]
        if not node_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing node id")

        result = query_contradiction_summary(
            self.current_store.read_conn,
            node_id,
        )
        self._json_response(200, result)

    def _get_task_context(self, path: str, qs: dict) -> None:
        """GET /task-context/{task_id} — task context binding (OHM-q9rt.4).

        Returns a task bundled with its 2-hop subgraph, rationale chain,
        expected outcome, and blocking tasks.
        """
        from ohm.queries import query_task_context

        prefix = "/task-context/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid task-context path")
        task_id = path[len(prefix) :]
        if not task_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing task id")

        result = query_task_context(
            self.current_store.read_conn,
            task_id,
        )
        self._json_response(200, result)

    def _get_confidence_report(self, path: str, qs: dict) -> None:
        """GET /confidence-report?agent=NAME&since=ISO8601 — confidence report (OHM-q9rt.5).

        Returns a per-agent report showing which of their edges had confidence
        changes since a timestamp, with the reason for each shift.
        """
        from ohm.queries import query_confidence_report
        from ohm.exceptions import ValidationError

        agent = qs.get("agent", [None])[0]
        if not agent:
            agent = getattr(self, "_current_agent", None)
            if not agent or agent == "ohm":
                raise ValidationError("agent parameter is required")

        since = qs.get("since", [None])[0]

        result = query_confidence_report(
            self.current_store.read_conn,
            agent_name=agent,
            since=since,
        )
        self._json_response(200, result)

    def _post_scenario(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /scenario — counterfactual scenario analysis (OHM-xagx).

        Body:
            {
              "node_id": "supplier-1",
              "failure_probability": 1.0,
              "max_depth": 10,
              "edge_overrides": {"edge-id-1": 0.3},
              "node_interventions": {"node-id-2": 0.9},
              "disabled_edges": ["edge-id-3"],
              "disabled_nodes": ["node-id-4"],
              "compare": true
            }

        When ``compare`` is true, runs both baseline and counterfactual
        and returns the comparison (deltas + summary). When false, returns
        only the counterfactual result.
        """
        from ohm.queries import query_counterfactual_cascade, query_compare_scenarios
        from ohm.exceptions import ValidationError

        node_id = body.get("node_id")
        if not node_id:
            raise ValidationError("node_id is required")

        failure_probability = float(body.get("failure_probability", 1.0))
        max_depth = int(body.get("max_depth", 10))
        edge_overrides = body.get("edge_overrides")
        node_interventions = body.get("node_interventions")
        disabled_edges = set(body.get("disabled_edges", []))
        disabled_nodes = set(body.get("disabled_nodes", []))
        compare = body.get("compare", True)

        if compare:
            result = query_compare_scenarios(
                self.current_store.read_conn,
                node_id,
                failure_probability=failure_probability,
                max_depth=max_depth,
                edge_overrides=edge_overrides,
                node_interventions=node_interventions,
                disabled_edges=disabled_edges,
                disabled_nodes=disabled_nodes,
            )
        else:
            cascade = query_counterfactual_cascade(
                self.current_store.read_conn,
                node_id,
                failure_probability=failure_probability,
                max_depth=max_depth,
                edge_overrides=edge_overrides,
                node_interventions=node_interventions,
                disabled_edges=disabled_edges,
                disabled_nodes=disabled_nodes,
            )
            result = {"node_id": node_id, "cascade": cascade}

        self._json_response(200, result)

    def _post_propose_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /propose-action — propose an action linked to a scenario (OHM-446a).

        Body: {scenario_id, label, rationale?, connects_to?}
        """
        from ohm.queries import propose_action
        from ohm.exceptions import ValidationError

        scenario_id = body.get("scenario_id")
        label = body.get("label")
        if not scenario_id:
            raise ValidationError("scenario_id is required")
        if not label:
            raise ValidationError("label is required")

        result = propose_action(
            self.current_store.conn,
            scenario_id=scenario_id,
            label=label,
            created_by=agent,
            rationale=body.get("rationale"),
            connects_to=body.get("connects_to"),
        )
        self._json_response(201, result)

    def _post_execute_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /execute-action — mark an action as executed (OHM-446a).

        Body: {action_id, outcome?, outcome_notes?}
        """
        from ohm.queries import execute_action
        from ohm.exceptions import ValidationError, NodeNotFoundError

        action_id = body.get("action_id")
        if not action_id:
            raise ValidationError("action_id is required")

        try:
            result = execute_action(
                self.current_store.conn,
                action_id=action_id,
                executed_by=agent,
                outcome=body.get("outcome"),
                outcome_notes=body.get("outcome_notes"),
            )
            self._json_response(200, result)
        except NodeNotFoundError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})

    def _get_loop_status(self, path: str, qs: dict) -> None:
        """GET /loop-status — autonomy loop status (OHM-446a).

        Returns proposed/executed actions and recent scenarios.
        Optional ?agent= filter. Optional ?half_life_days= for decay integration.
        """
        from ohm.queries import query_loop_status

        agent = qs.get("agent", [None])[0]
        half_life_days = 30.0
        hld = qs.get("half_life_days", [None])[0]
        if hld is not None:
            try:
                half_life_days = float(hld)
            except (ValueError, TypeError):
                pass
        result = query_loop_status(self.current_store.read_conn, agent_name=agent, half_life_days=half_life_days)
        self._json_response(200, result)

    # ── Prospect lifecycle (OHM-844) ─────────────────────────────────────

    def _post_prospect(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /prospect — create a prospect node (OHM-844).

        Body: {label, authority?, parent_scenario_id?, planned_start?,
               planned_end?, horizon_label?, tags?, content?, connects_to?}
        """
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.prospects import create_prospect

        label = body.get("label")
        if not label:
            raise ValidationError("label is required")

        result = create_prospect(
            self.current_store.conn,
            label=label,
            created_by=agent,
            authority=body.get("authority"),
            parent_scenario_id=body.get("parent_scenario_id"),
            planned_start=body.get("planned_start"),
            planned_end=body.get("planned_end"),
            horizon_label=body.get("horizon_label"),
            tags=body.get("tags"),
            content=body.get("content"),
            connects_to=body.get("connects_to"),
            confidence=body.get("confidence", 1.0),
        )
        self._json_response(201, result)

    def _post_prospect_transition(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /prospect/{id}/transition — transition prospect lifecycle (OHM-844).

        Body: {new_status, reason?}
        """
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.prospects import transition_prospect

        prospect_id = path.rstrip("/").split("/")[-1]
        new_status = body.get("new_status")
        if not new_status:
            raise ValidationError("new_status is required")

        try:
            result = transition_prospect(
                self.current_store.conn,
                prospect_id=prospect_id,
                new_status=new_status,
                agent=agent,
                reason=body.get("reason"),
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "invalid_transition", "message": str(e)})
        except PermissionError as e:
            self._json_response(403, {"error": "authority_mismatch", "message": str(e)})

    def _get_prospects(self, path: str, qs: dict) -> None:
        """GET /prospects — list prospects with optional filters (OHM-844).

        Query params: ?status=, ?tags= (multiple), ?created_by=, ?limit=
        """
        from ohm.graph.queries.prospects import list_prospects

        status = qs.get("status", [None])[0]
        tags = qs.get("tags", [])
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [20])[0])

        results = list_prospects(
            self.current_store.read_conn,
            status=status,
            tags=tags or None,
            created_by=created_by,
            limit=limit,
        )
        self._json_response(200, {"results": results, "count": len(results)})

    def _get_prospect_detail(self, path: str, qs: dict) -> None:
        """GET /prospect/{id} — prospect detail with children and observations (OHM-844)."""
        from ohm.graph.queries.prospects import prospect_detail

        prospect_id = path.rstrip("/").split("/")[-1]

        try:
            result = prospect_detail(
                self.current_store.read_conn,
                prospect_id=prospect_id,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})

    def _post_simulate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /simulate/{prospect_id} — Monte Carlo prospect simulation (OHM-843).

        Body: {n_iterations?, seed?}

        Runs a Monte Carlo simulation over the prospect's expectation nodes,
        sampling from Beta-PERT distributions per expectation. Persists the
        result as an experiment_result observation.
        """
        from ohm.graph.queries.simulate import simulate_prospect

        prospect_id = path.rstrip("/").split("/")[-1]
        n_iterations = int(body.get("n_iterations", 5000))
        seed = body.get("seed")

        try:
            result = simulate_prospect(
                self.current_store.conn,
                prospect_id=prospect_id,
                n_iterations=n_iterations,
                seed=int(seed) if seed is not None else None,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "simulation_failed", "message": str(e)})

    def _post_decision_autoresearch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /decision/{id}/autoresearch — run one autoresearch round (OHM-845).

        Body: {dry_run?, max_candidates?}

        Generates candidate hypothesis edges, evaluates each via
        transaction-insert-then-rollback, and promotes any that improve
        the recommendation.
        """
        from ohm.decision.autoresearch import run_autoresearch_round

        decision_id = path.rstrip("/").split("/")[-1]
        if decision_id == "autoresearch":
            decision_id = path.rstrip("/").split("/")[-2]

        dry_run = body.get("dry_run", False)
        max_candidates = int(body.get("max_candidates", 5))

        try:
            result = run_autoresearch_round(
                self.current_store.conn,
                decision_id=decision_id,
                dry_run=dry_run,
                max_candidates=max_candidates,
                agent=agent,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "autoresearch_failed", "message": str(e)})

    def _post_type_proposal_evaluate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /type-proposal/{id}/evaluate — evaluate a type proposal (OHM-846)."""
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        proposal_id = path.rstrip("/").split("/")[-1]
        if proposal_id == "evaluate":
            proposal_id = path.rstrip("/").split("/")[-2]

        try:
            result = evaluate_type_proposal(
                self.current_store.conn,
                proposal_id=proposal_id,
                min_distinct_agents=int(body.get("min_distinct_agents", 2)),
                min_evidence_nodes=int(body.get("min_evidence_nodes", 3)),
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "evaluation_failed", "message": str(e)})

    def _post_type_proposal_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /type-proposal/{id}/promote — promote a type to canonical schema (OHM-846)."""
        from ohm.graph.queries.type_proposals import promote_type

        proposal_id = path.rstrip("/").split("/")[-1]
        if proposal_id == "promote":
            proposal_id = path.rstrip("/").split("/")[-2]

        try:
            result = promote_type(
                self.current_store.conn,
                proposal_id=proposal_id,
                agent=agent,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "promotion_failed", "message": str(e)})

    def _post_type_proposal_demote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /type-proposal/{id}/demote — reject/demote a type proposal (OHM-846)."""
        from ohm.graph.queries.type_proposals import demote_type

        proposal_id = path.rstrip("/").split("/")[-1]
        if proposal_id == "demote":
            proposal_id = path.rstrip("/").split("/")[-2]

        try:
            result = demote_type(
                self.current_store.conn,
                proposal_id=proposal_id,
                agent=agent,
                reason=body.get("reason"),
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "demotion_failed", "message": str(e)})

    def _route_type_proposal_post(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """Dispatch /type-proposal/{id}/{evaluate|promote|demote} to the right handler (OHM-846)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /type-proposal/{id}/{evaluate|promote|demote} required")
        action = parts[2]
        if action == "evaluate":
            self._post_type_proposal_evaluate(path, qs, body, agent)
        elif action == "promote":
            self._post_type_proposal_promote(path, qs, body, agent)
        elif action == "demote":
            self._post_type_proposal_demote(path, qs, body, agent)
        else:
            raise ValidationError(f"unknown type-proposal action: {action}")

    def _get_type_proposals(self, path: str, qs: dict) -> None:
        """GET /type-proposals — list type proposals (OHM-846)."""
        from ohm.graph.queries.type_proposals import list_type_proposals

        status = qs.get("status", [None])[0]
        results = list_type_proposals(
            self.current_store.read_conn,
            status=status,
        )
        self._json_response(200, {"results": results, "count": len(results)})

    def _enforce_cross_link_requirement(self, node_id: str, body: dict) -> dict | None:
        """Return a 422 response body if *body* describes a node that must link.

        Per OHM-tjzh / ADR-018: synthesis-like node types (pattern, idea, task,
        decision, and the forward-compat synthesis/observation/interpretation/
        challenge types) cannot stand alone. They must reference an existing
        node via `connects_to` so the claim is anchored to graph structure.

        Exempt types (source, concept, entity) and updates of pre-existing
        nodes pass through. The caller should ``_json_response(422, error)``
        and ``return`` if a non-None error dict is returned.
        """
        from ohm.schema import requires_cross_link

        node_type = _resolve_type_field(body, "node_type", "type", default="concept") or "concept"
        if not requires_cross_link(node_type):
            return None

        # Updates of pre-existing nodes are exempt — you cannot fix a
        # historical dead-end by refusing to update it. The check only
        # applies to new nodes.
        existing = self.current_store.conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        if existing:
            return None

        connects_to = body.get("connects_to")
        if not connects_to:
            return {
                "error": "cross_link_required",
                "message": (
                    f"Nodes of type '{node_type}' must reference at least one existing "
                    f"node via the 'connects_to' field. A bare claim cannot be reached "
                    f"from context, cannot be challenged, and cannot propagate through "
                    f"Bayesian inference. See OHM-tjzh / ADR-018."
                ),
                "node_type": node_type,
                "hint": "Add a 'connects_to' field with one or more existing node ids, or use POST /batch to atomically create the node and at least one edge.",
            }

        if not isinstance(connects_to, list) or not all(isinstance(c, str) for c in connects_to):
            return {
                "error": "validation_error",
                "message": "connects_to must be a list of node id strings",
            }
        if not connects_to:
            return {
                "error": "cross_link_required",
                "message": f"connects_to for type '{node_type}' must list at least one existing node id",
                "node_type": node_type,
            }

        # Verify every referenced id actually exists. Reject 422 (not 404) —
        # the request is well-formed but cannot be processed because the
        # cross-link target is missing.
        placeholders = ",".join(["?"] * len(connects_to))
        rows = self.current_store.conn.execute(
            f"SELECT id FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            connects_to,
        ).fetchall()
        existing_ids = {row[0] for row in rows}
        missing = [cid for cid in connects_to if cid not in existing_ids]
        if missing:
            return {
                "error": "cross_link_unknown_target",
                "message": (f"connects_to references unknown node id(s): {missing}. Cross-link targets must already exist in the graph."),
                "missing": missing,
            }

        return None

    def _post_node(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node — create or upsert a node."""

        # ADR-015 source_url enforcement migrated to built-in pre_ingest hook
        # (python:ohm.hooks_builtin.source_url_required). See OHM-aznh.11.

        create_only = qs.get("create_only", ["false"])[0].lower() in ("true", "1", "yes")
        if create_only:
            existing = self.current_store.conn.execute(
                "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [body["id"]],
            ).fetchone()
            if existing:
                self._json_response(
                    409,
                    {
                        "error": "conflict",
                        "message": f"Node {body['id']} already exists. Use ?create_only=false for upsert.",
                    },
                )
                return

        # OHM-tjzh / ADR-018: cross-link enforcement migrated to
        # built-in pre_ingest hook (python:ohm.hooks_builtin.cross_link_check).
        # The hook is registered automatically on server startup (OHM-aznh.11).
        # Inline _enforce_cross_link_requirement is no longer called here.

        # ADR-022: Validate layer promotion constraints on write
        if body.get("layer") and _resolve_type_field(body, "node_type", "type") == "fragment":
            node_layer = body.get("layer", "L0")
            target_layer = body.get("promote_to_layer")
            if target_layer and node_layer != target_layer:
                from ohm.graph.constraints import validate_layer_promotion

                promote_valid, promote_warnings, promote_errors = validate_layer_promotion(
                    body["id"],
                    node_layer,
                    target_layer,
                    self.current_store.conn,
                    enforce=self.current_config.get("enforce_layer_gates", False),
                )
                if promote_errors:
                    self._json_response(
                        422,
                        {
                            "error": "layer_promotion_denied",
                            "message": "Layer promotion constraints not satisfied",
                            "constraint_errors": promote_errors,
                            "constraint_warnings": promote_warnings,
                        },
                    )
                    return

        hook_error = self._run_pre_ingest_hooks(agent, "node", body)
        if hook_error is not None:
            self._json_response(422, hook_error)
            return

        # OHM-742: When create_only=false and the node already exists, use
        # partial_update (PATCH semantics) so omitted fields preserve their
        # existing values instead of being nulled out (PUT semantics). For
        # new-node creation, defaults are applied as before.
        is_upsert = not create_only
        node_exists = False
        if is_upsert:
            existing_check = self.current_store.conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [body["id"]],
            ).fetchone()
            node_exists = existing_check is not None

        if node_exists:
            # Partial update: pass None for omitted fields so write_node
            # preserves existing values. label and type are always required.
            result = self.current_store.write_node(
                id=body["id"],
                label=body["label"],
                type=_resolve_type_field(body, "node_type", "type", default="concept") or "concept",
                content=body.get("content"),
                confidence=body.get("confidence"),
                visibility=body.get("visibility"),
                provenance=body.get("provenance"),
                tags=body.get("tags"),
                metadata=body.get("metadata"),
                priority=body.get("priority"),
                url=body.get("source_url", body.get("url")),
                task_status=body.get("task_status"),
                assigned_to=body.get("assigned_to"),
                due_date=body.get("due_date"),
                utility_scale=body.get("utility_scale"),
                current_best_action=body.get("current_best_action"),
                action_alternatives=body.get("action_alternatives"),
                utility_usd_per_day=body.get("utility_usd_per_day"),
                utility_currency=body.get("utility_currency"),
                source_tier=body.get("source_tier"),
                agent_name=agent,
                partial_update=True,
            )
        else:
            result = self.current_store.write_node(
                id=body["id"],
                label=body["label"],
                type=_resolve_type_field(body, "node_type", "type", default="concept") or "concept",
                content=body.get("content"),
                confidence=body.get("confidence", 1.0),
                visibility=body.get("visibility", "team"),
                provenance=body.get("provenance"),
                tags=body.get("tags"),
                metadata=body.get("metadata"),
                priority=body.get("priority"),
                url=body.get("source_url", body.get("url")),
                task_status=body.get("task_status"),
                assigned_to=body.get("assigned_to"),
                due_date=body.get("due_date"),
                utility_scale=body.get("utility_scale"),
                current_best_action=body.get("current_best_action"),
                action_alternatives=body.get("action_alternatives"),
                utility_usd_per_day=body.get("utility_usd_per_day"),
                utility_currency=body.get("utility_currency"),
                source_tier=body.get("source_tier"),
                agent_name=agent,
            )
        event_type = "node.created" if result.get("created") else "node.updated"
        decorations = self._run_post_ingest_hooks(agent, "node", result)
        if decorations:
            result["hook_decorations"] = decorations
        _server_module._trigger_webhooks(
            {
                "type": event_type,
                "agent": agent,
                "node": result,
            },
            customer_id=self._customer_id,
        )
        # ADR-017 + ADR-023: Cognitive nudge enrichment with decision detection
        nudges = generate_nudges(
            action="node",
            node_id=result.get("id"),
            tags=body.get("tags"),
            provenance=body.get("provenance"),
            store=self.current_store,
            node=body,
            source_tier=body.get("source_tier"),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
        )
        result = enrich_response(result, nudges, store=self.current_store, agent=agent, action="node", target_id=result.get("id"))

        # OHM-848: Auto-register proposed-type tags into ohm_type_proposals
        if body.get("tags"):
            try:
                from ohm.graph.queries.type_proposals import process_node_tags
                process_node_tags(
                    self.current_store.conn,
                    node_id=result.get("id", ""),
                    tags=body.get("tags"),
                    created_by=agent,
                )
            except Exception:
                pass  # Never fail the write for a type proposal

        # ADR-021: Proactive discoverability — post-write suggestions + connectivity nudge.
        # Run synchronously on the request thread under the write lock. Earlier versions used
        # a ThreadPoolExecutor here, which shared DuckDB connection state across threads and
        # caused intermittent segfaults during full-suite test runs (OHM-k0bi). A fresh
        # read-only suggestion connection is opened per suggestion call and the deadline keeps
        # the response bounded.
        if result.get("created", True) and _suggestions_module._suggestions_enabled():
            deadline = time.time() + _suggestions_module.SUGGESTION_TIMEOUT_S
            try:
                sugg = _suggestions_module.generate_suggestions(
                    store=self.current_store,
                    node_id=result.get("id", ""),
                    content=body.get("content"),
                    label=body.get("label"),
                    tags=body.get("tags"),
                    node_type=_resolve_type_field(body, "node_type", "type"),
                    has_edges=bool(body.get("connects_to")),
                    deadline=deadline,
                    use_store_conn=True,
                )
                nudge = _suggestions_module.generate_connectivity_nudge(self.current_store, agent, deadline=deadline)
                island = _suggestions_module.generate_island_nudge(self.current_store, agent, deadline=deadline)
                result["suggestions"] = sugg
                if nudge:
                    result["connectivity_warning"] = nudge["connectivity_warning"]
                if island:
                    result["island_warning"] = island["island_warning"]
            except Exception as e:
                logger.debug("Suggestions failed: %s", e)

        # OHM-g0kv Feature D: Auto-register alias and content hash on node creation
        if result.get("created", True):
            from ohm.queries import register_alias, register_content_hash
            from ohm.validation import normalize_alias, compute_content_hash

            node_id = result.get("id", body.get("id", ""))
            label = body.get("label", "")
            if node_id and label:
                try:
                    norm_label = normalize_alias(label)
                    if norm_label:
                        register_alias(self.current_store.conn, alias_norm=norm_label, node_id=node_id)
                    norm_id = normalize_alias(node_id)
                    if norm_id and norm_id != norm_label:
                        register_alias(self.current_store.conn, alias_norm=norm_id, node_id=node_id)
                except Exception:
                    logger.debug(f"Alias registration failed for {node_id}", exc_info=True)

            # Register content hash for source nodes with url or source_url
            url_val = body.get("source_url", body.get("url"))
            if url_val:
                try:
                    content_hash = compute_content_hash(url_val)
                    register_content_hash(self.current_store.conn, node_id=node_id, content_hash=content_hash)
                except Exception:
                    logger.debug(f"Content hash registration failed for {node_id}", exc_info=True)
            elif label:
                try:
                    content_hash = compute_content_hash(label)
                    register_content_hash(self.current_store.conn, node_id=node_id, content_hash=content_hash)
                except Exception:
                    logger.debug(f"Content hash registration failed for {node_id}", exc_info=True)

        if result.get("created", True):
            self._json_response(201, result)
        else:
            self._json_response(200, result)

    def _post_node_find_or_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node/find_or_create — find existing node by label+type, or create new one."""
        from ohm.queries import find_or_create_node

        node = find_or_create_node(
            self.current_store.conn,
            label=body["label"],
            node_type=_resolve_type_field(body, "node_type", "type", default="concept") or "concept",
            content=body.get("content"),
            created_by=agent,
            visibility=body.get("visibility", "team"),
            provenance=body.get("provenance"),
            confidence=body.get("confidence", 1.0),
            priority=body.get("priority"),
            url=body.get("url"),
        )
        is_new = node.pop("created", False)
        self._json_response(201 if is_new else 200, node)

    def _post_scratch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /scratch — write an L0 thinking fragment (OHM-a5rz.4).

        Minimal write: just content. Auto-generates id, label (first 80 chars),
        type='fragment'. Extracts URLs from content. Returns 201.
        """
        from ohm.queries import scratch

        content = body.get("content", "").strip()
        if not content:
            self._json_response(400, {"error": "content is required and must be non-empty"})
            return

        try:
            node = scratch(
                self.current_store.conn,
                content=content,
                created_by=agent,
                tags=body.get("tags"),
                connects_to=body.get("connects_to"),
                metadata=body.get("metadata"),
            )
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
            return

        decorations = self._run_post_ingest_hooks(agent, "scratch", node)
        if decorations:
            node["hook_decorations"] = decorations

        # ADR-021: Proactive discoverability — suggestions for scratch
        if _suggestions_module._suggestions_enabled():
            deadline = time.time() + _suggestions_module.SUGGESTION_TIMEOUT_S
            sugg = _suggestions_module.generate_suggestions(
                store=self.current_store,
                node_id=node.get("id", ""),
                content=content,
                label=node.get("label"),
                tags=body.get("tags"),
                node_type="fragment",
                has_edges=bool(body.get("connects_to")),
                deadline=deadline,
                use_store_conn=True,
            )
            node["suggestions"] = sugg

        self._json_response(201, node)

    def _post_fragment_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """Dispatch /fragments/{id}/* POST endpoints."""
        if path.endswith("/connect"):
            self._post_fragment_connect(path, qs, body, agent)
        elif path.endswith("/resolve"):
            self._post_fragment_resolve(path, qs, body, agent)
        elif path.endswith("/promote"):
            self._post_fragment_promote(path, qs, body, agent)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def _post_fragment_connect(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /fragments/{id}/connect — link a fragment to another fragment (OHM-a5rz.11).

        Creates an L0 edge (REFINES_FRAG or CONTRADICTS_FRAG) between two fragments.
        Both nodes must be type='fragment'.
        """
        from ohm.queries import create_edge

        if not path.endswith("/connect"):
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})
            return

        parts = path.rstrip("/").split("/")
        fragment_id = parts[-2]
        target_id = body.get("target_id")
        edge_type = body.get("edge_type", "REFINES_FRAG")
        note = body.get("note")

        if not target_id:
            self._json_response(400, {"error": "target_id is required"})
            return

        if edge_type not in ("REFINES_FRAG", "CONTRADICTS_FRAG", "INSPIRED_BY"):
            self._json_response(400, {"error": f"edge_type must be one of: REFINES_FRAG, CONTRADICTS_FRAG, INSPIRED_BY, got {edge_type}"})
            return

        from_node = self.current_store.conn.execute(
            "SELECT id, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [fragment_id],
        ).fetchone()
        if not from_node:
            self._json_response(404, {"error": f"Fragment not found: {fragment_id}"})
            return
        if from_node[1] != "fragment":
            self._json_response(400, {"error": f"Source node is not a fragment (type={from_node[1]})"})
            return

        to_node = self.current_store.conn.execute(
            "SELECT id, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [target_id],
        ).fetchone()
        if not to_node:
            self._json_response(404, {"error": f"Target fragment not found: {target_id}"})
            return
        if to_node[1] != "fragment":
            self._json_response(400, {"error": f"Target node is not a fragment (type={to_node[1]})"})
            return

        try:
            edge = create_edge(
                self.current_store.conn,
                from_node=fragment_id,
                to_node=target_id,
                layer="L0",
                edge_type=edge_type,
                created_by=agent,
                confidence=0.5,
                provenance="fragment_connect",
                metadata={"note": note} if note else None,
            )
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
            return

        decorations = self._run_post_ingest_hooks(agent, "fragment_connect", edge)
        if decorations:
            edge["hook_decorations"] = decorations
        self._json_response(201, edge)

    def _post_fragment_resolve(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /fragments/{id}/resolve — mark a question fragment as resolved (OHM-a5rz.12)."""
        from ohm.queries import resolve_question

        if not path.endswith("/resolve"):
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})
            return

        parts = path.rstrip("/").split("/")
        fragment_id = parts[-2]

        result = resolve_question(
            self.current_store.conn,
            fragment_id=fragment_id,
            resolved_by=agent,
        )
        if result is None:
            self._json_response(404, {"error": f"Question fragment not found or not a question: {fragment_id}"})
            return

        self._json_response(200, result)

    def _post_fragment_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /fragments/{id}/promote — promote fragment to L1 concept (OHM-a5rz.26).

        Validates ADR-022 L0→L1 promotion constraints (min_context_links ≥ 1).
        """
        from ohm.queries import promote_fragment
        from ohm.exceptions import ConstraintViolationError

        if not path.endswith("/promote"):
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})
            return

        parts = path.rstrip("/").split("/")
        fragment_id = parts[-2]

        # ADR-022: Validate L0→L1 promotion constraints before promoting
        from ohm.graph.constraints import validate_layer_promotion

        promote_valid, promote_warnings, promote_errors = validate_layer_promotion(
            fragment_id,
            "L0",
            "L1",
            self.current_store.conn,
            enforce=self.current_config.get("enforce_layer_gates", False),
        )
        if promote_errors:
            self._json_response(
                422,
                {
                    "error": "layer_promotion_denied",
                    "message": "Fragment does not satisfy L0→L1 promotion constraints",
                    "constraint_errors": promote_errors,
                    "constraint_warnings": promote_warnings,
                },
            )
            return

        try:
            result = promote_fragment(
                self.current_store.conn,
                fragment_id=fragment_id,
                promoted_by=agent,
            )
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
            return
        except ConstraintViolationError as e:
            self._json_response(422, {"error": "layer_promotion_denied", "message": str(e)})
            return

        # Include constraint info in response
        if promote_warnings:
            result["constraint_warnings"] = promote_warnings

        self._json_response(201, result)

    def _post_edge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /edge — create an edge.

        Validates ADR-022 edge-level constraints (min_layer, require_references, etc.).
        """
        # ADR-022: Validate edge-level constraints
        from ohm.graph.constraints import validate_edge_constraints

        edge_valid, edge_warnings, edge_errors = validate_edge_constraints(
            edge_type=_resolve_type_field(body, "edge_type", "type", default="") or "",
            layer=body.get("layer", "L3"),
            conn=self.current_store.conn,
            from_node=body.get("from"),
            confidence=body.get("confidence"),
            enforce=self.current_config.get("enforce_layer_gates", False),
        )
        if edge_errors:
            self._json_response(
                422,
                {
                    "error": "edge_constraint_denied",
                    "message": "Edge constraints not satisfied",
                    "constraint_errors": edge_errors,
                    "constraint_warnings": edge_warnings,
                },
            )
            return

        hook_error = self._run_pre_ingest_hooks(agent, "edge", body)
        if hook_error is not None:
            self._json_response(422, hook_error)
            return

        result = self.current_store.write_edge(
            from_node=body["from"],
            to_node=body["to"],
            edge_type=body["type"],
            layer=body.get("layer", "L3"),
            confidence=body.get("confidence"),
            condition=body.get("condition"),
            provenance=body.get("provenance"),
            challenge_of=body.get("challenge_of"),
            challenge_type=body.get("challenge_type"),
            urgency=body.get("urgency"),
            probability=body.get("probability"),
            probability_p05=body.get("probability_p05"),
            probability_p50=body.get("probability_p50"),
            probability_p95=body.get("probability_p95"),
            confidence_p05=body.get("confidence_p05"),
            confidence_p50=body.get("confidence_p50"),
            confidence_p95=body.get("confidence_p95"),
            source_tier=body.get("source_tier"),
            agent_name=agent,
        )
        decorations = self._run_post_ingest_hooks(agent, "edge", result)
        if decorations:
            result["hook_decorations"] = decorations
        _server_module._trigger_webhooks(
            {
                "type": "edge.created",
                "agent": agent,
                "edge": result,
            },
            customer_id=self._customer_id,
        )
        # ADR-017 + ADR-023: Cognitive nudge enrichment with causal guidance
        _challenge_ratio = self._get_challenge_ratio()
        _edge_type_for_nudge = _resolve_type_field(body, "edge_type", "type")
        nudges = generate_nudges(
            action="edge",
            node_id=body.get("to") or body.get("from"),
            edge_type=_edge_type_for_nudge,
            confidence=body.get("confidence"),
            provenance=body.get("provenance"),
            tags=None,
            store=self.current_store,
            from_node_id=body.get("from"),
            to_node_id=body.get("to"),
            challenge_ratio=_challenge_ratio,
            source_tier=body.get("source_tier"),
            condition=body.get("condition"),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
        )
        result = enrich_response(result, nudges, store=self.current_store, agent=agent, action="edge", target_id=result.get("id") if isinstance(result, dict) else None)

        # ADR-021: Relational tags — add edge type as tag on both endpoints
        try:
            from ohm.server.relational_tags import add_relational_tags

            tag_result = add_relational_tags(
                conn=self.current_store.conn,
                from_node=body["from"],
                to_node=body["to"],
                edge_type=_resolve_type_field(body, "edge_type", "type", default="") or "",
            )
            if tag_result["tags_added"]:
                result["relational_tags"] = tag_result
        except Exception as e:
            # Relational tags never fail the write
            logger.debug(f"Relational tag enrichment failed: {e}")

        # ADR-021: Proactive discoverability — post-write edge suggestions
        if _suggestions_module._suggestions_enabled():
            try:
                deadline = time.time() + _suggestions_module.SUGGESTION_TIMEOUT_S
                edge_suggestions = _suggestions_module.generate_edge_suggestions(
                    store=self.current_store,
                    from_node=body["from"],
                    to_node=body["to"],
                    edge_type=_resolve_type_field(body, "edge_type", "type", default="") or "",
                    layer=body.get("layer", "L3"),
                    deadline=deadline,
                    use_store_conn=True,
                )
                if edge_suggestions["related_edges"] or edge_suggestions["edge_patterns"] or edge_suggestions["orphan_resolved"]:
                    result["suggestions"] = edge_suggestions
            except Exception as e:
                # Suggestions never fail the write
                logger.debug(f"Edge suggestions failed: {e}")

        # ADR-022: Include constraint warnings in response (advisory mode)
        if edge_warnings:
            result["constraint_warnings"] = edge_warnings

        self._json_response(201, result)

    def _post_challenge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /challenge/{id} — challenge an existing edge.

        ADR-025: ``challenge_type`` in the request body is a semantic label
        (e.g. ``empirical``, ``logical``) stored in the ``challenge_type``
        column. The ``edge_type`` is always ``CHALLENGED_BY`` for this
        endpoint — use POST /support/{id} to create SUPPORTS edges.
        """
        edge_id = path[11:]
        from ohm.validation import validate_identifier
        from ohm.exceptions import EdgeNotFoundError

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.5)
        semantic_type = body.get("challenge_type", "CHALLENGED_BY")
        result = self.current_store.challenge_edge(
            edge_id,
            reason,
            confidence,
            "CHALLENGED_BY",
            agent_name=agent,
            challenge_type_column=semantic_type,
        )
        if result:
            # OHM-a5rz.15: reflect the challenge back to originating L0 fragments
            try:
                from ohm.graph.queries import reflect_challenge_to_fragments

                reflected = reflect_challenge_to_fragments(
                    self.current_store.conn,
                    edge_id,
                    result.get("id", ""),
                    agent,
                )
                if reflected:
                    result["backflow_fragments"] = [r["fragment_id"] for r in reflected]
            except Exception:
                pass  # backflow is advisory; never block the challenge
            _server_module._trigger_webhooks(
                {
                    "type": "edge.challenged",
                    "agent": agent,
                    "edge": result,
                    "challenge_type": semantic_type,
                },
                customer_id=self._customer_id,
            )
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _post_support(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /support/{id} — support an existing edge."""
        edge_id = path[9:]
        from ohm.validation import validate_identifier
        from ohm.exceptions import EdgeNotFoundError

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.8)
        result = self.current_store.challenge_edge(edge_id, reason, confidence, "SUPPORTS", agent_name=agent, challenge_type_column="SUPPORTS")
        if result:
            _server_module._trigger_webhooks(
                {
                    "type": "edge.supported",
                    "agent": agent,
                    "edge": result,
                },
                customer_id=self._customer_id,
            )
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _post_observe(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /observe/{id} — record an observation on a node."""
        from ohm.exceptions import NodeNotFoundError, ValidationError

        node_id = path[9:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        if not self.current_store.get_node(node_id):
            raise NodeNotFoundError(f"Node not found: {node_id}")
        obs_type = _resolve_type_field(body, "obs_type", "type", default="measurement") or "measurement"
        if obs_type not in self.schema_config.observation_types:
            raise ValidationError(f"Invalid observation type '{obs_type}' — must be one of: {', '.join(sorted(self.schema_config.observation_types))}")
        scale = body.get("scale")
        if scale is not None:
            from ohm.graph.schema import VALID_OBSERVATION_SCALES

            if scale not in VALID_OBSERVATION_SCALES:
                raise ValidationError(f"Invalid scale '{scale}' — must be one of: {', '.join(sorted(VALID_OBSERVATION_SCALES))}")
            # ADR-025: Normalize binary to probability
            if scale == "binary":
                scale = "probability"
            if scale == "probability":
                value = body.get("value")
                if value is not None and (value < 0.0 or value > 1.0):
                    raise ValidationError(f"Observation value {value} is outside [0, 1] for scale='probability'")
        # ADR-026: Validate compression framework fields
        compression_type = body.get("compression_type")
        if compression_type is not None:
            from ohm.graph.schema import VALID_COMPRESSION_TYPES

            if compression_type not in VALID_COMPRESSION_TYPES:
                raise ValidationError(f"Invalid compression_type '{compression_type}' — must be one of: {', '.join(sorted(VALID_COMPRESSION_TYPES))}")
        compression_degree = body.get("compression_degree")
        if compression_degree is not None and (compression_degree < 0.0 or compression_degree > 1.0):
            raise ValidationError(f"compression_degree {compression_degree} is outside [0, 1]")
        revisability = body.get("revisability")
        if revisability is not None and (revisability < 0.0 or revisability > 1.0):
            raise ValidationError(f"revisability {revisability} is outside [0, 1]")
        beneficiary = body.get("beneficiary")  # List of agent/node IDs
        if beneficiary is not None and not isinstance(beneficiary, list):
            raise ValidationError("beneficiary must be a list of strings")
        result = self.current_store.write_observation(
            node_id=node_id,
            type=obs_type,
            value=body.get("value"),
            baseline=body.get("baseline"),
            sigma=body.get("sigma"),
            source=body.get("source"),
            notes=body.get("notes"),
            source_name=body.get("source_name"),
            source_url=body.get("source_url"),
            scale=scale,
            agent_name=agent,
            half_life_days=body.get("half_life_days"),
            weibull_shape=body.get("weibull_shape"),
            compression_degree=compression_degree,
            compression_type=compression_type,
            beneficiary=beneficiary,
            revisability=revisability,
            idempotency_key=body.get("idempotency_key"),
        )
        _server_module._trigger_webhooks(
            {
                "type": "observation.created",
                "agent": agent,
                "observation": result,
            },
            customer_id=self._customer_id,
        )
        # ADR-017 + ADR-023: Cognitive nudge enrichment with inference delta
        nudges = generate_nudges(
            action="observation",
            node_id=node_id,
            confidence=body.get("value"),
            provenance=body.get("source"),
            source_url=body.get("source_url"),
            store=self.current_store,
            obs_type=_resolve_type_field(body, "obs_type", "type", default="measurement") or "measurement",
            half_life_days=body.get("half_life_days"),
            value=body.get("value"),
        )
        result = enrich_response(result, nudges, store=self.current_store, agent=agent, action="observation", target_id=node_id)
        self._json_response(201, result)

    def _post_nudge_accept(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /nudges/{id}/accept — accept or reject a logged nudge (OHM-jdfq).

        Body: {"helpful": bool, "notes": str?}

        Updates ohm_nudge_log.accepted and accepted_at. The agent must match
        the nudge's recorded agent (unless no agent was recorded, in which
        case the check is skipped). Re-accepting overwrites the prior
        response — last write wins.
        """
        from ohm.exceptions import ValidationError
        from ohm.server.nudges import accept_nudge

        # path is "/nudges/{id}/accept" → strip "/nudges/" and "/accept"
        nudge_id = path[len("/nudges/") :]
        if nudge_id.endswith("/accept"):
            nudge_id = nudge_id[: -len("/accept")]
        if not nudge_id:
            raise ValidationError("Missing nudge id in path")

        helpful = bool(body.get("helpful", True))
        notes = body.get("notes")

        result = accept_nudge(
            self.current_store.conn,
            nudge_id=nudge_id,
            agent=agent,
            helpful=helpful,
            notes=notes,
        )
        self._json_response(
            200,
            {
                "nudge_id": result["id"],
                "nudge_type": result["nudge_type"],
                "accepted": result["accepted"],
                "accepted_at": str(result["accepted_at"]) if result["accepted_at"] else None,
                "agent": result["agent"],
                "target_id": result["target_id"],
                "message": result["message"],
            },
        )

    def _get_nudge_quality(self, path: str, qs: dict) -> None:
        """GET /admin/nudges/quality — aggregate nudge acceptance stats.

        Query params (optional): since (ISO timestamp), agent (filter).
        Returns per-type and per-agent acceptance rates so operators can
        see which nudges are actually helping.
        """
        from ohm.server.nudges import nudge_acceptance_stats

        since = qs.get("since", [None])[0]
        agent_filter = qs.get("agent", [None])[0]
        stats = nudge_acceptance_stats(
            self.current_store.conn,
            since=since,
            agent=agent_filter,
        )
        self._json_response(200, stats)

    def _post_observations(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /observations — bulk observation upload (OHM-0lf)."""
        from ohm.exceptions import ValidationError

        obs_list = body.get("observations", [])
        if not isinstance(obs_list, list):
            raise ValidationError("'observations' must be an array")
        if len(obs_list) > 1000:
            raise ValidationError(f"Too many observations: {len(obs_list)} (max 1000)")

        results = []
        errors = []
        for i, obs in enumerate(obs_list):
            node_id = obs.get("node_id")
            if not node_id:
                errors.append({"index": i, "error": "missing node_id"})
                continue
            from ohm.validation import validate_identifier

            try:
                node_id = validate_identifier(node_id, name="node_id")
            except ValueError as e:
                errors.append({"index": i, "error": str(e)})
                continue
            try:
                obs_type = obs.get("obs_type", obs.get("type", "measurement"))
                if obs_type not in self.schema_config.observation_types:
                    errors.append({"index": i, "error": f"Invalid observation type '{obs_type}' — must be one of: {', '.join(sorted(self.schema_config.observation_types))}"})
                    continue
                scale = obs.get("scale")
                if scale is not None:
                    from ohm.graph.schema import VALID_OBSERVATION_SCALES

                    if scale not in VALID_OBSERVATION_SCALES:
                        errors.append({"index": i, "error": f"Invalid scale '{scale}' — must be one of: {', '.join(sorted(VALID_OBSERVATION_SCALES))}"})
                        continue
                    # ADR-025: Normalize binary to probability
                    if scale == "binary":
                        scale = "probability"
                    if scale == "probability":
                        value = obs.get("value")
                        if value is not None and (value < 0.0 or value > 1.0):
                            errors.append({"index": i, "error": f"Observation value {value} is outside [0, 1] for scale='probability'"})
                            continue
                result = self.current_store.write_observation(
                    node_id=node_id,
                    type=obs_type,
                    value=obs.get("value"),
                    baseline=obs.get("baseline"),
                    sigma=obs.get("sigma"),
                    source=obs.get("source"),
                    notes=obs.get("notes"),
                    source_name=obs.get("source_name"),
                    source_url=obs.get("source_url"),
                    scale=scale,
                    agent_name=agent,
                    half_life_days=obs.get("half_life_days"),
                    weibull_shape=obs.get("weibull_shape"),
                    idempotency_key=obs.get("idempotency_key"),
                )
                results.append(result)
            except Exception as e:
                errors.append({"index": i, "node_id": node_id, "error": str(e)})

        self._json_response(
            201,
            {
                "created": len(results),
                "errors": errors,
                "observations": results,
            },
        )

    def _post_outcome(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /outcome — record whether a source agent's claim was correct."""
        from ohm.exceptions import ValidationError

        source_agent = body.get("source_agent")
        claim_node = body.get("claim_node")
        outcome = body.get("outcome")
        notes = body.get("notes")
        if not source_agent or not claim_node or outcome is None:
            raise ValidationError("outcome requires source_agent, claim_node, and outcome fields")
        from ohm.queries import query_record_outcome

        result = query_record_outcome(
            self.current_store.conn,
            source_agent=source_agent,
            claim_node=claim_node,
            outcome=bool(outcome),
            recorded_by=agent,
            notes=notes,
        )
        self._json_response(201, result)

    def _post_node_sign(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node/sign/<id> — sign a node's write with HMAC."""
        from ohm.exceptions import ValidationError
        from ohm.validation import validate_identifier

        node_id = validate_identifier(path[len("/node/sign/") :], name="node_id")
        key = body.get("key", "").encode()
        key_id = body.get("key_id", "default")
        algorithm = body.get("algorithm", "hmac-sha256")
        if not key:
            raise ValidationError("key is required")
        from ohm.queries import sign_node_write

        result = sign_node_write(self.current_store.conn, node_id, key=key, algorithm=algorithm, key_id=key_id)
        self._json_response(200, result)

    def _post_node_verify(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node/verify/<id> — verify a node's write signature."""
        from ohm.validation import validate_identifier

        node_id = validate_identifier(path[len("/node/verify/") :], name="node_id")
        key = body.get("key", "").encode()
        if not key:
            from ohm.exceptions import ValidationError

            raise ValidationError("key is required")
        from ohm.queries import verify_node_write

        result = verify_node_write(self.current_store.conn, node_id, key=key)
        self._json_response(200, result)

    def _post_edge_sign(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /edge/sign/<id> — sign an edge's write with HMAC."""
        from ohm.exceptions import ValidationError
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(path[len("/edge/sign/") :], name="edge_id")
        key = body.get("key", "").encode()
        key_id = body.get("key_id", "default")
        algorithm = body.get("algorithm", "hmac-sha256")
        if not key:
            raise ValidationError("key is required")
        from ohm.queries import sign_edge_write

        result = sign_edge_write(self.current_store.conn, edge_id, key=key, algorithm=algorithm, key_id=key_id)
        self._json_response(200, result)

    def _post_edge_verify(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /edge/verify/<id> — verify an edge's write signature."""
        from ohm.exceptions import ValidationError
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(path[len("/edge/verify/") :], name="edge_id")
        key = body.get("key", "").encode()
        if not key:
            raise ValidationError("key is required")
        from ohm.queries import verify_edge_write

        result = verify_edge_write(self.current_store.conn, edge_id, key=key)
        self._json_response(200, result)

    def _post_synthesis(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /agent/synthesis — one-call L3 writing: concept node + edges + observation."""
        from ohm.exceptions import ValidationError

        label = body.get("label")
        content = body.get("content")
        cluster_ids = body.get("cluster_ids", [])
        edge_type = body.get("edge_type", "SUPPORTS")
        confidence = body.get("confidence", 0.8)
        sigma = body.get("sigma", 0.1)
        provenance = body.get("provenance")
        tags = body.get("tags")

        if not label or not content or not cluster_ids:
            raise ValidationError("agent/synthesis requires label, content, and cluster_ids")

        from ohm.graph.schema import generate_node_id
        from ohm.validation import validate_identifier
        from ohm.exceptions import NodeNotFoundError
        import json as _json

        # OHM-tjzh: Validate that all cluster_ids reference existing nodes
        # before creating the synthesis node. Synthesis without connections
        # is a dead end — the cross-link constraint prevents this.
        validated_cluster_ids = []
        invalid_ids = []
        for cid in cluster_ids:
            try:
                safe_cid = validate_identifier(cid, name="cluster_id")
            except ValueError:
                invalid_ids.append(cid)
                continue
            # Check that the target node exists (OHM-tjzh)
            exists = self.current_store.conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [safe_cid],
            ).fetchone()
            if exists:
                validated_cluster_ids.append(safe_cid)
            else:
                invalid_ids.append(safe_cid)

        if not validated_cluster_ids:
            raise ValidationError(f"agent/synthesis requires at least one cluster_id that references an existing node. None of the provided cluster_ids were found: {cluster_ids}")

        if invalid_ids:
            import logging

            logging.getLogger("ohm.handlers").warning("Synthesis cluster_ids not found, skipping: %s", invalid_ids)

        node_id = generate_node_id(label)
        node_result = self.current_store.write_node(
            id=node_id,
            label=label,
            type="concept",
            content=content,
            confidence=confidence,
            agent_name=agent,
            provenance=provenance or f"{agent}_synthesis",
        )
        node_id = node_result["id"] if isinstance(node_result, dict) else node_id

        if tags:
            self.current_store.conn.execute(
                "UPDATE ohm_nodes SET tags = ? WHERE id = ?",
                [_json.dumps(tags), node_id],
            )

        edges_created = 0
        edge_errors = []
        for cid in validated_cluster_ids:
            try:
                self.current_store.write_edge(
                    from_node=node_id,
                    to_node=cid,
                    edge_type=edge_type,
                    layer="L3",
                    confidence=confidence,
                    agent_name=agent,
                )
                edges_created += 1
            except NodeNotFoundError as e:
                edge_errors.append(str(e))
            except Exception:
                edge_errors.append(f"Failed to create edge to {cid}")

        from ohm.queries import create_observation

        obs_result = create_observation(
            self.current_store.conn,
            node_id=node_id,
            obs_type="pattern",
            value=confidence,
            sigma=sigma,
            source="synthesis",
            notes=content,
            created_by=agent,
        )

        result = {
            "node": node_result if isinstance(node_result, dict) else {"id": node_id, "label": label},
            "edges_created": edges_created,
            "observation": obs_result,
        }
        if invalid_ids or edge_errors:
            result["warnings"] = []
            if invalid_ids:
                result["warnings"].append(f"cluster_ids not found (skipped): {invalid_ids}")
            if edge_errors:
                result["warnings"].extend(edge_errors)

        # OHM-jbsr: Oppositional review — flag CAUSES edges with homogeneous
        # source_tier/agent support that touch the clusters this synthesis
        # backs. Non-fatal: never blocks the synthesis.
        try:
            from ohm.graph.methods import oppositional_review

            all_flagged = []
            seen = set()
            for cid in validated_cluster_ids:
                review = oppositional_review(
                    self.current_store.conn,
                    target_node_id=cid,
                    auto_challenge=False,
                    limit=10,
                )
                for entry in review["flagged_edges"]:
                    if entry["edge_id"] not in seen:
                        seen.add(entry["edge_id"])
                        all_flagged.append(entry)
            if all_flagged:
                result["oppositional_review"] = {
                    "flagged_edges": all_flagged,
                    "challenged_edges": [],
                    "review_summary": {
                        "total_flagged": len(all_flagged),
                        "total_challenged": 0,
                        "dimensions_used": ["source_tier", "agent_authorship"],
                        "auto_challenge": False,
                    },
                }
        except Exception:
            import logging

            logging.getLogger("ohm.handlers").debug("oppositional review skipped for synthesis %s", node_id, exc_info=True)

        # OHM-8q5d: Source diversity — aggregate Shannon entropy across
        # evidence backing the cluster_ids. Non-fatal enrichment.
        try:
            from ohm.graph.methods import source_diversity_score

            cluster_diversity = []
            for cid in validated_cluster_ids:
                ds = source_diversity_score(self.current_store.conn, cid)
                cluster_diversity.append(ds)
            if cluster_diversity:
                avg_score = sum(d["score"] for d in cluster_diversity) / len(cluster_diversity)
                result["source_diversity"] = {
                    "cluster_diversity": cluster_diversity,
                    "aggregate_score": round(avg_score, 4),
                    "cluster_count": len(cluster_diversity),
                }
            else:
                result["source_diversity"] = {
                    "cluster_diversity": [],
                    "aggregate_score": 0.0,
                    "cluster_count": 0,
                }
        except Exception:
            import logging

            logging.getLogger("ohm.handlers").debug("source_diversity_score skipped for synthesis %s", node_id, exc_info=True)

        self._json_response(201, result)

    def _post_batch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /batch — batch node and edge creation (all-or-nothing transaction)."""
        from ohm.exceptions import ValidationError
        import json

        nodes = body.get("nodes", [])
        edges = body.get("edges", [])
        errors = []
        nodes_created = 0
        edges_created = 0

        if len(nodes) + len(edges) > _server_module.MAX_BATCH_SIZE:
            raise ValidationError(f"Batch too large: {len(nodes)} nodes + {len(edges)} edges = {len(nodes) + len(edges)} items exceeds limit of {_server_module.MAX_BATCH_SIZE}")

        for i, node in enumerate(nodes):
            if "id" not in node or "label" not in node:
                errors.append({"index": i, "type": "node", "error": "Missing required field: id and label"})
        for i, edge in enumerate(edges):
            if "from" not in edge or "to" not in edge or "type" not in edge:
                errors.append({"index": i, "type": "edge", "error": "Missing required field: from, to, type"})

        if errors:
            raise ValidationError(f"Batch validation failed: {json.dumps(errors)}")

        batch_node_ids = {n.get("id") for n in nodes if n.get("id")}

        try:
            self.current_store.conn.execute("BEGIN TRANSACTION")
            for node in nodes:
                # OHM-jw1x: run pre_ingest hooks for each node in the batch,
                # same as the normal POST /node path. Pass the batch's edges
                # and node ids so cross_link_check can implement ADR-018
                # option 2 (accept a node when an edge in the same batch
                # references it).
                hook_error = self._run_pre_ingest_hooks(
                    agent, "node", node, batch_edges=edges, batch_node_ids=batch_node_ids
                )
                if hook_error is not None:
                    raise ValidationError(f"Batch node {node.get('id', '?')} rejected by pre_ingest hook: {hook_error.get('message', hook_error)}")
                self.current_store.write_node(
                    id=node["id"],
                    label=node["label"],
                    type=node.get("type", "concept"),
                    content=node.get("content"),
                    confidence=node.get("confidence", 1.0),
                    visibility=node.get("visibility", "team"),
                    provenance=node.get("provenance"),
                    tags=node.get("tags"),
                    metadata=node.get("metadata"),
                    priority=node.get("priority"),
                    url=node.get("url"),
                    task_status=node.get("task_status"),
                    assigned_to=node.get("assigned_to"),
                    due_date=node.get("due_date"),
                    utility_scale=node.get("utility_scale"),
                    current_best_action=node.get("current_best_action"),
                    action_alternatives=node.get("action_alternatives"),
                    utility_usd_per_day=node.get("utility_usd_per_day"),
                    utility_currency=node.get("utility_currency"),
                    agent_name=agent,
                )
                nodes_created += 1
            for edge in edges:
                self.current_store.write_edge(
                    from_node=edge["from"],
                    to_node=edge["to"],
                    edge_type=edge["type"],
                    layer=edge.get("layer", "L3"),
                    confidence=edge.get("confidence"),
                    condition=edge.get("condition"),
                    provenance=edge.get("provenance"),
                    challenge_of=edge.get("challenge_of"),
                    challenge_type=edge.get("challenge_type"),
                    urgency=edge.get("urgency"),
                    probability=edge.get("probability"),
                    probability_p05=edge.get("probability_p05"),
                    probability_p50=edge.get("probability_p50"),
                    probability_p95=edge.get("probability_p95"),
                    confidence_p05=edge.get("confidence_p05"),
                    confidence_p50=edge.get("confidence_p50"),
                    confidence_p95=edge.get("confidence_p95"),
                    agent_name=agent,
                )
                edges_created += 1
            self.current_store.conn.execute("COMMIT")
        except Exception:
            self.current_store.conn.execute("ROLLBACK")
            raise

        self._json_response(
            201,
            {
                "nodes_created": nodes_created,
                "edges_created": edges_created,
                "errors": errors,
            },
        )

    def _post_webhook(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /webhook — register or update webhook callback URL for this agent."""
        import json as _json
        from ohm.exceptions import ValidationError

        url = body.get("url", "")
        events = body.get("events", ["node.created", "node.updated", "edge.created"])
        if not url:
            raise ValidationError("Webhook requires a 'url' field")
        _server_module._validate_webhook_url(url)
        # OHM-whbk: persist to DuckDB so registrations survive restarts.
        # Single-tenant mode uses customer_id="" as the key.
        customer_id = self._customer_id or ""
        events_json = _json.dumps(list(events))
        self.current_store.conn.execute(
            """
            INSERT INTO ohm_webhook_subscriptions (customer_id, agent, url, events, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (customer_id, agent) DO UPDATE SET
                url = excluded.url,
                events = excluded.events,
                updated_at = CURRENT_TIMESTAMP
            """,
            [customer_id, agent, url, events_json],
        )
        with _server_module._webhook_lock:
            if self._customer_id not in _server_module._webhook_registry:
                _server_module._webhook_registry[self._customer_id] = {}
            _server_module._webhook_registry[self._customer_id][agent] = {"url": url, "events": events}
        self._json_response(
            200,
            {
                "status": "registered",
                "agent": agent,
                "url": url,
                "events": events,
            },
        )

    def _post_state(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /state — update agent state/focus."""
        result = self.current_store.update_agent_state(
            current_focus=body.get("focus"),
            active_patterns=body.get("patterns"),
            available_services=body.get("services"),
            session_id=body.get("session_id"),
            agent_name=agent,
        )
        self._json_response(200, result)

    def _post_register(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /register — agent registration (idempotent: creates or updates agent node + edges)."""
        from ohm.queries import create_edge, find_or_create_node
        import re

        agent_label = body.get("name", agent)
        agent_id = "agent_" + re.sub(r"[^a-zA-Z0-9]+", "_", agent_label.lower()).strip("_")

        existing_active = self.current_store.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id]).fetchone()
        existing_soft_deleted = self.current_store.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [agent_id]).fetchone()

        if existing_active:
            self.current_store.conn.execute(
                "UPDATE ohm_nodes SET content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                [body.get("description"), agent, agent_id],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
            reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
            placeholders = ",".join(["?"] * len(reg_edge_types))
            self.current_store.conn.execute(
                f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                [agent_id] + list(reg_edge_types),
            )
        elif existing_soft_deleted:
            self.current_store.conn.execute(
                """UPDATE ohm_nodes SET
                    content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ?,
                    deleted_at = NULL
                WHERE id = ?""",
                [body.get("description"), agent, agent_id],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
            reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
            placeholders = ",".join(["?"] * len(reg_edge_types))
            self.current_store.conn.execute(
                f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                [agent_id] + list(reg_edge_types),
            )
        else:
            self.current_store.conn.execute(
                """INSERT INTO ohm_nodes
                   (id, label, type, content, created_by, confidence, visibility, created_at, updated_at)
                   VALUES (?, ?, 'agent', ?, ?, 1.0, 'team', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                [agent_id, agent_label, body.get("description"), agent],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]

        created_edges = []
        for v in body.get("values", []):
            value_node = find_or_create_node(
                self.current_store.conn,
                label=v,
                node_type="value",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=value_node["id"],
                edge_type="VALUES",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for g in body.get("goals", []):
            goal_node = find_or_create_node(
                self.current_store.conn,
                label=g,
                node_type="goal",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=goal_node["id"],
                edge_type="GOALS",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for c in body.get("capabilities", []):
            cap_node = find_or_create_node(
                self.current_store.conn,
                label=c,
                node_type="skill",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=cap_node["id"],
                edge_type="CAPABLE_OF",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for i in body.get("interests", []):
            topic_node = find_or_create_node(
                self.current_store.conn,
                label=i,
                node_type="topic",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=topic_node["id"],
                edge_type="INTERESTED_IN",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for a in body.get("listens_to", []):
            other = find_or_create_node(
                self.current_store.conn,
                label=a,
                node_type="agent",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=other["id"],
                edge_type="LISTENS_TO",
                layer="L3",
                created_by=agent,
                confidence=0.7,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        self._json_response(
            201,
            {
                "agent": me,
                "edges_created": len(created_edges),
            },
        )

    def _post_sync(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /sync — explicit DuckLake sync trigger (OHM-7301)."""
        sync_result = self.current_store.sync_heartbeat()
        self._json_response(200, sync_result)

    def _post_task(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /tasks — create a task node (OHM-7304)."""
        import re
        import uuid

        task_id = body.get("id") or ("task_" + re.sub(r"[^a-z0-9]+", "_", body["label"].lower()).strip("_")[:48] + "_" + str(uuid.uuid4())[:8])

        # OHM-tjzh: tasks are derived claims (action items derived from context).
        # They must link to existing structure. The synthesized body mirrors
        # what /node would see so the same enforcement path runs.
        synthesized_body = dict(body)
        synthesized_body["id"] = task_id
        synthesized_body.setdefault("type", "task")
        cross_link_error = self._enforce_cross_link_requirement(task_id, synthesized_body)
        if cross_link_error is not None:
            self._json_response(422, cross_link_error)
            return

        result = self.current_store.write_node(
            id=task_id,
            label=body["label"],
            type="task",
            content=body.get("content"),
            confidence=body.get("confidence", 1.0),
            visibility=body.get("visibility", "team"),
            provenance=body.get("provenance"),
            tags=body.get("tags"),
            metadata=body.get("metadata"),
            priority=body.get("priority"),
            url=body.get("url"),
            task_status=body.get("task_status", "open"),
            assigned_to=body.get("assigned_to"),
            due_date=body.get("due_date"),
            utility_usd_per_day=body.get("utility_usd_per_day"),
            utility_currency=body.get("utility_currency"),
            agent_name=agent,
        )
        _server_module._trigger_webhooks({"type": "task.created", "agent": agent, "node": result}, customer_id=self._customer_id)
        if result.get("created", True):
            self._json_response(201, result)
        else:
            self._json_response(200, result)

    def _post_task_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /tasks/<id>/... — task sub-actions (OHM-f5iq).

        Currently dispatches ``/tasks/<id>/outcome`` to close a task with a
        recorded outcome. Returns 404 for unknown sub-paths.
        """
        from ohm.exceptions import ValidationError
        from ohm.validation import validate_identifier

        sub = path[len("/tasks/") :]
        parts = sub.split("/", 1)
        if len(parts) != 2 or parts[1] != "outcome":
            self._json_response(404, {"error": "unknown_task_action", "path": path})
            return
        task_id = validate_identifier(parts[0], name="task_id")

        outcome = body.get("outcome")
        notes = body.get("notes")
        claim_node = body.get("claim_node")
        if outcome is None:
            raise ValidationError("outcome is required (TRUE, FALSE, or AMBIGUOUS)")

        from ohm.graph.queries import query_close_task_with_outcome

        result = query_close_task_with_outcome(
            self.current_store.conn,
            task_id=task_id,
            outcome=str(outcome),
            recorded_by=agent,
            notes=notes,
            claim_node=claim_node,
        )
        _server_module._trigger_webhooks(
            {"type": "task.completed", "agent": agent, "task": result["task"], "outcome": result["outcome"]},
            customer_id=self._customer_id,
        )
        self._json_response(200, result)

    def _post_skill(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /skill — Create a portable skill node (OHM-461f).

        Body:
            label (required): Human-readable skill name
            trigger (required): When this skill activates
            scope (optional): personal (default), project, or universal
            required_tools (optional): List of tool names
            boundaries (optional): Constraints on what the skill does
            output_format (optional): Expected output format
            verification_evidence (optional): List of evidence types
            connects_to (optional): List of existing node IDs to link
        """
        from ohm.queries import create_skill
        from ohm.exceptions import ValidationError

        label = body.get("label")
        trigger = body.get("trigger")
        if not label or not trigger:
            raise ValidationError("label and trigger are required")

        skill = create_skill(
            self.current_store.conn,
            label=label,
            trigger=trigger,
            scope=body.get("scope", "personal"),
            required_tools=body.get("required_tools", []),
            boundaries=body.get("boundaries"),
            output_format=body.get("output_format"),
            verification_evidence=body.get("verification_evidence", []),
            connects_to=body.get("connects_to", []),
            created_by=agent,
        )
        self._json_response(201, skill)

    def _post_runbook(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /runbook — Create an ordered chain of skills (OHM-461f).

        Body:
            label (required): Human-readable runbook name
            skill_ids (required): Ordered list of existing skill node IDs
            description (optional): Free-text description
        """
        from ohm.queries import create_runbook
        from ohm.exceptions import ValidationError

        label = body.get("label")
        skill_ids = body.get("skill_ids", [])
        if not label:
            raise ValidationError("label is required")
        if not skill_ids or not isinstance(skill_ids, list):
            raise ValidationError("skill_ids must be a non-empty list")

        runbook = create_runbook(
            self.current_store.conn,
            label=label,
            skill_ids=skill_ids,
            description=body.get("description"),
            created_by=agent,
        )
        self._json_response(201, runbook)

    def _get_runbook_steps(self, path: str, qs: dict) -> None:
        """GET /runbook/{id}/steps — Get ordered skills in a runbook (OHM-461f)."""
        from ohm.queries import get_runbook_steps
        from ohm.exceptions import NodeNotFoundError, ValidationError

        prefix = "/runbook/"
        suffix = "/steps"
        if not path.endswith(suffix):
            raise ValidationError("Path must end with /steps")
        runbook_id = path[len(prefix) : -len(suffix)]
        if not runbook_id:
            raise ValidationError("runbook_id is required")

        try:
            result = get_runbook_steps(self.current_store.conn, runbook_id=runbook_id)
            self._json_response(200, result)
        except NodeNotFoundError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})

    def _get_vault(self, path: str, qs: dict) -> None:
        """GET /vault — list vault contents for the authenticated agent (OHM-cuu0).

        Returns nodes with ``visibility='vault'`` created by the authenticated
        agent, plus any edges attached to those nodes.
        """
        agent = self._authenticate()
        if agent is None:
            if self.no_auth:
                agent = "ohm"
            else:
                raise AuthenticationError(  # noqa: F821
                    "Authentication required"
                )
        nodes = self.current_store.execute(
            "SELECT * FROM ohm_nodes WHERE visibility = 'vault' AND created_by = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 100",
            [agent],
        )
        node_ids = [n["id"] for n in nodes]
        edges: list = []
        if node_ids:
            placeholders = ",".join(["?"] * len(node_ids))
            edges = self.current_store.execute(
                f"SELECT * FROM ohm_edges WHERE (from_node IN ({placeholders}) OR to_node IN ({placeholders})) AND deleted_at IS NULL",
                node_ids + node_ids,
            )
        self._json_response(200, {"agent": agent, "nodes": nodes, "edges": edges, "count": len(nodes)})

    def _post_vault_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /vault/promote — promote a vault node to the shared graph (OHM-cuu0).

        Changes ``visibility`` from ``vault`` to ``team`` for the given node
        and its edges (if any). Only the owning agent can promote their own
        vault content.

        Body: {"node_id": "<node_id>"}
        """
        node_id = body.get("node_id", "")
        if not node_id:
            self._json_response(400, {"error": "validation_error", "message": "node_id is required"})
            return

        node = self.current_store.conn.execute(
            "SELECT id, visibility, created_by FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        if not node:
            self._json_response(404, {"error": "not_found", "message": f"Node not found: {node_id}"})
            return
        nid, vis, creator = node

        if vis != "vault":
            self._json_response(400, {"error": "validation_error", "message": f"Node {node_id} has visibility '{vis}', not 'vault'"})
            return

        # OHM-tjzh: promotion requires at least one cross-link to shared graph
        from ohm.schema import requires_cross_link

        if requires_cross_link(node["type"] if len(node) > 3 else "concept"):
            edge_count = self.current_store.conn.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
                [node_id, node_id],
            ).fetchone()[0]
            if edge_count == 0:
                self._json_response(
                    422,
                    {
                        "error": "cross_link_required",
                        "message": f"Vault node '{node_id}' has no edges. Per ADR-018 / OHM-tjzh, nodes must have at least one edge before promotion to the shared graph.",
                        "hint": "Add an edge to an existing shared-graph node via POST /edge, then retry promotion.",
                    },
                )
                return

        now = self.current_store._now()
        self.current_store.conn.execute(
            "UPDATE ohm_nodes SET visibility = 'team', updated_at = ?, updated_by = ? WHERE id = ?",
            [now, agent, node_id],
        )
        # Also promote related edges
        self.current_store.conn.execute(
            "UPDATE ohm_edges SET updated_at = ?, updated_by = ? WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
            [now, agent, node_id, node_id],
        )

        self._json_response(
            200,
            {
                "promoted": node_id,
                "previous_visibility": "vault",
                "new_visibility": "team",
                "promoted_by": agent,
            },
        )

    def _post_heartbeat(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /heartbeat — agent heartbeat with sync and orient enrichment.

        ADR-023: Heartbeat now includes orient data, contradictions, stale observations,
        and anomalies so agents see what needs attention without extra API calls.
        """
        from ohm.methods import agent_heartbeat
        from ohm.server.suggestions import generate_island_nudge

        result = agent_heartbeat(
            self.current_store.conn,
            agent,
            focus=body.get("focus"),
        )
        sync_result = self.current_store.sync_heartbeat()
        result["ducklake_sync"] = sync_result

        # OHM-tr71.4: Island isolation nudge in heartbeat
        try:
            island = generate_island_nudge(self.current_store, agent)
            if island:
                result["island_warning"] = island["island_warning"]
        except Exception as exc:
            logger.debug("Heartbeat island nudge failed: %s", exc)

        # OHM-jx4q: Orphan rate nudge in heartbeat
        try:
            from ohm.queries import query_graph_health

            health = query_graph_health(self.current_store.conn)
            total_nodes = health.get("total_nodes") or 0
            orphans = health.get("orphan_nodes") or 0
            orphan_rate = round(orphans / total_nodes, 4) if total_nodes else 0
            if orphan_rate > 0.10:
                result["orphan_rate_warning"] = {
                    "orphan_rate": orphan_rate,
                    "orphan_count": orphans,
                    "total_nodes": total_nodes,
                    "orphan_type_breakdown": health.get("orphan_type_breakdown", {}),
                    "triage_endpoint": "GET /admin/orphan-triage",
                }
        except Exception as exc:
            logger.debug("Heartbeat orphan rate nudge failed: %s", exc)

        # ADR-023: Proactive orient enrichment
        try:
            orient = self._get_orient_data(agent)
            if orient:
                result["orient"] = orient
        except Exception as exc:
            logger.debug("Heartbeat orient enrichment failed: %s", exc)

        # ADR-023: Proactive contradictions (limit 3)
        try:
            contradictions = self._get_contradictions_data(limit=3)
            if contradictions:
                result["contradictions"] = contradictions
        except Exception as exc:
            logger.debug("Heartbeat contradictions enrichment failed: %s", exc)

        # ADR-023: Stale observations nudge
        try:
            stale = self._get_stale_data(days=7, limit=3)
            if stale:
                result["stale_observations"] = stale
        except Exception as exc:
            logger.debug("Heartbeat stale enrichment failed: %s", exc)

        self._json_response(200, result)

    def _get_orient_data(self, agent: str) -> dict | None:
        """Lightweight orient data for heartbeat enrichment."""
        try:
            conn = self.current_store.read_conn

            _hours = 24  # noqa: F841
            # Last activity
            last_activity = conn.execute(
                "SELECT MAX(la) FROM (SELECT created_at AS la FROM ohm_nodes WHERE created_by = ? UNION ALL SELECT created_at AS la FROM ohm_edges WHERE created_by = ? UNION ALL SELECT created_at AS la FROM ohm_observations WHERE created_by = ?)",
                [agent, agent, agent],
            ).fetchone()[0]
            # Open tasks
            tasks = conn.execute(
                "SELECT id, label, priority, due_date FROM ohm_nodes WHERE assigned_to = ? AND task_status = 'open' AND deleted_at IS NULL ORDER BY priority DESC LIMIT 5",
                [agent],
            ).fetchall()
            return {
                "last_activity": str(last_activity) if last_activity else None,
                "open_tasks": len(tasks),
                "task_summaries": [{"id": t[0], "label": t[1], "priority": t[2]} for t in tasks[:3]],
            }
        except Exception:
            return None

    def _get_contradictions_data(self, limit: int = 3) -> list | None:
        """Lightweight contradictions for heartbeat enrichment."""
        try:
            from ohm.methods import detect_contradictions

            result = detect_contradictions(self.current_store.read_conn, confidence_threshold=0.5)
            if isinstance(result, list):
                return result[:limit]
            return None
        except Exception:
            return None

    def _get_stale_data(self, days: int = 7, limit: int = 3) -> list | None:
        """Lightweight stale observations for heartbeat enrichment."""
        try:
            from ohm.queries import query_stale_edges

            result = query_stale_edges(self.current_store.read_conn, stale_threshold=0.1)
            if isinstance(result, list):
                return result[:limit]
            return None
        except Exception:
            return None

    def _post_deduplicate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /deduplicate — remove duplicate edges (same from→to, type, layer), keeping most recent."""
        from ohm.exceptions import ValidationError

        layer = qs.get("layer", [None])[0]
        if layer:
            from ohm.validation import validate_layer

            try:
                validate_layer(layer)
            except ValueError as e:
                raise ValidationError(str(e))
        removed = self.current_store.deduplicate_edges(layer=layer)
        self._json_response(200, {"removed": removed, "layer": layer})

    def _delete_node(self, path: str, agent: str) -> None:
        """DELETE /node/{id} — removes a node and its associated edges."""
        from ohm.exceptions import NodeNotFoundError

        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")

        node = self.current_store.conn.execute(
            "SELECT id, created_by FROM ohm_nodes WHERE id = ?",
            [node_id],
        ).fetchone()
        if not node:
            raise NodeNotFoundError(f"Node not found: {node_id}")

        result = self.current_store.delete_node(node_id, deleted_by=agent)
        self._json_response(200, result)

    def _delete_edge(self, path: str, agent: str) -> None:
        """DELETE /edge/{id} — removes an edge."""
        from ohm.exceptions import EdgeNotFoundError

        edge_id = path[6:]
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")

        edge = self.current_store.conn.execute(
            "SELECT id, created_by FROM ohm_edges WHERE id = ?",
            [edge_id],
        ).fetchone()
        if not edge:
            raise EdgeNotFoundError(f"Edge not found: {edge_id}")

        result = self.current_store.delete_edge(edge_id, deleted_by=agent)
        self._json_response(200, result)

    def _post_ask_synthesis(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /ask — conversational analytics: natural language question → synthesized insights.

        Converts OHM's AND-gate interface into an OR-gate by orchestrating
        search, neighborhood expansion, Bayesian inference, and challenge
        checking into a single structured response.

        Pipeline:
          1. Node search (text + semantic) to find relevant nodes
          2. Neighborhood expansion for top matches
          3. Bayesian inference on causal chains (optional)
          4. Challenge check for relevant edges
          5. Synthesis combining all results

        Input:
          question (required): Natural language question
          agent: Requesting agent name (defaults to authenticated agent)
          depth: Neighborhood depth, 1-3 (default 2)
          include_inference: Run Bayesian inference (default true)
          limit: Max search results per method (default 5)
        """
        from ohm.queries import search, semantic_search, query_neighborhood
        from ohm.bayesian import bayesian_inference, PGMPY_AVAILABLE
        from ohm.validation import validate_identifier

        question = body.get("question", "").strip()
        if not question:
            self._json_response(400, {"error": "missing_parameter", "message": "'question' is required"})
            return

        depth = min(max(int(body.get("depth", 2)), 1), 3)
        include_inference = body.get("include_inference", True)
        limit = min(max(int(body.get("limit", 5)), 1), 20)
        # Step 1: Node search — text + semantic
        matched_nodes = []
        search_errors = []

        # Direct node ID lookup — if the question contains a known node ID, use it
        question_lower = question.lower().replace(" ", "_").replace("-", "_")
        try:
            # Check if question matches an existing node ID directly
            direct_node = self.current_store.get_node(question_lower)
            if direct_node:
                matched_nodes.append(
                    {
                        "id": direct_node["id"],
                        "label": direct_node.get("label", ""),
                        "type": direct_node.get("type", ""),
                        "confidence": direct_node.get("confidence"),
                        "match_method": "direct_id",
                    }
                )
        except Exception:
            pass

        # Also try common variations (hormuz and gate → hormuz_and_gate)
        if not matched_nodes:
            for variant in [question_lower, question_lower.replace(" and ", "_and_").replace(" ", "_")]:
                try:
                    node = self.current_store.get_node(variant)
                    if node and node["id"] not in {n["id"] for n in matched_nodes}:
                        matched_nodes.append(
                            {
                                "id": node["id"],
                                "label": node.get("label", ""),
                                "type": node.get("type", ""),
                                "confidence": node.get("confidence"),
                                "match_method": "direct_id",
                            }
                        )
                        break
                except Exception:
                    pass

        # Text search
        try:
            text_results = search(
                self.current_store.conn,
                query=question,
                limit=limit,
            )
            for r in text_results:
                matched_nodes.append(
                    {
                        "id": r.get("id", ""),
                        "label": r.get("label", ""),
                        "type": r.get("type", ""),
                        "confidence": r.get("confidence"),
                        "match_method": "text",
                    }
                )
        except Exception as e:
            search_errors.append(f"text_search: {e}")

        # Semantic search
        try:
            sem_results = semantic_search(
                self.current_store.conn,
                query=question,
                limit=limit,
            )
            # Merge: add semantic results that aren't already in matched_nodes
            existing_ids = {n["id"] for n in matched_nodes}
            for r in sem_results:
                nid = r.get("node_id", r.get("id", ""))
                if nid and nid not in existing_ids:
                    matched_nodes.append(
                        {
                            "id": nid,
                            "label": r.get("label", ""),
                            "type": r.get("type", ""),
                            "confidence": r.get("confidence"),
                            "distance": r.get("distance"),
                            "match_method": "semantic",
                        }
                    )
                    existing_ids.add(nid)
        except Exception as e:
            # Semantic search may be unavailable (no Ollama)
            search_errors.append(f"semantic_search: {e}")

        # Fuzzy search fallback
        if not matched_nodes:
            try:
                from ohm.graph.queries import fuzzy_search

                fuzzy_results = fuzzy_search(
                    self.current_store.conn,
                    query=question,
                    limit=limit,
                )
                existing_ids = {n["id"] for n in matched_nodes}
                for r in fuzzy_results:
                    nid = r.get("id", "")
                    if nid and nid not in existing_ids:
                        matched_nodes.append(
                            {
                                "id": nid,
                                "label": r.get("label", ""),
                                "type": r.get("type", ""),
                                "confidence": r.get("confidence"),
                                "distance": r.get("distance"),
                                "match_method": r.get("match_type", "fuzzy"),
                            }
                        )
                        existing_ids.add(nid)
            except Exception as e:
                search_errors.append(f"fuzzy_search: {e}")

        # Step 2: Neighborhood expansion for top matches
        all_node_ids = set()
        all_edges = []
        node_details = []
        for node in matched_nodes[:limit]:
            nid = node["id"]
            if not nid:
                continue
            all_node_ids.add(nid)
            try:
                n_edges = query_neighborhood(
                    self.current_store.conn,
                    nid,
                    depth=depth,
                )
                for edge in n_edges:
                    all_node_ids.add(edge.get("from_node", edge.get("from", "")))
                    all_node_ids.add(edge.get("to_node", edge.get("to", "")))
                    all_edges.append(edge)
            except Exception:
                pass

        # Fetch node details for all discovered nodes
        if all_node_ids:
            placeholders = ",".join(["?"] * len(all_node_ids))
            node_details = self.current_store.execute(
                f"SELECT id, label, type, confidence, content, tags, created_by, provenance FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                list(all_node_ids),
            )

        # Step 3: Bayesian inference on causal chains
        inference_results = {}
        inference_errors = []
        if include_inference and PGMPY_AVAILABLE and matched_nodes:
            # Find nodes with causal edges (CAUSES, DEPENDS_ON, THREATENS, NEGATES)
            target_ids = [n["id"] for n in matched_nodes if n.get("id")]
            if target_ids:
                placeholders = ",".join(["?"] * len(target_ids))
                # Find causal edges involving our matched nodes
                causal_edges = self.current_store.execute(
                    f"""SELECT DISTINCT from_node, to_node, edge_type, confidence, probability
                       FROM ohm_edges
                       WHERE (from_node IN ({placeholders}) OR to_node IN ({placeholders}))
                         AND edge_type IN ('CAUSES', 'DEPENDS_ON', 'THREATENS', 'NEGATES')
                         AND deleted_at IS NULL
                       LIMIT 50""",
                    target_ids + target_ids,
                )

                if causal_edges:
                    # Build evidence from observed nodes (high-confidence observations)
                    # OHM-w1iv.2: batch the latest probability observation for all targets.
                    if target_ids:
                        placeholders = ",".join(["?"] * len(target_ids))
                        obs_rows = self.current_store.execute(
                            f"""SELECT node_id, value FROM (
                                SELECT node_id, value,
                                    ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY created_at DESC) AS rn
                                FROM ohm_observations
                                WHERE node_id IN ({placeholders})
                                  AND type = 'probability'
                                  AND deleted_at IS NULL
                            ) WHERE rn = 1""",
                            target_ids,
                        )
                        evidence = {}
                        for row in obs_rows:
                            try:
                                val = float(row["value"])
                                if 0.0 <= val <= 1.0:
                                    evidence[row["node_id"]] = 1 if val >= 0.5 else 0
                            except (ValueError, TypeError, KeyError):
                                pass

                    # Run inference on each matched node that has causal connections
                    for target_id in target_ids[:3]:  # Limit to top 3 to avoid timeouts
                        try:
                            target_safe = validate_identifier(target_id, name="target")
                            result = bayesian_inference(
                                self.current_store.conn,
                                target_safe,
                                evidence,
                                customer_id=self._customer_id,
                            )
                            if "error" not in result:
                                # ADR-025: Extract only posteriors, not full network info
                                posterior = result.get("posterior", result)
                                network_info = result.get("network_info", {})
                                inference_results[target_safe] = {
                                    "posterior": posterior,
                                    "n_nodes": network_info.get("n_nodes", 0),
                                    "n_edges": network_info.get("n_edges", 0),
                                    "method": result.get("method", "bayesian_variable_elimination"),
                                }
                        except Exception as e:
                            inference_errors.append(f"inference({target_id}): {e}")

        # Step 4: Challenge check for relevant edges
        challenges = []
        challenge_node_ids = list(all_node_ids)[:50]  # Limit to prevent runaway queries
        if challenge_node_ids:
            placeholders = ",".join(["?"] * len(challenge_node_ids))
            challenge_edges = self.current_store.execute(
                f"""SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
                          e.challenge_type, e.provenance, e.created_by,
                          n_from.label AS from_label, n_to.label AS to_label
                   FROM ohm_edges e
                   LEFT JOIN ohm_nodes n_from ON e.from_node = n_from.id
                   LEFT JOIN ohm_nodes n_to ON e.to_node = n_to.id
                   WHERE e.edge_type = 'CHALLENGED_BY'
                     AND (e.to_node IN ({placeholders}) OR e.from_node IN ({placeholders}))
                     AND e.deleted_at IS NULL
                   LIMIT 20""",
                challenge_node_ids + challenge_node_ids,
            )
            for ce in challenge_edges:
                challenges.append(
                    {
                        "edge_id": ce.get("id"),
                        "challenger_node": ce.get("from_node"),
                        "challenged_node": ce.get("to_node"),
                        "challenger_label": ce.get("from_label", ""),
                        "challenged_label": ce.get("to_label", ""),
                        "challenge_type": ce.get("challenge_type"),
                        "confidence": ce.get("confidence"),
                        "provenance": ce.get("provenance"),
                        "created_by": ce.get("created_by"),
                    }
                )

        # Step 5: Build synthesis
        # Confidence based on: search match quality + inference certainty + challenge coverage
        confidence = 0.5
        match_count = len(matched_nodes)
        if match_count >= 3:
            confidence += 0.15
        elif match_count >= 1:
            confidence += 0.1

        # Boost if semantic matches are close
        semantic_matches = [n for n in matched_nodes if n.get("match_method") == "semantic"]
        if semantic_matches:
            min_dist = min((n.get("distance", 1.0) for n in semantic_matches), default=1.0)
            if min_dist < 0.3:
                confidence += 0.1
            elif min_dist < 0.5:
                confidence += 0.05

        # Boost if inference converged
        if inference_results:
            for target_id, inf in inference_results.items():
                posterior = inf.get("posterior", {}).get(target_id, {})
                if posterior:
                    max_prob = max(posterior.get("good", 0), posterior.get("bad", 0))
                    confidence += 0.1 * max_prob  # Higher certainty → more confidence

        # Reduce if challenges exist on key edges
        if challenges:
            challenge_count = len(challenges)
            confidence -= 0.05 * min(challenge_count, 3)

        confidence = max(0.1, min(1.0, round(confidence, 2)))

        # Build synthesis text from gathered context
        synthesis_parts = []

        if matched_nodes:
            node_labels = [f"{n['label']} ({n['id']})" for n in matched_nodes[:5] if n.get("label")]
            if node_labels:
                synthesis_parts.append(f"Relevant nodes: {', '.join(node_labels)}.")

        if inference_results:
            for target_id, inf in inference_results.items():
                posterior = inf.get("posterior", {}).get(target_id, {})
                if posterior:
                    p_good = posterior.get("good", 0)
                    p_bad = posterior.get("bad", 0)
                    synthesis_parts.append(f"Bayesian inference on {target_id}: P(good)={p_good:.2%}, P(bad)={p_bad:.2%}.")

        if challenges:
            challenge_descs = []
            for c in challenges[:3]:
                cdesc = f"{c.get('challenger_label', c.get('challenger_node', '?'))} challenges {c.get('challenged_label', c.get('challenged_node', '?'))}"
                if c.get("challenge_type"):
                    cdesc += f" ({c['challenge_type']})"
                challenge_descs.append(cdesc)
            synthesis_parts.append(f"Active challenges: {'; '.join(challenge_descs)}.")

        if not synthesis_parts:
            synthesis_parts.append(f"No matching nodes or inference results found for '{question}'.")

        synthesis = " ".join(synthesis_parts)

        # Source node IDs for traceability
        sources = list({n["id"] for n in matched_nodes if n.get("id")})[:20]

        response = {
            "question": question,
            "matched_nodes": matched_nodes[:20],
            "neighborhood": {
                "nodes": node_details[:50],
                "edges": all_edges[:100],
            },
            "inference_results": inference_results,
            "challenges": challenges,
            "synthesis": synthesis,
            "confidence": confidence,
            "sources": sources,
        }

        if inference_errors:
            response["inference_errors"] = inference_errors
        if search_errors:
            response["search_errors"] = search_errors
        if not PGMPY_AVAILABLE:
            response["inference_skipped"] = True
            response["inference_reason"] = "pgmpy not available"
        if not include_inference:
            response["inference_skipped"] = True
            response["inference_reason"] = "include_inference=false"

        self._json_response(200, response)

    def _post_register_twin(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/register — register an external domain twin (OHM-josq).

        Body: {label, target_node_id, endpoint_url?, description?, connects_to?}
        """
        from ohm.queries import register_twin
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        target_node_id = body.get("target_node_id")
        if not label:
            raise ValidationError("label is required")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = register_twin(
                self.current_store.conn,
                label=label,
                target_node_id=target_node_id,
                created_by=agent,
                endpoint_url=body.get("endpoint_url"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_register_twin_with_bindings(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/register-with-bindings — register twin with bindings (OHM-f7tl).

        Body: {label, target_node_id, decision_node_id?, feed_node_ids?,
               model_candidate_ids?, description?, endpoint_url?}
        """
        from ohm.queries import register_twin_with_bindings
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        target_node_id = body.get("target_node_id")
        if not label:
            raise ValidationError("label is required")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = register_twin_with_bindings(
                self.current_store.conn,
                label=label,
                target_node_id=target_node_id,
                decision_node_id=body.get("decision_node_id"),
                feed_node_ids=body.get("feed_node_ids"),
                model_candidate_ids=body.get("model_candidate_ids"),
                created_by=agent,
                description=body.get("description"),
                endpoint_url=body.get("endpoint_url"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_add_twin_bindings(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/add-bindings — add/remove feed bindings (OHM-f7tl).

        Body: {feed_node_ids?, feed_node_ids_remove?}
        """
        from ohm.queries import add_twin_bindings
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        try:
            result = add_twin_bindings(
                self.current_store.conn,
                twin_id=twin_id,
                feed_node_ids=body.get("feed_node_ids"),
                feed_node_ids_remove=body.get("feed_node_ids_remove"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_attach_twin_models(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/attach-models — attach/detach model candidates (OHM-f7tl).

        Body: {model_candidate_ids?, model_candidate_ids_remove?}
        """
        from ohm.queries import attach_twin_models
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        try:
            result = attach_twin_models(
                self.current_store.conn,
                twin_id=twin_id,
                model_candidate_ids=body.get("model_candidate_ids"),
                model_candidate_ids_remove=body.get("model_candidate_ids_remove"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_predict(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/predict — twin predictions as edge_overrides (OHM-josq)."""
        from ohm.queries import twin_predict
        from ohm.exceptions import NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("twin_id is required in path")

        try:
            result = twin_predict(self.current_store.read_conn, twin_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_constraints(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/constraints — twin constraints (OHM-josq)."""
        from ohm.queries import twin_constraints
        from ohm.exceptions import NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("twin_id is required in path")

        try:
            result = twin_constraints(self.current_store.read_conn, twin_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_validate_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """Dispatch /twin/{id}/{validate-action|auto-promote|add-bindings|attach-models} to the right handler (OHM-josq, OHM-75tw, OHM-f7tl)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /twin/{id}/{validate-action|auto-promote|add-bindings|attach-models} required")
        action = parts[2]
        if action == "validate-action":
            self._post_twin_validate_action(path, qs, body, agent)
        elif action == "auto-promote":
            self._post_auto_promote(path, qs, body, agent)
        elif action == "add-bindings":
            self._post_add_twin_bindings(path, qs, body, agent)
        elif action == "attach-models":
            self._post_attach_twin_models(path, qs, body, agent)
        else:
            raise ValidationError(f"unknown twin POST action: {action}")

    def _post_twin_validate_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/validate-action — validate action against twin constraints (OHM-josq).

        Body: {action_id}
        """
        from ohm.queries import validate_action_against_twin
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        action_id = body.get("action_id")
        if not action_id:
            raise ValidationError("action_id is required")

        try:
            result = validate_action_against_twin(
                self.current_store.conn,
                twin_id=twin_id,
                action_id=action_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_explain(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/explain — explain what the twin models (OHM-josq)."""
        from ohm.queries import explain_twin
        from ohm.exceptions import NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("twin_id is required in path")

        try:
            result = explain_twin(self.current_store.read_conn, twin_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_readiness(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/readiness — check twin readiness gates (OHM-f7tl).

        Optional query params:
          - freshness_days (int): override the default 7-day feed
            freshness window. When set, the response distinguishes
            "no threshold set" from "threshold exceeded" (kg16 item 4).
        """
        from ohm.queries import get_twin_readiness
        from ohm.exceptions import NodeNotFoundError, ValidationError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        freshness_days: int | None = None
        raw_days = qs.get("freshness_days", [None])[0]
        if raw_days is not None:
            try:
                freshness_days = int(raw_days)
            except (TypeError, ValueError) as e:
                raise ValidationError(f"freshness_days must be an integer, got {raw_days!r}") from e

        try:
            result = get_twin_readiness(
                self.current_store.read_conn,
                twin_id=twin_id,
                freshness_days=freshness_days,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _route_twin_get(self, path: str, qs: dict) -> None:
        """Dispatch /twin/{id}/{action} to the right handler (OHM-josq, OHM-bf45)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /twin/{id}/{predict|constraints|explain|drift|ensemble} required")
        parts[1]
        action = parts[2]
        if action == "predict":
            self._get_twin_predict(path, qs)
        elif action == "constraints":
            self._get_twin_constraints(path, qs)
        elif action == "explain":
            self._get_twin_explain(path, qs)
        elif action == "drift":
            self._get_detect_drift(path, qs)
        elif action == "ensemble":
            self._get_ensemble_predict(path, qs)
        elif action == "readiness":
            self._get_twin_readiness(path, qs)
        else:
            raise ValidationError(f"unknown twin action: {action}")

    def _post_create_twin_template(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin-template — create a twin template (OHM-hl61).

        Body: {label, target_node_id, constraint_schema?, required_edges?, description?, connects_to?}
        """
        from ohm.queries import create_twin_template
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        target_node_id = body.get("target_node_id")
        if not label:
            raise ValidationError("label is required")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = create_twin_template(
                self.current_store.conn,
                label=label,
                target_node_id=target_node_id,
                created_by=agent,
                constraint_schema=body.get("constraint_schema"),
                required_edges=body.get("required_edges"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_templates(self, path: str, qs: dict) -> None:
        """GET /twin-templates — list twin templates (OHM-hl61).

        Filters: ?target_node_id=, ?created_by=, ?limit=
        """
        from ohm.queries import list_twin_templates

        target_node_id = qs.get("target_node_id", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [50])[0])

        result = list_twin_templates(
            self.current_store.read_conn,
            target_node_id=target_node_id,
            created_by=created_by,
            limit=limit,
        )
        self._json_response(200, {"ok": True, "data": result})

    def _get_twin_template(self, path: str, qs: dict) -> None:
        """GET /twin-template/{id} — get a twin template (OHM-hl61)."""
        from ohm.queries import get_twin_template
        from ohm.exceptions import NodeNotFoundError, ValidationError

        parts = path.strip("/").split("/")
        template_id = parts[1] if len(parts) >= 2 else None
        if not template_id:
            raise ValidationError("template_id is required in path")

        try:
            result = get_twin_template(self.current_store.read_conn, template_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_instantiate_twin(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin-template/{id}/instantiate — instantiate a twin from template (OHM-hl61).

        Body: {target_node_id, label?, connects_to?}
        """
        from ohm.queries import instantiate_twin_from_template
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        template_id = parts[1] if len(parts) >= 2 else None
        if not template_id:
            raise ValidationError("template_id is required in path")

        target_node_id = body.get("target_node_id")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = instantiate_twin_from_template(
                self.current_store.conn,
                template_id=template_id,
                target_node_id=target_node_id,
                created_by=agent,
                label=body.get("label"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_assemble_twin(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/assemble — assemble a decision-specific twin (OHM-f7tl).

        Body: {decision_node_id, goal, horizon?, preferred_template_id?, preferred_model_id?}
        """
        from ohm.queries import assemble_twin_for_decision
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_node_id = body.get("decision_node_id")
        goal = body.get("goal")
        if not decision_node_id:
            raise ValidationError("decision_node_id is required")
        if not goal:
            raise ValidationError("goal is required")

        try:
            result = assemble_twin_for_decision(
                self.current_store.conn,
                decision_node_id=decision_node_id,
                goal=goal,
                horizon=body.get("horizon", 7),
                preferred_template_id=body.get("preferred_template_id"),
                preferred_model_id=body.get("preferred_model_id"),
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _route_twin_template_get(self, path: str, qs: dict) -> None:
        """Dispatch /twin-template/{id}/{action} to the right handler (OHM-hl61)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 2:
            raise ValidationError("GET /twin-template/{id} required")
        parts[1]
        if len(parts) >= 3 and parts[2]:
            raise ValidationError(f"unknown twin-template action: {parts[2]}")
        self._get_twin_template(path, qs)

    def _post_register_model_candidate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/register — register a model candidate for a twin (OHM-75tw).

        Body: {label, twin_id, model_parameters?, description?, connects_to?}
        """
        from ohm.queries import register_model_candidate
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        twin_id = body.get("twin_id")
        if not label:
            raise ValidationError("label is required")
        if not twin_id:
            raise ValidationError("twin_id is required")

        try:
            result = register_model_candidate(
                self.current_store.conn,
                label=label,
                twin_id=twin_id,
                created_by=agent,
                model_parameters=body.get("model_parameters"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_evaluate_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/{id}/evaluate — evaluate a model candidate (OHM-75tw).

        Body: {metrics, dataset?, description?}
        """
        from ohm.queries import evaluate_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_candidate_id = parts[1] if len(parts) >= 2 else None
        if not model_candidate_id:
            raise ValidationError("model_candidate_id is required in path")

        metrics = body.get("metrics")
        if not metrics:
            raise ValidationError("metrics is required")

        try:
            result = evaluate_model(
                self.current_store.conn,
                model_candidate_id=model_candidate_id,
                created_by=agent,
                metrics=metrics,
                dataset=body.get("dataset"),
                description=body.get("description"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_promote_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/{id}/promote — promote a model candidate to active (OHM-75tw)."""
        from ohm.queries import promote_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_candidate_id = parts[1] if len(parts) >= 2 else None
        if not model_candidate_id:
            raise ValidationError("model_candidate_id is required in path")

        try:
            result = promote_model(
                self.current_store.conn,
                model_candidate_id=model_candidate_id,
                created_by=agent,
                policy=body.get("policy", "accuracy"),
                decision_node_id=body.get("decision_node_id"),
                min_improvement=float(body.get("min_improvement", 0.0)),
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation_error", "message": str(e)})

    def _get_compare_models(self, path: str, qs: dict) -> None:
        """GET /model/compare — compare model candidates for a twin (OHM-75tw).

        Query params: ?twin_id=
        """
        from ohm.queries import compare_models
        from ohm.exceptions import ValidationError, NodeNotFoundError

        twin_id = qs.get("twin_id", [None])[0]
        if not twin_id:
            raise ValidationError("twin_id query parameter is required")

        try:
            result = compare_models(
                self.current_store.read_conn,
                twin_id=twin_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _route_model_post(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """Dispatch /model/{id}/{evaluate|promote|validate|retire|promotion-policy} to the right handler (OHM-75tw, OHM-bf45)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /model/{id}/{evaluate|promote|validate|retire|promotion-policy} required")
        action = parts[2]
        if action == "evaluate":
            self._post_evaluate_model(path, qs, body, agent)
        elif action == "promote":
            self._post_promote_model(path, qs, body, agent)
        elif action == "validate":
            self._post_validate_model(path, qs, body, agent)
        elif action == "retire":
            self._post_auto_retire_model(path, qs, body, agent)
        elif action == "promotion-policy":
            self._post_set_promotion_policy(path, qs, body, agent)
        else:
            raise ValidationError(f"unknown model action: {action}")

    def _post_register_shadow_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import register_shadow_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        twin_id = body.get("twin_id")
        label = body.get("label")
        source_model_id = body.get("source_model_id")
        if not twin_id:
            raise ValidationError("twin_id is required")
        if not label:
            raise ValidationError("label is required")
        if not source_model_id:
            raise ValidationError("source_model_id is required")

        try:
            result = register_shadow_model(
                self.current_store.conn,
                twin_id=twin_id,
                label=label,
                source_model_id=source_model_id,
                created_by=agent,
                model_parameters=body.get("model_parameters"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_detect_drift(self, path: str, qs: dict) -> None:
        from ohm.queries import detect_drift
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        window_size = int(qs.get("window_size", [100])[0])
        residual_threshold = float(qs.get("residual_threshold", [0.15])[0])

        try:
            result = detect_drift(
                self.current_store.read_conn,
                twin_id=twin_id,
                window_size=window_size,
                residual_threshold=residual_threshold,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_validate_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import run_walk_forward_validation
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_id = parts[1] if len(parts) >= 2 else None
        if not model_id:
            raise ValidationError("model_id is required in path")

        n_splits = int(body.get("n_splits", 5))
        min_train_size = int(body.get("min_train_size", 50))

        try:
            result = run_walk_forward_validation(
                self.current_store.conn,
                model_id=model_id,
                n_splits=n_splits,
                min_train_size=min_train_size,
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_ensemble_predict(self, path: str, qs: dict) -> None:
        from ohm.queries import ensemble_predict
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        observation_window = int(qs.get("observation_window", [50])[0])

        try:
            result = ensemble_predict(
                self.current_store.read_conn,
                twin_id=twin_id,
                observation_window=observation_window,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_decision_value(self, path: str, qs: dict) -> None:
        from ohm.queries import compute_decision_value
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_id = parts[1] if len(parts) >= 2 else None
        if not model_id:
            raise ValidationError("model_id is required in path")

        decision_node_id = qs.get("decision_node_id", [None])[0]
        utility_scale_str = qs.get("utility_scale", [None])[0]
        if not decision_node_id:
            raise ValidationError("decision_node_id query parameter is required")
        if not utility_scale_str:
            raise ValidationError("utility_scale query parameter is required")

        utility_scale = float(utility_scale_str)

        try:
            result = compute_decision_value(
                self.current_store.read_conn,
                model_id=model_id,
                decision_node_id=decision_node_id,
                utility_scale=utility_scale,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_auto_retire_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import auto_retire_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_id = parts[1] if len(parts) >= 2 else None
        if not model_id:
            raise ValidationError("model_id is required in path")

        reason = body.get("reason")
        if not reason:
            raise ValidationError("reason is required")

        try:
            result = auto_retire_model(
                self.current_store.conn,
                model_id=model_id,
                reason=reason,
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_set_promotion_policy(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/{id}/promotion-policy — set promotion policy on a model candidate (OHM-75tw)."""
        from ohm.queries import set_promotion_policy
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_candidate_id = parts[1] if len(parts) >= 2 else None
        if not model_candidate_id:
            raise ValidationError("model_candidate_id is required in path")

        policy = body.get("policy")
        if not policy:
            raise ValidationError("policy is required")

        try:
            result = set_promotion_policy(
                self.current_store.conn,
                model_candidate_id=model_candidate_id,
                policy=policy,
                decision_node_id=body.get("decision_node_id"),
                min_improvement=float(body.get("min_improvement", 0.0)),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation_error", "message": str(e)})

    def _post_auto_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/auto-promote — auto-promote the best model for a twin (OHM-75tw)."""
        from ohm.queries import auto_promote_best_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        try:
            result = auto_promote_best_model(
                self.current_store.conn,
                twin_id=twin_id,
                decision_node_id=body.get("decision_node_id"),
                policy=body.get("policy", "decision_value"),
                min_improvement=float(body.get("min_improvement", 0.0)),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation_error", "message": str(e)})

    def _route_model_get(self, path: str, qs: dict) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /model/{id}/{decision-value} required")
        action = parts[2]
        if action == "decision-value":
            self._get_decision_value(path, qs)
        else:
            raise ValidationError(f"unknown model GET action: {action}")

    def _post_set_freshness_threshold(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import set_freshness_threshold
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_id = body.get("decision_id")
        max_age_seconds = body.get("max_age_seconds")
        if not decision_id:
            raise ValidationError("decision_id is required")
        if max_age_seconds is None:
            raise ValidationError("max_age_seconds is required")

        try:
            max_age_seconds = int(max_age_seconds)
        except (ValueError, TypeError):
            raise ValidationError("max_age_seconds must be an integer")

        try:
            result = set_freshness_threshold(
                self.current_store.conn,
                decision_id=decision_id,
                max_age_seconds=max_age_seconds,
                created_by=agent,
                label=body.get("label"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_compute_feed_investment(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import compute_feed_investment
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_id = body.get("decision_id")
        if not decision_id:
            raise ValidationError("decision_id is required")

        try:
            observation_cost = float(body.get("observation_cost", 0.5))
        except (ValueError, TypeError):
            raise ValidationError("observation_cost must be a number")

        try:
            result = compute_feed_investment(
                self.current_store.conn,
                decision_id=decision_id,
                created_by=agent,
                observation_cost=observation_cost,
                label=body.get("label"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_record_mode_switch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import get_current_mode, record_mode_switch
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_id = body.get("decision_id")
        from_mode = body.get("from_mode")
        to_mode = body.get("to_mode")
        if not decision_id:
            raise ValidationError("decision_id is required")
        if not to_mode:
            raise ValidationError("to_mode is required")

        # from_mode is optional (OHM-kg16 item 5): if not provided,
        # derive it from the most recent mode_switch node for this
        # decision. The caller can always override by passing it
        # explicitly.
        from_mode_source = "explicit"
        if not from_mode:
            current = get_current_mode(self.current_store.conn, decision_id=decision_id)
            if current is None or not current.get("to_mode"):
                raise ValidationError("from_mode is required for the first mode switch on a decision — no prior mode_switch node exists. Pass from_mode explicitly or call GET /temporal/{decision_id}/mode first.")
            from_mode = current["to_mode"]
            from_mode_source = "derived"

        try:
            result = record_mode_switch(
                self.current_store.conn,
                decision_id=decision_id,
                from_mode=from_mode,
                to_mode=to_mode,
                created_by=agent,
                reason=body.get("reason"),
                label=body.get("label"),
            )
            self._json_response(
                201,
                {
                    "ok": True,
                    "data": result,
                    "from_mode_source": from_mode_source,
                },
            )
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation", "message": str(e)})

    def _route_temporal_get(self, path: str, qs: dict) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /temporal/{decision_id}/{action} required")
        decision_id = parts[1]
        action = parts[2]
        if action == "freshness":
            self._get_freshness_status(decision_id, qs)
        elif action == "mode":
            self._get_recommend_mode(decision_id, qs)
        elif action == "summary":
            self._get_temporal_summary(decision_id, qs)
        else:
            raise ValidationError(f"unknown temporal GET action: {action}")

    def _get_freshness_status(self, decision_id: str, qs: dict) -> None:
        from ohm.queries import get_freshness_status
        from ohm.exceptions import NodeNotFoundError

        try:
            result = get_freshness_status(
                self.current_store.conn,
                decision_id=decision_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_recommend_mode(self, decision_id: str, qs: dict) -> None:
        from ohm.queries import recommend_mode
        from ohm.exceptions import NodeNotFoundError

        try:
            result = recommend_mode(
                self.current_store.conn,
                decision_id=decision_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_temporal_summary(self, decision_id: str, qs: dict) -> None:
        from ohm.queries import temporal_decision_summary
        from ohm.exceptions import NodeNotFoundError

        try:
            result = temporal_decision_summary(
                self.current_store.conn,
                decision_id=decision_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_twin_design_start(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import start_twin_design_session
        from ohm.exceptions import ValidationError

        goal = body.get("goal")
        if not goal:
            raise ValidationError("goal is required")

        result = start_twin_design_session(
            self.current_store.conn,
            goal=goal,
            context=body.get("context"),
            created_by=agent,
            label=body.get("label"),
        )
        self._json_response(201, {"ok": True, "data": result})

    def _route_twin_design_post(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /twin/design/{session_id}/{action} required")

        session_id = parts[2]
        action = parts[3] if len(parts) >= 4 else ""

        if action == "transition":
            to_state = body.get("to_state")
            if not to_state:
                raise ValidationError("to_state is required")
            from ohm.queries import transition_session

            result = transition_session(
                self.current_store.conn,
                session_id=session_id,
                to_state=to_state,
                notes=body.get("notes"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "observe":
            observations = body.get("observations")
            if not observations:
                raise ValidationError("observations is required")
            from ohm.queries import add_session_observation

            result = add_session_observation(
                self.current_store.conn,
                session_id=session_id,
                observations=observations,
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "propose":
            from ohm.queries import propose_twin_config

            result = propose_twin_config(
                self.current_store.conn,
                session_id=session_id,
                decision_node_id=body.get("decision_node_id"),
                preferred_template_id=body.get("preferred_template_id"),
                preferred_model_id=body.get("preferred_model_id"),
                confidence_threshold=body.get("confidence_threshold", 0.6),
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        elif action == "review":
            proposal_id = body.get("proposal_id")
            decision = body.get("decision")
            if not proposal_id:
                raise ValidationError("proposal_id is required")
            if not decision:
                raise ValidationError("decision is required")
            from ohm.queries import review_proposal

            result = review_proposal(
                self.current_store.conn,
                session_id=session_id,
                proposal_id=proposal_id,
                decision=decision,
                approved_aspects=body.get("approved_aspects"),
                declined_aspects=body.get("declined_aspects"),
                modifications=body.get("modifications"),
                reason=body.get("reason"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "instantiate":
            from ohm.queries import instantiate_from_session

            result = instantiate_from_session(
                self.current_store.conn,
                session_id=session_id,
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        elif action == "calibrate":
            observations = body.get("observations")
            actuals = body.get("actuals")
            if not observations or not actuals:
                raise ValidationError("observations and actuals are required")
            from ohm.queries import record_calibration

            result = record_calibration(
                self.current_store.conn,
                session_id=session_id,
                observations=observations,
                actuals=actuals,
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "evolve":
            reason = body.get("reason")
            proposed_changes = body.get("proposed_changes")
            if not reason:
                raise ValidationError("reason is required")
            from ohm.queries import evolve_session

            result = evolve_session(
                self.current_store.conn,
                session_id=session_id,
                reason=reason,
                proposed_changes=proposed_changes or {},
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        else:
            raise ValidationError(f"unknown twin design action: {action}")

    def _route_twin_design_get(self, path: str, qs: dict) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /twin/design/{session_id}/{state|audit} required")

        session_id = parts[2]
        action = parts[3] if len(parts) >= 4 else "state"

        if action == "state":
            from ohm.queries import get_session_state

            result = get_session_state(
                self.current_store.read_conn,
                session_id=session_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "audit":
            from ohm.queries import get_session_audit

            result = get_session_audit(
                self.current_store.read_conn,
                session_id=session_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        else:
            raise ValidationError(f"unknown twin design GET action: {action}")

    def _get_detect_verifications(self, path: str, qs: dict) -> None:
        agent = qs.get("agent", [None])[0]
        days_threshold = int(qs.get("days_threshold", ["14"])[0])
        confidence_threshold = float(qs.get("confidence_threshold", ["0.85"])[0])
        limit = int(qs.get("limit", ["100"])[0])
        from ohm.queries import detect_verifiable_claims

        results = detect_verifiable_claims(
            self.current_store.read_conn,
            agent=agent,
            days_threshold=days_threshold,
            confidence_threshold=confidence_threshold,
            limit=limit,
        )
        self._json_response(200, {"ok": True, "data": results})

    def _post_create_nudge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        edge_id = body.get("edge_id")
        if not edge_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("edge_id is required")
        reason = body.get("reason")
        confidence = float(body.get("confidence", 0.5))
        from ohm.queries import create_verification_nudge

        result = create_verification_nudge(
            self.current_store.conn,
            edge_id=edge_id,
            created_by=agent,
            confidence=confidence,
            reason=reason,
        )
        self._json_response(201, {"ok": True, "data": result})

    def _post_record_verification_outcome(self, path: str, qs: dict, body: dict, agent: str) -> None:
        edge_id = body.get("edge_id")
        outcome = body.get("outcome")
        if not edge_id or not outcome:
            from ohm.exceptions import ValidationError

            raise ValidationError("edge_id and outcome are required")
        reason = body.get("reason")
        from ohm.queries import record_verification_outcome

        result = record_verification_outcome(
            self.current_store.conn,
            edge_id=edge_id,
            outcome=outcome,
            recorded_by=agent,
            reason=reason,
        )
        self._json_response(201, {"ok": True, "data": result})

    def _get_list_verifications(self, path: str, qs: dict) -> None:
        agent = qs.get("agent", [None])[0]
        limit = int(qs.get("limit", ["100"])[0])
        from ohm.queries import list_pending_verifications

        results = list_pending_verifications(
            self.current_store.read_conn,
            agent=agent,
            limit=limit,
        )
        self._json_response(200, {"ok": True, "data": results})
