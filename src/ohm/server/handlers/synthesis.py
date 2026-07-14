"""Synthesis handler mixin."""

from __future__ import annotations

from ohm.server import server as _server_module
from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import IngestHelperMixin


class SynthesisHandlerMixin(IngestHelperMixin, OhmHandlerBase):
    """Handler mixin for synthesis handler mixin."""

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

