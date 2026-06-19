"""Graph handler mixin — node/edge CRUD, search, observations, webhooks, and agent state."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from ohm.framework.exceptions import NodeNotFoundError, AuthenticationError
from ohm.server import server as _server_module
from ohm.server.nudges import generate_nudges, enrich_response


class GraphHandlerMixin:
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

    def _run_pre_ingest_hooks(self, agent: str, action: str, body: dict) -> dict | None:
        """Run pre_ingest hooks. Return error dict if any hook rejects, else None."""
        from ohm.hooks import HookRunner

        runner = HookRunner(self.current_store.conn)
        results = runner.run_hooks("pre_ingest", {"agent": agent, "action": action, "body": body})
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
                "observe": "POST /observe/{id} — Record a measurement or observation on a node. Required: node_id, obs_type, value. Optional: notes, source, source_url, sigma, compression_degree (0-1), compression_type (inversion|normative_inversion|retrojection|composite), beneficiary (list of agent IDs), revisability (0-1). Also: POST /observations for bulk upload, GET /observations for listing. ADR-026: Myth Compression Framework fields.",
                "challenge": "POST /challenge — Challenge an L3 interpretation. Required: edge_id. Optional: reason, confidence.",
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
                "listen": "GET /listen?since=ISO8601 — Change feed. See what agents have added recently.",
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
        }

        self._json_response(
            200,
            {
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
            },
        )

    def _get_layers(self, path: str, qs: dict) -> None:
        """GET /layers — layer descriptions."""
        self._json_response(200, self.schema_config.layer_descriptions)

    def _get_node(self, path: str, qs: dict) -> None:
        """GET /node/<id> — fetch a node with effective_layer and constraint_status."""
        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        node = self.current_store.get_node(node_id)
        if node:
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
            edges = self.current_store.execute(
                "SELECT * FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL ORDER BY created_at DESC",
                [node_id, node_id],
            )
            result["edges"] = edges
            result["edge_count"] = len(edges)
            self._json_response(200, result)
        except NodeNotFoundError:  # noqa: F821
            raise
        except Exception as e:
            self._json_response(500, {"error": "deep_content_failed", "message": str(e)})

    def _get_edge(self, path: str, qs: dict) -> None:
        """GET /edge/<id> — fetch an edge."""
        edge_id = path[6:]
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")
        edge = self.current_store.get_edge(edge_id)
        if edge:
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
        from ohm.graph.constraints import effective_layer

        LARGE_NEIGHBORHOOD_THRESHOLD = 500
        response = {"nodes": node_rows, "edges": edges}

        if len(node_rows) <= LARGE_NEIGHBORHOOD_THRESHOLD:
            for n in node_rows:
                eff_layer, _cs = effective_layer(self.current_store.conn, n["id"])
                n["effective_layer"] = eff_layer
        else:
            response["warning"] = f"Neighborhood has {len(node_rows)} nodes; effective_layer computation skipped for performance. Use /constraint-report?batch=true for bulk analysis."
            response["truncated"] = True

        self._json_response(200, response)

    def _get_path(self, path: str, qs: dict) -> None:
        """GET /path/<from>/<to> — shortest path."""
        parts = path[6:].split("/")
        if len(parts) >= 2:
            from ohm.validation import validate_identifier

            from_node = validate_identifier(parts[0], name="from_node")
            to_node = validate_identifier(parts[1], name="to_node")
            from ohm.queries import query_path

            results = query_path(self.current_store.conn, from_node, to_node)
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
        from ohm.queries import query_impact

        results = query_impact(self.current_store.conn, node_id, depth=depth)
        self._json_response(200, results)

    def _get_confidence(self, path: str, qs: dict) -> None:
        """GET /confidence/<id> — confidence breakdown."""
        target_id = path[12:]
        from ohm.validation import validate_identifier

        target_id = validate_identifier(target_id, name="target_id")
        from ohm.queries import query_confidence

        is_node = self.current_store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE id = ?",
            [target_id],
        ).fetchone()
        is_edge = self.current_store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE id = ?",
            [target_id],
        ).fetchone()

        if is_node and is_node[0] > 0:
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
            results = query_confidence(self.current_store.conn, target_id)
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
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0]["cnt"] if total_result else len(results)
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
        total = total_result[0]["cnt"] if total_result else len(results)
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
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        node_type = qs.get("type", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        since = qs.get("since", [None])[0]
        until = qs.get("until", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
        limit = int(qs.get("limit", [20])[0])
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
        params.append(limit)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
        results = self.current_store.execute(sql, params)

        # OHM-tr71.8: Automatic semantic fallback on empty text search
        # When text search returns 0 results, try semantic search automatically
        if not results and not node_type:
            try:
                from ohm.graph.queries import semantic_search

                semantic_results = semantic_search(
                    self.current_store.conn,
                    query=query_text,
                    limit=limit,
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
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        if not query_text:
            raise ValidationError("Semantic search requires ?q=QUERY")
        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [10])[0])
        min_confidence = qs.get("min_confidence", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
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
        total = total_result[0]["cnt"] if total_result else len(results)
        self._json_response(200, {"observations": results, "total": total, "limit": limit, "offset": offset})

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

        node_type = body.get("type", "concept")
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
        if body.get("layer") and body.get("type") == "fragment":
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

        result = self.current_store.write_node(
            id=body["id"],
            label=body["label"],
            type=body.get("type", "concept"),
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
        )
        result = enrich_response(result, nudges)

        # ADR-021: Proactive discoverability — post-write suggestions + connectivity nudge.
        # Both run under a 300ms hard cap so they never delay the write response on slow
        # graphs. If the budget is exceeded, the response omits suggestions (not an error).
        if result.get("created", True):
            import concurrent.futures
            from ohm.server.suggestions import generate_suggestions, generate_connectivity_nudge, generate_island_nudge

            def _suggestions_and_nudge():
                sugg = generate_suggestions(
                    store=self.current_store,
                    node_id=result.get("id", ""),
                    content=body.get("content"),
                    label=body.get("label"),
                    tags=body.get("tags"),
                    node_type=body.get("type"),
                    has_edges=bool(body.get("connects_to")),
                )
                nudge = generate_connectivity_nudge(self.current_store, agent)
                island = generate_island_nudge(self.current_store, agent)
                return sugg, nudge, island

            try:
                _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                future = _pool.submit(_suggestions_and_nudge)
                try:
                    suggestions, nudge, island = future.result(timeout=0.3)
                    result["suggestions"] = suggestions
                    if nudge:
                        result["connectivity_warning"] = nudge["connectivity_warning"]
                    if island:
                        result["island_warning"] = island["island_warning"]
                except concurrent.futures.TimeoutError:
                    logger.debug("Suggestion budget exceeded (>300ms), skipping for this write")
                except Exception as e:
                    logger.debug(f"Suggestions failed: {e}")
                finally:
                    _pool.shutdown(wait=False)
            except Exception:
                pass

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
            node_type=body.get("type", "concept"),
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
        from ohm.server.suggestions import generate_suggestions

        suggestions = generate_suggestions(
            store=self.current_store,
            node_id=node.get("id", ""),
            content=content,
            label=node.get("label"),
            tags=body.get("tags"),
            node_type="fragment",
            has_edges=bool(body.get("connects_to")),
        )
        node["suggestions"] = suggestions

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
            edge_type=body.get("type", ""),
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
        nudges = generate_nudges(
            action="edge",
            node_id=body.get("to") or body.get("from"),
            edge_type=body.get("type"),
            confidence=body.get("confidence"),
            provenance=body.get("provenance"),
            tags=None,
            store=self.current_store,
            from_node_id=body.get("from"),
            to_node_id=body.get("to"),
            challenge_ratio=_challenge_ratio,
        )
        result = enrich_response(result, nudges)

        # ADR-021: Relational tags — add edge type as tag on both endpoints
        try:
            from ohm.server.relational_tags import add_relational_tags

            tag_result = add_relational_tags(
                conn=self.current_store.conn,
                from_node=body["from"],
                to_node=body["to"],
                edge_type=body.get("type", ""),
            )
            if tag_result["tags_added"]:
                result["relational_tags"] = tag_result
        except Exception as e:
            # Relational tags never fail the write
            logger.debug(f"Relational tag enrichment failed: {e}")

        # ADR-021: Proactive discoverability — post-write edge suggestions
        try:
            from ohm.server.suggestions import generate_edge_suggestions

            edge_suggestions = generate_edge_suggestions(
                store=self.current_store,
                from_node=body["from"],
                to_node=body["to"],
                edge_type=body.get("type", ""),
                layer=body.get("layer", "L3"),
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
        """POST /challenge/{id} — challenge an existing edge."""
        edge_id = path[11:]
        from ohm.validation import validate_identifier
        from ohm.exceptions import EdgeNotFoundError

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.5)
        challenge_type = body.get("challenge_type", "CHALLENGED_BY")
        result = self.current_store.challenge_edge(edge_id, reason, confidence, challenge_type, agent_name=agent)
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
                    "challenge_type": challenge_type,
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
        result = self.current_store.challenge_edge(edge_id, reason, confidence, "SUPPORTS", agent_name=agent)
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
        obs_type = body.get("type", "measurement")
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
        )
        result = enrich_response(result, nudges)
        self._json_response(201, result)

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

        try:
            self.current_store.conn.execute("BEGIN TRANSACTION")
            for node in nodes:
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

    def _patch_node(self, path: str, body: dict, agent: str) -> None:
        """PATCH /node/<id> — partial node update."""
        from datetime import datetime, timezone
        from ohm.exceptions import NodeNotFoundError, ValidationError
        from ohm.validation import validate_identifier

        node_id = path[6:]
        node_id = validate_identifier(node_id, name="node_id")
        node = self.current_store.get_node(node_id)
        if not node:
            raise NodeNotFoundError(f"Node not found: {node_id}")

        from ohm.server.boundary import enforce_l2_immutability

        enforce_l2_immutability(self.current_store.conn, agent, node_id)

        # Validate type change against schema
        if "type" in body and body["type"] not in self.schema_config.node_types:
            raise ValidationError(f"Invalid node type: '{body['type']}' — must be one of: {', '.join(sorted(self.schema_config.node_types))}")

        now = datetime.now(timezone.utc).isoformat()
        patchable = [
            "label",
            "type",
            "content",
            "confidence",
            "visibility",
            "provenance",
            "tags",
            "metadata",
            "priority",
            "url",
            "task_status",
            "assigned_to",
            "due_date",
            "utility_scale",
            "current_best_action",
            "action_alternatives",
            "utility_usd_per_day",
            "utility_currency",
        ]
        update_fields = []
        update_params = []
        for field in patchable:
            if field in body:
                update_fields.append(f"{field} = ?")
                update_params.append(body[field])

        if not update_fields:
            raise ValidationError("No updatable fields provided")

        update_fields.append("updated_at = ?")
        update_params.append(now)
        update_fields.append("updated_by = ?")
        update_params.append(agent)
        update_params.append(node_id)

        self.current_store.conn.execute(
            f"UPDATE ohm_nodes SET {', '.join(update_fields)} WHERE id = ?",
            update_params,
        )
        self.current_store._log_change("ohm_nodes", node_id, "UPDATE", "L3", agent_name=agent)
        self.current_store._increment_graph_generation()
        updated = self.current_store.get_node(node_id)
        _server_module._trigger_webhooks(
            {"type": "node.updated", "agent": agent, "node": updated},
            customer_id=self._customer_id,
        )
        self._json_response(200, updated)

    def _patch_edge(self, path: str, body: dict, agent: str) -> None:
        """PATCH /edge/<id> — partial edge update."""
        from datetime import datetime, timezone
        from ohm.exceptions import NodeNotFoundError, ValidationError
        from ohm.validation import validate_identifier

        edge_id = path[6:]
        edge_id = validate_identifier(edge_id, name="edge_id")
        edge = self.current_store.get_edge(edge_id)
        if not edge:
            raise NodeNotFoundError(f"Edge not found: {edge_id}")

        from ohm.server.boundary import enforce_write_boundary

        enforce_write_boundary(self.current_store.conn, agent, edge_id)

        now = datetime.now(timezone.utc).isoformat()
        pert_fields = [
            "probability",
            "probability_p05",
            "probability_p50",
            "probability_p95",
            "confidence",
            "confidence_p05",
            "confidence_p50",
            "confidence_p95",
            "condition",
            "provenance",
            "urgency",
        ]
        update_fields = []
        update_params = []
        for field in pert_fields:
            if field in body:
                update_fields.append(f"{field} = ?")
                update_params.append(body[field])

        # Allow edge_type updates for causal restructuring (ADR-023)
        if "edge_type" in body:
            from ohm.validation import validate_identifier

            new_type = validate_identifier(body["edge_type"], name="edge_type")
            update_fields.append("edge_type = ?")
            update_params.append(new_type)

        if "probability_p50" in body and "probability" not in body:
            from ohm.pert import compute_pert_mean

            p05 = body.get("probability_p05", edge.get("probability_p05") or body["probability_p50"])
            p95 = body.get("probability_p95", edge.get("probability_p95") or body["probability_p50"])
            pert_mean = compute_pert_mean(p05, body["probability_p50"], p95)
            update_fields.append("probability = ?")
            update_params.append(pert_mean)

        if not update_fields:
            raise ValidationError("No updatable fields provided")

        update_fields.append("updated_at = ?")
        update_params.append(now)
        update_fields.append("updated_by = ?")
        update_params.append(agent)
        update_params.append(edge_id)

        self.current_store.conn.execute(
            f"UPDATE ohm_edges SET {', '.join(update_fields)} WHERE id = ?",
            update_params,
        )
        self.current_store._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"], agent_name=agent)
        self.current_store._increment_graph_generation()
        updated = self.current_store.get_edge(edge_id)
        _server_module._trigger_webhooks(
            {"type": "edge.updated", "agent": agent, "edge": updated},
            customer_id=self._customer_id,
        )
        self._json_response(200, updated)

    def _patch_edges(self, path: str, body: dict, agent: str) -> None:
        """PATCH /edges — bulk edge update with PERT fields."""
        from datetime import datetime, timezone
        from ohm.exceptions import ValidationError
        from ohm.validation import validate_identifier

        edges = body.get("edges", [])
        if not edges:
            raise ValidationError("No edges provided in 'edges' array")
        if not isinstance(edges, list):
            raise ValidationError("'edges' must be an array of {id, ...} objects")

        now = datetime.now(timezone.utc).isoformat()
        pert_fields = [
            "probability",
            "probability_p05",
            "probability_p50",
            "probability_p95",
            "confidence",
            "confidence_p05",
            "confidence_p50",
            "confidence_p95",
            "condition",
            "provenance",
            "urgency",
        ]

        results = []
        errors = []
        for item in edges:
            edge_id = item.get("id")
            if not edge_id:
                errors.append({"error": "missing id field", "item": item})
                continue
            edge_id = validate_identifier(edge_id, name="edge_id")
            edge = self.current_store.get_edge(edge_id)
            if not edge:
                errors.append({"error": f"Edge not found: {edge_id}"})
                continue

            from ohm.server.boundary import enforce_write_boundary

            enforce_write_boundary(self.current_store.conn, agent, edge_id)

            update_fields = []
            update_params = []
            for field in pert_fields:
                if field in item:
                    update_fields.append(f"{field} = ?")
                    update_params.append(item[field])

            # Allow edge_type updates for causal restructuring (ADR-023)
            if "edge_type" in item:
                new_type = validate_identifier(item["edge_type"], name="edge_type")
                update_fields.append("edge_type = ?")
                update_params.append(new_type)

            if "probability_p50" in item and "probability" not in item:
                from ohm.pert import compute_pert_mean

                p05 = item.get("probability_p05", edge.get("probability_p05") or item["probability_p50"])
                p95 = item.get("probability_p95", edge.get("probability_p95") or item["probability_p50"])
                pert_mean = compute_pert_mean(p05, item["probability_p50"], p95)
                update_fields.append("probability = ?")
                update_params.append(pert_mean)

            if not update_fields:
                errors.append({"error": "No updatable fields provided", "edge_id": edge_id})
                continue

            update_fields.append("updated_at = ?")
            update_params.append(now)
            update_fields.append("updated_by = ?")
            update_params.append(agent)
            update_params.append(edge_id)

            try:
                self.current_store.conn.execute(
                    f"UPDATE ohm_edges SET {', '.join(update_fields)} WHERE id = ?",
                    update_params,
                )
                self.current_store._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"], agent_name=agent)
                self.current_store._increment_graph_generation()
                updated = self.current_store.get_edge(edge_id)
                results.append(updated)
            except Exception as e:
                errors.append({"error": str(e), "edge_id": edge_id})

        response = {"updated": results, "count": len(results)}
        if errors:
            response["errors"] = errors
        self._json_response(200 if not errors else 207, response)

    def _post_ask(self, path: str, qs: dict, body: dict, agent: str) -> None:
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
                    evidence = {}
                    for nid in target_ids:
                        obs_rows = self.current_store.execute(
                            "SELECT value FROM ohm_observations WHERE node_id = ? AND type = 'probability' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
                            [nid],
                        )
                        if obs_rows:
                            try:
                                val = float(obs_rows[0]["value"])
                                if 0.0 <= val <= 1.0:
                                    evidence[nid] = 1 if val >= 0.5 else 0
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

    def _patch_task(self, path: str, body: dict, agent: str) -> None:
        """PATCH /tasks/<id> — task update via node patch."""
        node_id = path[8:]
        self._patch_node(f"/node/{node_id}", body, agent)
