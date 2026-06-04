"""Admin handler mixin — checkpoint, embeddings, snapshot, and hook endpoints."""

import time


class AdminHandlerMixin:
    """Handler mixin for administrative operations (OHM-brry)."""

    def _post_hooks(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /hooks — register a new hook.

        Body: {event, command, timeout_ms?, enabled?}
        """
        from ohm.queries import create_hook
        from ohm.exceptions import ValidationError

        event = body.get("event")
        command = body.get("command")
        timeout_ms = body.get("timeout_ms", 5000)
        enabled = body.get("enabled", True)

        if not event:
            raise ValidationError("event is required")
        if not command:
            raise ValidationError("command is required")

        try:
            hook = create_hook(
                self.current_store.conn,
                event=event,
                command=command,
                created_by=agent,
                timeout_ms=int(timeout_ms),
                enabled=bool(enabled),
            )
            self._json_response(201, hook)
        except ValueError as e:
            raise ValidationError(str(e))

    def _get_hooks(self, path: str, qs: dict) -> None:
        """GET /hooks — list registered hooks. Optional ?event= filter."""
        from ohm.queries import query_hooks
        from ohm.exceptions import ValidationError

        event = qs.get("event", [None])[0]
        try:
            hooks = query_hooks(self.current_store.conn, event=event)
            self._json_response(200, {"hooks": hooks, "count": len(hooks)})
        except ValueError as e:
            raise ValidationError(str(e))

    def _delete_hook(self, path: str, agent: str) -> None:
        """DELETE /hooks/{id} — remove a hook."""
        from ohm.queries import delete_hook
        from ohm.exceptions import ValidationError

        hook_id = path[len("/hooks/"):]
        if not hook_id:
            raise ValidationError("Hook ID is required")

        try:
            result = delete_hook(
                self.current_store.conn,
                hook_id=hook_id,
                deleted_by=agent,
            )
            self._json_response(200, result)
        except ValueError as e:
            raise ValidationError(str(e))

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
                        "processed": 0,
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

        # OHM-od01.16: single SQL pass per row was 2n round-trips (verify +
        # UPDATE + verify). Use UPDATE … RETURNING to combine into one
        # statement; check the type in Python from a pre-fetched set of
        # source-node ids. For typical 200-item batches this turns
        # 400 round-trips into ~3.
        node_ids = [
            item.get("node_id")
            for item in updates
            if item.get("node_id") and item.get("url")
        ]
        bad_items = [
            item for item in updates
            if not (item.get("node_id") and item.get("url"))
        ]
        for item in bad_items:
            errors.append({"node_id": item.get("node_id"), "error": "missing node_id or url"})

        source_ids: set[str] = set()
        if node_ids:
            placeholders = ",".join(["?"] * len(node_ids))
            type_rows = self.current_store.conn.execute(
                f"SELECT id, type FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                node_ids,
            ).fetchall()
            existing_ids = {row[0] for row in type_rows}
            source_ids = {row[0] for row in type_rows if row[1] == "source"}
            not_found = sum(1 for nid in node_ids if nid not in existing_ids)
            for row in type_rows:
                if row[1] != "source":
                    not_source += 1
                    errors.append(
                        {"node_id": row[0], "error": f"node type is '{row[1]}', not 'source'"}
                    )

        # Single executemany UPDATE … RETURNING for the source nodes only.
        source_updates = [
            (item["url"], item["node_id"])
            for item in updates
            if item.get("node_id") in source_ids and item.get("url")
        ]
        if source_updates:
            updated_rows = self.current_store.conn.executemany(
                "UPDATE ohm_nodes SET url = ? WHERE id = ? AND deleted_at IS NULL "
                "AND type = 'source' RETURNING id",
                source_updates,
            )
            updated = len(updated_rows)
            for row_id, in updated_rows:
                self.current_store._log_change("ohm_nodes", row_id, "UPDATE", "L2", agent_name=agent)

        self._json_response(200, {
            "updated": updated,
            "not_found": not_found,
            "not_source": not_source,
            "errors": errors[:10],
            "total_requested": len(updates),
        })

    def _post_admin_pert_backfill(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/pert-backfill — auto-populate PERT estimates on edges.

        Derives probability_p05/p50/p95 from observation values or confidence.
        Bypasses write boundary for admin-level backfill.

        Body: {
            "edge_types": ["CAUSES", "INFLUENCES", ...],  // optional, defaults to causal types
            "method": "auto",  // "auto" | "observations" | "confidence"
            "dry_run": false   // if true, returns what would be updated without applying
        }
        """
        from ohm.inference.pert import auto_pert_from_observations, compute_pert_mean

        edge_types = body.get("edge_types", ["CAUSES", "INFLUENCES", "BLOCKS", "DEPENDS_ON", "ENABLES", "THREATENS", "SUPPORTS", "APPLIES_TO"])
        method = body.get("method", "auto")
        dry_run = body.get("dry_run", False)

        # Collect observations indexed by node_id
        obs_rows = self.current_store.conn.execute(
            "SELECT node_id, value FROM ohm_observations WHERE deleted_at IS NULL AND value IS NOT NULL"
        ).fetchall()
        obs_by_node = {}
        for row in obs_rows:
            nid, val = row[0], row[1]
            if nid not in obs_by_node:
                obs_by_node[nid] = []
            obs_by_node[nid].append(float(val))

        # Find edges that need PERT (have confidence but no p50)
        edge_rows = self.current_store.conn.execute(
            "SELECT id, edge_type, from_node, to_node, confidence, probability_p50 "
            "FROM ohm_edges WHERE deleted_at IS NULL AND probability_p50 IS NULL"
        ).fetchall()

        candidates = []
        for row in edge_rows:
            eid, etype, from_n, to_n, conf, p50 = row
            if etype not in edge_types:
                continue
            if p50 is not None:
                continue
            candidates.append({"id": eid, "edge_type": etype, "from": from_n, "to": to_n, "confidence": float(conf) if conf else None})

        # Derive PERT estimates
        updates = []
        from_obs = 0
        from_conf = 0

        for c in candidates:
            eid = c["id"]
            to_node = c["to"]
            conf = c["confidence"]

            # Try observation-based first
            obs_values = obs_by_node.get(to_node, [])
            if len(obs_values) >= 3 and method in ("auto", "observations"):
                result = auto_pert_from_observations(obs_values)
                if result["method"] != "insufficient_data":
                    updates.append({
                        "id": eid,
                        "probability_p05": result["p05"],
                        "probability_p50": result["p50"],
                        "probability_p95": result["p95"],
                        "provenance": "auto_pert_from_observations",
                    })
                    from_obs += 1
                    continue

            # Fall back to confidence-based
            if conf is not None and 0 < conf <= 1 and method in ("auto", "confidence"):
                spread = 0.3 * (1.0 - conf)
                p50 = round(float(conf), 4)
                p05 = round(max(0.01, p50 - spread / 2), 4)
                p95 = round(min(0.99, p50 + spread / 2), 4)
                if p05 >= p50:
                    p05 = round(max(0.01, p50 - 0.05), 4)
                if p50 >= p95:
                    p95 = round(min(0.99, p50 + 0.05), 4)
                updates.append({
                    "id": eid,
                    "probability_p05": p05,
                    "probability_p50": p50,
                    "probability_p95": p95,
                    "provenance": "auto_pert_from_confidence",
                })
                from_conf += 1

        if dry_run:
            self._json_response(200, {
                "status": "dry_run",
                "candidates": len(candidates),
                "from_observations": from_obs,
                "from_confidence": from_conf,
                "total_updates": len(updates),
                "sample": updates[:10],
            })
            return

        # Apply updates directly (admin bypass)
        updated = 0
        errors = []
        from ohm.validation import validate_identifier
        for item in updates:
            try:
                eid = item["id"]
                p05 = item["probability_p05"]
                p50 = item["probability_p50"]
                p95 = item["probability_p95"]
                prov = item["provenance"]
                pert_mean = compute_pert_mean(p05, p50, p95)

                self.current_store.conn.execute(
                    "UPDATE ohm_edges SET probability_p05 = ?, probability_p50 = ?, probability_p95 = ?, "
                    "probability = ?, provenance = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? "
                    "WHERE id = ? AND deleted_at IS NULL",
                    [p05, p50, p95, pert_mean, prov, agent, eid],
                )
                self.current_store._log_change("ohm_edges", eid, "UPDATE", "L3", agent_name=agent)
                updated += 1
            except Exception as e:
                errors.append({"edge_id": eid, "error": str(e)})

        self.current_store._increment_graph_generation()

        self._json_response(200, {
            "status": "ok",
            "candidates": len(candidates),
            "from_observations": from_obs,
            "from_confidence": from_conf,
            "updated": updated,
            "errors": errors[:10],
            "total_updates": len(updates),
        })

    def _get_admin_snapshots(self, path: str, qs: dict) -> None:
        """GET /admin/snapshots — list DuckLake snapshots."""
        snapshots = self.current_store.list_snapshots()
        self._json_response(200, {"snapshots": snapshots, "count": len(snapshots)})