"""Edge handler mixin."""

from __future__ import annotations

import logging
import time
from typing import Any

from ohm.server import server as _server_module
from ohm.server import suggestions as _suggestions_module
from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import IngestHelperMixin, _resolve_type_field
from ohm.server.nudges import generate_nudges, enrich_response

logger = logging.getLogger(__name__)


class EdgeHandlerMixin(IngestHelperMixin, OhmHandlerBase):
    """Handler mixin for edge get/suggest-type/create/sign/verify/delete endpoints."""

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
            agent=agent,
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
