"""Graph handler mixin — node/edge CRUD, search, observations, webhooks, and agent state."""

from __future__ import annotations

from ohm.server import server as _server_module
from ohm.server.nudges import generate_nudges, enrich_response


class GraphHandlerMixin:
    """Handler mixin for graph CRUD endpoints (OHM-hpxa).

    Methods migrated from server.py: 38 handler methods covering node/edge
    read/write/delete, search, observations, agent registration, webhooks,
    and batch operations.
    """

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
                    r.hook_id, r.exit_code, r.stderr,
                )
        return decorations

    def _get_stats(self, path: str, qs: dict) -> None:
        """GET /stats — graph statistics."""
        from ohm.queries import query_stats

        stats = query_stats(self.current_store.conn)
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
        """GET /schema — schema description."""
        schema = self.schema_config
        all_edge_types: set[str] = set()
        for types in schema.layer_edge_types.values():
            all_edge_types.update(types)
        self._json_response(
            200,
            {
                "schema": schema.name,
                "node_types": sorted(schema.node_types),
                "edge_types": sorted(all_edge_types),
                "edge_types_by_layer": {k: sorted(v) for k, v in schema.layer_edge_types.items()},
                "layers": schema.layer_descriptions,
            },
        )

    def _get_layers(self, path: str, qs: dict) -> None:
        """GET /layers — layer descriptions."""
        self._json_response(200, self.schema_config.layer_descriptions)

    def _get_node(self, path: str, qs: dict) -> None:
        """GET /node/<id> — fetch a node."""
        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        node = self.current_store.get_node(node_id)
        if node:
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
        except NodeNotFoundError:
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
        """GET /neighborhood/<id> — node neighborhood."""
        node_id = path[14:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        depth = int(qs.get("depth", [3])[0])
        layer = qs.get("layer", [None])[0]
        from ohm.queries import query_neighborhood

        edges = query_neighborhood(self.current_store.conn, node_id, depth=depth, layer=layer)
        node_ids = {node_id}
        for e in edges:
            node_ids.add(e["from_node"])
            node_ids.add(e["to_node"])

        # ADR-013: Add citation_status to L3 edges
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
        self._json_response(200, {"nodes": node_rows, "edges": edges})

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
                raise AuthenticationError("Authentication required — provide Bearer token")
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
        """GET /search — text search over nodes."""
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        node_type = qs.get("type", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [20])[0])
        if not query_text:
            raise ValidationError("Search requires ?q=QUERY")
        conditions = ["deleted_at IS NULL", "(label ILIKE ? OR content ILIKE ?)"]
        params = [f"%{query_text}%", f"%{query_text}%"]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
        results = self.current_store.execute(sql, params)
        self._json_response(200, results)

    def _get_semantic_search(self, path: str, qs: dict) -> None:
        """GET /semantic_search — vector similarity search."""
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        if not query_text:
            raise ValidationError("Semantic search requires ?q=QUERY")
        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [10])[0])
        min_confidence = qs.get("min_confidence", [None])[0]
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
                "hint": "Add a 'connects_to' field with one or more existing node ids, "
                "or use POST /batch to atomically create the node and at least one edge.",
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
                "message": (
                    f"connects_to references unknown node id(s): {missing}. "
                    f"Cross-link targets must already exist in the graph."
                ),
                "missing": missing,
            }

        return None

    def _post_node(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node — create or upsert a node."""
        # ADR-013 source_url enforcement migrated to built-in pre_ingest hook
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
            url=body.get("url"),
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
        # ADR-017: Cognitive nudge enrichment
        nudges = generate_nudges(
            action="node",
            node_id=result.get("id"),
            tags=body.get("tags"),
            provenance=body.get("provenance"),
        )
        result = enrich_response(result, nudges)
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

    def _post_edge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /edge — create an edge."""
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
        # ADR-017: Cognitive nudge enrichment
        nudges = generate_nudges(
            action="edge",
            edge_type=body.get("type"),
            confidence=body.get("confidence"),
            provenance=body.get("provenance"),
            tags=None,
        )
        result = enrich_response(result, nudges)
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
            if scale == "probability":
                value = body.get("value")
                if value is not None and (value < 0.0 or value > 1.0):
                    raise ValidationError(f"Observation value {value} is outside [0, 1] for scale='probability'")
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
        )
        _server_module._trigger_webhooks(
            {
                "type": "observation.created",
                "agent": agent,
                "observation": result,
            },
            customer_id=self._customer_id,
        )
        # ADR-017: Cognitive nudge enrichment
        nudges = generate_nudges(
            action="observation",
            node_id=node_id,
            confidence=body.get("value"),
            provenance=body.get("source"),
            source_url=body.get("source_url"),
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
        import json as _json

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
        for cid in cluster_ids:
            try:
                safe_cid = validate_identifier(cid, name="cluster_id")
            except ValueError:
                continue
            try:
                self.current_store.write_edge(
                    from_node=node_id,
                    to_node=safe_cid,
                    edge_type=edge_type,
                    layer="L3",
                    confidence=confidence,
                    agent_name=agent,
                )
                edges_created += 1
            except Exception:
                continue

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

        self._json_response(
            201,
            {
                "node": node_result if isinstance(node_result, dict) else {"id": node_id, "label": label},
                "edges_created": edges_created,
                "observation": obs_result,
            },
        )

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

    def _post_heartbeat(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /heartbeat — agent heartbeat with sync."""
        from ohm.methods import agent_heartbeat

        result = agent_heartbeat(
            self.current_store.conn,
            agent,
            focus=body.get("focus"),
        )
        sync_result = self.current_store.sync_heartbeat()
        result["ducklake_sync"] = sync_result
        self._json_response(200, result)

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

        now = datetime.now(timezone.utc).isoformat()
        patchable = [
            "label",
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

    def _patch_task(self, path: str, body: dict, agent: str) -> None:
        """PATCH /tasks/<id> — task update via node patch."""
        node_id = path[8:]
        self._patch_node(f"/node/{node_id}", body, agent)
