"""Reports and misc handler mixin."""

from __future__ import annotations

from ohm.server import server as _server_module
from ohm.server.handlers._base import OhmHandlerBase


class ReportsHandlerMixin(OhmHandlerBase):
    """Handler mixin for reports and misc handler mixin."""

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

