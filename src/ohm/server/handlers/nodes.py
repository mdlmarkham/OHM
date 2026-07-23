"""Node handler mixin."""

from __future__ import annotations

import logging
import time

from ohm.framework.exceptions import NodeNotFoundError
from ohm.server import server as _server_module
from ohm.server import suggestions as _suggestions_module
from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import IngestHelperMixin, _resolve_type_field
from ohm.server.nudges import generate_nudges, enrich_response

logger = logging.getLogger(__name__)


class NodeHandlerMixin(IngestHelperMixin, OhmHandlerBase):
    """Handler mixin for node get/deep/neighborhood/path/impact/confidence/agent/create/sign/verify/delete endpoints."""

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

    def _post_node(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node — create or upsert a node."""

        # ADR-015 source_url enforcement migrated to built-in pre_ingest hook
        # (python:ohm.hooks_builtin.source_url_required). See OHM-aznh.11.

        create_only = qs.get("create_only", ["true"])[0].lower() in ("true", "1", "yes")
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
            previous_node = self.current_store.get_node(body["id"]) or {}
            previous_updated_by = (
                previous_node.get("updated_by")
                or previous_node.get("created_by")
            )
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
            result["overwrote"] = True
            if previous_updated_by:
                result["previous_updated_by"] = previous_updated_by
                if previous_updated_by != agent:
                    result["overwrite_warning"] = (
                        f"Overwrote node previously authored by '{previous_updated_by}'. "
                        "Review for unintended data loss."
                    )
            else:
                result["previous_updated_by"] = None
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
