"""Fragment handler mixin: connection, resolution, promotion."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import IngestHelperMixin


class FragmentHandlerMixin(IngestHelperMixin, OhmHandlerBase):
    """Handler mixin for fragment get/action/connect/resolve/promote endpoints."""

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
