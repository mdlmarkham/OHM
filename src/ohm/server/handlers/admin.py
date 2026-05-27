"""Admin handler mixin — checkpoint, embeddings, and snapshot endpoints."""

import time


class AdminHandlerMixin:
    """Handler mixin for administrative operations (OHM-brry)."""

    def _get_admin_checkpoint(self, path: str, qs: dict) -> None:
        """GET /admin/checkpoint — force DuckDB CHECKPOINT."""
        self._require_write_auth()
        try:
            self.current_store.conn.execute("CHECKPOINT")
            self._json_response(200, {"status": "ok", "message": "WAL flushed to main database"})
        except Exception as e:
            self._json_response(500, {"error": "checkpoint_failed", "message": str(e)})

    def _post_admin_checkpoint(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/checkpoint — force DuckDB CHECKPOINT to flush WAL to main DB file."""
        try:
            self.current_store.conn.execute("CHECKPOINT")
            self._json_response(200, {"status": "ok", "message": "WAL flushed to main database"})
        except Exception as e:
            self._json_response(500, {"error": "checkpoint_failed", "message": str(e)})

    def _get_admin_embeddings(self, path: str, qs: dict) -> None:
        """GET /admin/embeddings — batch generate embeddings."""
        try:
            from ohm.queries import update_node_embedding

            batch_size = 5
            delay_ms = 200
            if qs.get("batch_size"):
                try:
                    batch_size = int(qs["batch_size"][0])
                    if batch_size < 1:
                        batch_size = 1
                    elif batch_size > 50:
                        batch_size = 50
                except ValueError:
                    pass
            if qs.get("delay_ms"):
                try:
                    delay_ms = int(qs["delay_ms"][0])
                    if delay_ms < 0:
                        delay_ms = 0
                    elif delay_ms > 5000:
                        delay_ms = 5000
                except ValueError:
                    pass

            rows = self.current_store.execute("SELECT id, label FROM ohm_nodes WHERE embedding IS NULL AND deleted_at IS NULL")
            if not rows:
                self._json_response(
                    200,
                    {
                        "status": "ok",
                        "updated": 0,
                        "failed": 0,
                        "total": 0,
                        "remaining": 0,
                        "message": "All nodes already have embeddings",
                    },
                )
                return

            updated = 0
            failed = 0
            processed = 0
            for row in rows:
                if processed >= batch_size:
                    break
                try:
                    if update_node_embedding(self.current_store.conn, row["id"]):
                        updated += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                processed += 1
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)

            total_missing = len(rows)
            remaining = total_missing - processed
            self._json_response(
                200,
                {
                    "status": "ok" if remaining == 0 else "partial",
                    "updated": updated,
                    "failed": failed,
                    "processed": processed,
                    "total": total_missing,
                    "remaining": remaining,
                    "message": f"Generated {updated} embeddings ({failed} failed). {remaining} remaining — re-call to continue.",
                },
            )
        except Exception as e:
            self._json_response(500, {"error": "embedding_backfill_failed", "message": str(e)})

    def _post_admin_edge_layer_fix(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/edge-layer-fix — bulk move edges to correct layer based on schema.

        Body: {"edge_type": "REFERENCES", "from_layer": "L3", "to_layer": "L2"}
        Only moves edges that match the schema's layer assignment for the given type.
        """
        edge_type = body.get("edge_type")
        from_layer = body.get("from_layer")
        to_layer = body.get("to_layer")

        if not edge_type or not from_layer or not to_layer:
            self._json_response(400, {"error": "edge_type, from_layer, and to_layer are required"})
            return

        # Validate against schema
        schema_layers = self.current_store.schema.layer_edge_types
        expected_layer = None
        for layer, types in schema_layers.items():
            if edge_type in types:
                expected_layer = layer
                break

        if expected_layer and expected_layer != to_layer:
            self._json_response(400, {
                "error": f"Schema assigns {edge_type} to {expected_layer}, not {to_layer}",
                "expected": expected_layer,
            })
            return

        try:
            result = self.current_store.conn.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE edge_type = ? AND layer = ? AND deleted_at IS NULL",
                [edge_type, from_layer],
            ).fetchone()
            count = result[0]

            self.current_store.conn.execute(
                "UPDATE ohm_edges SET layer = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? "
                "WHERE edge_type = ? AND layer = ? AND deleted_at IS NULL",
                [to_layer, agent, edge_type, from_layer],
            )

            self.current_store._log_change("ohm_edges", "bulk", "UPDATE", to_layer, agent_name=agent)
            self.current_store._increment_graph_generation()

            self._json_response(200, {
                "status": "ok",
                "edge_type": edge_type,
                "from_layer": from_layer,
                "to_layer": to_layer,
                "moved": count,
            })
        except Exception as e:
            self._json_response(500, {"error": "edge_layer_fix_failed", "message": str(e)})

    def _post_admin_observation_source_urls(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/observation-source-urls — bulk update source_url on observations (ADR-013 backfill).

        Body: {"updates": [{"observation_id": "<uuid>", "source_url": "https://..."}, ...]}
        Max 200 updates per call.
        """
        updates = body.get("updates", [])
        if not isinstance(updates, list):
            self._json_response(400, {"error": "updates must be an array"})
            return
        if len(updates) > 200:
            self._json_response(400, {"error": f"Too many updates: {len(updates)} (max 200)"})
            return

        updated = 0
        not_found = 0
        errors = []
        for item in updates:
            obs_id = item.get("observation_id")
            source_url = item.get("source_url")
            if not obs_id or not source_url:
                errors.append({"observation_id": obs_id, "error": "missing observation_id or source_url"})
                continue
            try:
                self.current_store.conn.execute(
                    "UPDATE ohm_observations SET source_url = ? WHERE id = ? AND deleted_at IS NULL",
                    [source_url, obs_id],
                )
                # Check if row was updated
                result = self.current_store.conn.execute(
                    "SELECT id FROM ohm_observations WHERE id = ? AND source_url = ? AND deleted_at IS NULL",
                    [obs_id, source_url],
                ).fetchone()
                if result:
                    updated += 1
                    self.current_store._log_change("ohm_observations", obs_id, "UPDATE", "L2", agent_name=agent)
                else:
                    not_found += 1
            except Exception as e:
                errors.append({"observation_id": obs_id, "error": str(e)})

        self._json_response(200, {
            "updated": updated,
            "not_found": not_found,
            "errors": errors[:10],
            "total_requested": len(updates),
        })

    def _post_admin_source_node_urls(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/source-node-urls — bulk update url on source nodes (ADR-013 backfill).

        Source nodes are L2-immutable via regular PATCH, so this admin endpoint
        is needed to backfill URLs on source nodes created before ADR-013 enforcement.

        Body: {"updates": [{"node_id": "source-reuters", "url": "https://..."}, ...]}
        Max 200 updates per call.
        """
        updates = body.get("updates", [])
        if not isinstance(updates, list):
            self._json_response(400, {"error": "updates must be an array"})
            return
        if len(updates) > 200:
            self._json_response(400, {"error": f"Too many updates: {len(updates)} (max 200)"})
            return

        updated = 0
        not_found = 0
        not_source = 0
        errors = []
        for item in updates:
            node_id = item.get("node_id")
            url = item.get("url")
            if not node_id or not url:
                errors.append({"node_id": node_id, "error": "missing node_id or url"})
                continue
            try:
                # Verify node is a source type
                result = self.current_store.conn.execute(
                    "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [node_id],
                ).fetchone()
                if not result:
                    not_found += 1
                    continue
                if result[0] != "source":
                    not_source += 1
                    errors.append({"node_id": node_id, "error": f"node type is '{result[0]}', not 'source'"})
                    continue
                self.current_store.conn.execute(
                    "UPDATE ohm_nodes SET url = ? WHERE id = ? AND deleted_at IS NULL",
                    [url, node_id],
                )
                # Verify update
                check = self.current_store.conn.execute(
                    "SELECT id FROM ohm_nodes WHERE id = ? AND url = ? AND deleted_at IS NULL",
                    [node_id, url],
                ).fetchone()
                if check:
                    updated += 1
                    self.current_store._log_change("ohm_nodes", node_id, "UPDATE", "L2", agent_name=agent)
                else:
                    not_found += 1
            except Exception as e:
                errors.append({"node_id": node_id, "error": str(e)})

        self._json_response(200, {
            "updated": updated,
            "not_found": not_found,
            "not_source": not_source,
            "errors": errors[:10],
            "total_requested": len(updates),
        })

    def _get_admin_snapshots(self, path: str, qs: dict) -> None:
        """GET /admin/snapshots — list DuckLake snapshots."""
        snapshots = self.current_store.list_snapshots()
        self._json_response(200, {"snapshots": snapshots, "count": len(snapshots)})