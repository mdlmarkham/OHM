"""Admin handler mixin — checkpoint, embeddings, snapshot, and hook endpoints."""

import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
import logging

logger = logging.getLogger(__name__)


class AdminHandlerMixin:
    """Handler mixin for administrative operations (OHM-brry)."""

    def _post_hooks(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /hooks — register a new hook.

        Hook registration stores a command that the server later executes
        (shell=False, sandboxed, attributed) — treat it as a privileged
        operation. When auth is enabled and roles are configured, only the
        ``admin`` role may register hooks. Dev mode (``no_auth``) and
        unconfigured deployments fall through to the write-access gate that
        ``_do_POST`` already enforces.

        Body: {event, command, timeout_ms?, enabled?}
        """
        from ohm.exceptions import PermissionDeniedError

        if not getattr(self, "no_auth", False) and getattr(self, "roles", None):
            from ohm.server.server import _lookup_role

            if _lookup_role(self.roles, agent, self._customer_id) != "admin":
                raise PermissionDeniedError("Hook registration requires admin role")

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

        hook_id = path[len("/hooks/") :]
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

    def _post_admin_cleanup_hooks(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/cleanup-hooks — remove ohm_hooks rows with invalid event values.

        The ohm_hooks table is tenant-writable and can accumulate corrupt rows
        where the `event` column contains a node id or other garbage instead of
        a valid hook event name. This endpoint deletes those rows so they stop
        triggering skip warnings at every hook invocation.

        Returns:
          {"deleted": int, "valid_events": list, "sample_invalid": list}
        """
        from ohm.hooks import VALID_HOOK_EVENTS

        try:
            # Build a safe IN clause with the frozenset of valid events.
            placeholders = ",".join(["?"] * len(VALID_HOOK_EVENTS))
            # Count invalid rows first so we can report an accurate number even
            # when DuckDB's cursor.rowcount is unavailable.
            count_row = self.current_store.conn.execute(
                f"""SELECT COUNT(*) FROM ohm_hooks WHERE event NOT IN ({placeholders})""",
                list(VALID_HOOK_EVENTS),
            ).fetchone()
            invalid_count = count_row[0] if count_row else 0
            # Sample a few invalid rows for the response (read-only check).
            sample_rows = self.current_store.conn.execute(
                f"""SELECT id, event, command, created_by, created_at
                    FROM ohm_hooks
                    WHERE event NOT IN ({placeholders})
                    ORDER BY created_at ASC
                    LIMIT 10""",
                list(VALID_HOOK_EVENTS),
            ).fetchall()
            sample = [
                {
                    "id": row[0],
                    "event": row[1],
                    "command": row[2][:80] if row[2] else None,
                    "created_by": row[3],
                    "created_at": str(row[4]) if row[4] else None,
                }
                for row in sample_rows
            ]
            # Delete invalid rows inside the global write lock.
            with self._write_lock:
                customer_id = self._customer_id
                if customer_id and self.tenant_manager:
                    from ohm.tenant import TenantNotFoundError

                    try:
                        write_lock = self.tenant_manager.get_write_lock(customer_id)
                    except TenantNotFoundError:
                        raise TenantNotFoundError("Tenant not found — provision this tenant before use")
                    with write_lock:
                        self.current_store.conn.execute(
                            f"""DELETE FROM ohm_hooks
                                WHERE event NOT IN ({placeholders})""",
                            list(VALID_HOOK_EVENTS),
                        )
                else:
                    self.current_store.conn.execute(
                        f"""DELETE FROM ohm_hooks
                            WHERE event NOT IN ({placeholders})""",
                        list(VALID_HOOK_EVENTS),
                    )
            deleted = invalid_count
            self._json_response(
                200,
                {
                    "deleted": deleted,
                    "valid_events": sorted(VALID_HOOK_EVENTS),
                    "sample_invalid": sample,
                },
            )
        except Exception as e:
            self._json_response(500, {"error": "cleanup_hooks_failed", "message": str(e)})

    def _get_admin_embeddings(self, path: str, qs: dict) -> None:
        """GET /admin/embeddings — batch generate embeddings."""
        try:
            from ohm.queries import update_node_embedding

            batch_size = 3
            delay_ms = 100
            ollama_url = None  # Default: use localhost Ollama
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
            if qs.get("ollama_url"):
                ollama_url = qs["ollama_url"][0]
                # OHM-ssrf: validate scheme and restrict host to allowlist
                if not ollama_url.startswith(("http://", "https://")):
                    self._json_response(400, {"error": "invalid_ollama_url", "message": "ollama_url must start with http:// or https://"})
                    return
                from urllib.parse import urlparse

                _parsed = urlparse(ollama_url)
                _allowed_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
                # Config can extend the allowlist via embeddings.allowed_hosts
                _cfg_hosts = (self.current_config or {}).get("embeddings", {}).get("allowed_hosts", [])
                _allowed_hosts.update(_cfg_hosts)
                if _parsed.hostname not in _allowed_hosts:
                    self._json_response(
                        400,
                        {
                            "error": "ssrf_blocked",
                            "message": f"ollama_url host '{_parsed.hostname}' is not in the allowed hosts list. Add it to embeddings.allowed_hosts in config to permit.",
                        },
                    )
                    return

            rows = self.current_store.execute("SELECT id, label FROM ohm_nodes WHERE embedding IS NULL AND deleted_at IS NULL")
            total_missing = len(rows)
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

            # Run embedding generation in background thread to avoid HTTP timeout
            background = qs.get("background", [""])[0].lower() in ("true", "1", "yes")

            if background:
                # Track progress in server state
                if not hasattr(self.server, "_embed_progress"):
                    self.server._embed_progress = {"status": "idle", "updated": 0, "failed": 0, "total": 0}
                self.server._embed_progress = {
                    "status": "error",
                    "updated": 0,
                    "failed": 0,
                    "total": total_missing,
                    "message": "Background embedding temporarily disabled — use synchronous batch mode with small batch_size (e.g. ?batch_size=3) to avoid timeout. Background mode causes DuckDB concurrency issues.",
                }

                self._json_response(
                    503,
                    {
                        "status": "error",
                        "total": total_missing,
                        "message": "Background embedding is temporarily disabled due to DuckDB concurrency issues. Use synchronous mode with small batch_size (e.g. ?batch_size=3) and re-call until remaining=0.",
                    },
                )
                return

                # NOTE: Background mode disabled due to DuckDB malloc corruption.
                # The background thread writes to DuckDB while the main thread also writes,
                # causing memory corruption. Use synchronous batch mode instead.
                def _background_embed(rows, store, progress):
                    from ohm.queries import update_node_embedding as _update

                    u, f = 0, 0
                    for row in rows:
                        try:
                            with store._lock:
                                if _update(store.conn, row["id"]):
                                    u += 1
                                else:
                                    f += 1
                        except Exception:
                            f += 1
                        progress["updated"] = u
                        progress["failed"] = f
                        if delay_ms > 0:
                            time.sleep(delay_ms / 1000.0)
                    progress["status"] = "done"
                    progress["updated"] = u
                    progress["failed"] = f

                t = threading.Thread(target=_background_embed, args=(rows, self.current_store, self.server._embed_progress), daemon=True)
                t.start()
                self._json_response(
                    202,
                    {
                        "status": "started",
                        "total": total_missing,
                        "message": f"Embedding generation started for {total_missing} nodes. GET /admin/embeddings/status to check progress.",
                    },
                )
                return

            # Synchronous mode (original behavior)
            updated = 0
            failed = 0
            processed = 0
            for row in rows:
                if processed >= batch_size:
                    break
                try:
                    if update_node_embedding(self.current_store.conn, row["id"], ollama_url=ollama_url):
                        updated += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                processed += 1
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)

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

    def _get_admin_embeddings_status(self, path: str, qs: dict) -> None:
        """GET /admin/embeddings/status — check progress of background embedding generation."""
        if not hasattr(self.server, "_embed_progress"):
            self._json_response(200, {"status": "never_run", "updated": 0, "failed": 0, "total": 0})
            return
        progress = self.server._embed_progress
        self._json_response(200, progress)

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
            self._json_response(
                400,
                {
                    "error": f"Schema assigns {edge_type} to {expected_layer}, not {to_layer}",
                    "expected": expected_layer,
                },
            )
            return

        try:
            result = self.current_store.conn.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE edge_type = ? AND layer = ? AND deleted_at IS NULL",
                [edge_type, from_layer],
            ).fetchone()
            count = result[0]

            self.current_store.conn.execute(
                "UPDATE ohm_edges SET layer = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE edge_type = ? AND layer = ? AND deleted_at IS NULL",
                [to_layer, agent, edge_type, from_layer],
            )

            self.current_store._log_change("ohm_edges", "bulk", "UPDATE", to_layer, agent_name=agent)
            self.current_store._increment_graph_generation()

            self._json_response(
                200,
                {
                    "status": "ok",
                    "edge_type": edge_type,
                    "from_layer": from_layer,
                    "to_layer": to_layer,
                    "moved": count,
                },
            )
        except Exception as e:
            self._json_response(500, {"error": "edge_layer_fix_failed", "message": str(e)})

    def _post_admin_observation_source_urls(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/observation-source-urls — bulk update source_url on observations (ADR-015 backfill).

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

        self._json_response(
            200,
            {
                "updated": updated,
                "not_found": not_found,
                "errors": errors[:10],
                "total_requested": len(updates),
            },
        )

    def _post_admin_source_node_urls(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/source-node-urls — bulk update url on source nodes (ADR-015 backfill).

        Source nodes are L2-immutable via regular PATCH, so this admin endpoint
        is needed to backfill URLs on source nodes created before ADR-015 enforcement.

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
        node_ids = [item.get("node_id") for item in updates if item.get("node_id") and item.get("url")]
        bad_items = [item for item in updates if not (item.get("node_id") and item.get("url"))]
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
                    errors.append({"node_id": row[0], "error": f"node type is '{row[1]}', not 'source'"})

        # Single executemany UPDATE … RETURNING for the source nodes only.
        source_updates = [(item["url"], item["node_id"]) for item in updates if item.get("node_id") in source_ids and item.get("url")]
        if source_updates:
            updated_rows = self.current_store.conn.executemany(
                "UPDATE ohm_nodes SET url = ? WHERE id = ? AND deleted_at IS NULL AND type = 'source' RETURNING id",
                source_updates,
            )
            updated = len(updated_rows)
            for (row_id,) in updated_rows:
                self.current_store._log_change("ohm_nodes", row_id, "UPDATE", "L2", agent_name=agent)

        self._json_response(
            200,
            {
                "updated": updated,
                "not_found": not_found,
                "not_source": not_source,
                "errors": errors[:10],
                "total_requested": len(updates),
            },
        )

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
        obs_rows = self.current_store.conn.execute("SELECT node_id, value FROM ohm_observations WHERE deleted_at IS NULL AND value IS NOT NULL").fetchall()
        obs_by_node = {}
        for row in obs_rows:
            nid, val = row[0], row[1]
            if nid not in obs_by_node:
                obs_by_node[nid] = []
            obs_by_node[nid].append(float(val))

        # Find edges that need PERT (have confidence but no p50)
        edge_rows = self.current_store.conn.execute("SELECT id, edge_type, from_node, to_node, confidence, probability_p50 FROM ohm_edges WHERE deleted_at IS NULL AND probability_p50 IS NULL").fetchall()

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
                    updates.append(
                        {
                            "id": eid,
                            "probability_p05": result["p05"],
                            "probability_p50": result["p50"],
                            "probability_p95": result["p95"],
                            "provenance": "auto_pert_from_observations",
                        }
                    )
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
                updates.append(
                    {
                        "id": eid,
                        "probability_p05": p05,
                        "probability_p50": p50,
                        "probability_p95": p95,
                        "provenance": "auto_pert_from_confidence",
                    }
                )
                from_conf += 1

        if dry_run:
            self._json_response(
                200,
                {
                    "status": "dry_run",
                    "candidates": len(candidates),
                    "from_observations": from_obs,
                    "from_confidence": from_conf,
                    "total_updates": len(updates),
                    "sample": updates[:10],
                },
            )
            return

        # Apply updates directly (admin bypass)
        updated = 0
        errors = []

        for item in updates:
            try:
                eid = item["id"]
                p05 = item["probability_p05"]
                p50 = item["probability_p50"]
                p95 = item["probability_p95"]
                prov = item["provenance"]
                pert_mean = compute_pert_mean(p05, p50, p95)

                self.current_store.conn.execute(
                    "UPDATE ohm_edges SET probability_p05 = ?, probability_p50 = ?, probability_p95 = ?, probability = ?, provenance = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ? AND deleted_at IS NULL",
                    [p05, p50, p95, pert_mean, prov, agent, eid],
                )
                self.current_store._log_change("ohm_edges", eid, "UPDATE", "L3", agent_name=agent)
                updated += 1
            except Exception as e:
                errors.append({"edge_id": eid, "error": str(e)})

        self.current_store._increment_graph_generation()

        self._json_response(
            200,
            {
                "status": "ok",
                "candidates": len(candidates),
                "from_observations": from_obs,
                "from_confidence": from_conf,
                "updated": updated,
                "errors": errors[:10],
                "total_updates": len(updates),
            },
        )

    def _get_admin_verification_scan(self, path: str, qs: dict) -> None:
        """GET /admin/verification-scan — scan for unverified edges and nodes.

        Per ADR-018: Verification loops ensure claims don't persist without evidence.

        Query params:
          days_threshold: minimum age in days for unverified edges (default 14)
          confidence_threshold: minimum confidence to flag (default 0.85)
          causal_only: if true, only scan CAUSES/PREDICTS/EXPECTS edges (default true)
        """
        from datetime import datetime, timedelta

        days_threshold = int(qs.get("days_threshold", ["14"])[0])
        confidence_threshold = float(qs.get("confidence_threshold", ["0.85"])[0])
        causal_only = qs.get("causal_only", ["true"])[0].lower() != "false"

        conn = self.current_store.conn

        # 1. Unverified causal edges (CAUSES, PREDICTS, EXPECTS) with no recorded outcomes
        # An edge is "unverified" if no outcome has been recorded that references
        # either the edge's from_node or to_node as a claim_node.
        causal_types = ["CAUSES", "PREDICTS", "EXPECTS"]
        if not causal_only:
            causal_types = None  # scan all edge types

        type_filter = ""
        if causal_types:
            placeholders = ",".join(["?"] * len(causal_types))
            type_filter = f"AND e.edge_type IN ({placeholders})"

        cutoff_date = (datetime.utcnow() - timedelta(days=days_threshold)).strftime("%Y-%m-%d")

        # Edges with no outcomes recorded against their from_node (the claim node)
        outcome_check = """
            SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
                   e.created_by, e.created_at,
                   fn.label AS from_label, tn.label AS to_label,
                   EXTRACT(DAY FROM CURRENT_TIMESTAMP - e.created_at) AS age_days
            FROM ohm_edges e
            LEFT JOIN ohm_nodes fn ON e.from_node = fn.id AND fn.deleted_at IS NULL
            LEFT JOIN ohm_nodes tn ON e.to_node = tn.id AND tn.deleted_at IS NULL
            WHERE e.deleted_at IS NULL
              AND e.layer = 'L3'
              {type_filter}
              AND e.created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_outcomes oc
                  WHERE oc.claim_node = e.from_node
              )
              AND fn.id IS NOT NULL
            ORDER BY e.confidence DESC, e.created_at ASC
        """.replace("{type_filter}", type_filter)

        params = causal_types + [cutoff_date] if causal_types else [cutoff_date]
        unverified_rows = conn.execute(outcome_check, params).fetchall()
        unverified_edges = []
        for row in unverified_rows:
            d = dict(zip(["id", "from_node", "to_node", "edge_type", "confidence", "created_by", "created_at", "from_label", "to_label", "age_days"], row))
            if d.get("age_days") is not None:
                d["age_days"] = round(float(d["age_days"]), 1)
            # ADR-018.4: Include age_days for agent prioritization
            if d.get("created_at"):
                try:
                    created = datetime.fromisoformat(str(d["created_at"]).replace("Z", "+00:00"))
                    d["age_days"] = round((datetime.utcnow() - created.replace(tzinfo=None)).total_seconds() / 86400, 1)
                except (ValueError, TypeError):
                    d["age_days"] = None
            else:
                d["age_days"] = None
            unverified_edges.append(d)

        # 2. High-confidence nodes with no observations
        high_conf_no_obs = conn.execute(
            """
            SELECT n.id, n.label, n.type, n.confidence, n.created_by, n.created_at,
                   COUNT(o.id) AS obs_count
            FROM ohm_nodes n
            LEFT JOIN ohm_observations o ON n.id = o.node_id AND o.deleted_at IS NULL
            WHERE n.deleted_at IS NULL
              AND n.confidence >= ?
              AND n.type NOT IN ('source', 'agent')
            GROUP BY n.id, n.label, n.type, n.confidence, n.created_by, n.created_at
            HAVING COUNT(o.id) = 0
            ORDER BY n.confidence DESC
        """,
            [confidence_threshold],
        ).fetchall()

        high_conf_nodes = []
        for row in high_conf_no_obs:
            d = dict(zip(["id", "label", "type", "confidence", "created_by", "created_at", "obs_count"], row))
            # ADR-018.4: Include age_days for sacred reference identification
            if d.get("created_at"):
                try:
                    created = datetime.fromisoformat(str(d["created_at"]).replace("Z", "+00:00"))
                    d["age_days"] = round((datetime.utcnow() - created.replace(tzinfo=None)).total_seconds() / 86400, 1)
                except (ValueError, TypeError):
                    d["age_days"] = None
            else:
                d["age_days"] = None
            high_conf_nodes.append(d)

        # 3. Source reliability scores per agent
        source_reliability = conn.execute("""
            SELECT source_agent,
                   COUNT(*) AS total_outcomes,
                   SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS accurate,
                   SUM(CASE WHEN outcome = FALSE THEN 1 ELSE 0 END) AS inaccurate,
                   CASE WHEN COUNT(*) > 0
                        THEN ROUND(CAST(SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*), 3)
                        ELSE NULL END AS p_accurate
            FROM ohm_outcomes
            GROUP BY source_agent
            ORDER BY total_outcomes DESC
        """).fetchall()

        reliability = [dict(zip(["source_agent", "total_outcomes", "accurate", "inaccurate", "p_accurate"], row)) for row in source_reliability]

        # 3. Unverified hypotheses (no TESTS edges or experiment_result observations)
        unverified_hypotheses = conn.execute(
            """
            SELECT n.id, n.label, n.type, n.confidence, n.hypothesis_status,
                   n.created_by, n.created_at
            FROM ohm_nodes n
            WHERE n.type = 'hypothesis'
              AND n.deleted_at IS NULL
              AND (n.hypothesis_status IS NULL OR n.hypothesis_status NOT IN ('verified', 'pruned', 'superseded'))
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE e.to_node = n.id AND e.edge_type = 'TESTS' AND e.deleted_at IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_observations o
                  WHERE o.node_id = n.id AND o.type = 'experiment_result' AND o.deleted_at IS NULL
              )
            ORDER BY n.confidence DESC
        """,
        ).fetchall()

        unverified_hypotheses_list = []
        for row in unverified_hypotheses:
            d = dict(zip(["id", "label", "type", "confidence", "hypothesis_status", "created_by", "created_at"], row))
            if d.get("created_at"):
                try:
                    created = datetime.fromisoformat(str(d["created_at"]).replace("Z", "+00:00"))
                    d["age_days"] = round((datetime.utcnow() - created.replace(tzinfo=None)).total_seconds() / 86400, 1)
                except (ValueError, TypeError):
                    d["age_days"] = None
            else:
                d["age_days"] = None
            unverified_hypotheses_list.append(d)

        # 4. Hypotheses with conflicting evidence (CONTRADICTS_EVIDENCE > SUPPORTS_EVIDENCE)
        conflicting_evidence = conn.execute(
            """
            SELECT n.id, n.label, n.hypothesis_status, n.confidence,
                   SUM(CASE WHEN e.edge_type = 'SUPPORTS_EVIDENCE' THEN 1 ELSE 0 END) AS supporting,
                   SUM(CASE WHEN e.edge_type = 'CONTRADICTS_EVIDENCE' THEN 1 ELSE 0 END) AS contradicting
            FROM ohm_nodes n
            JOIN ohm_edges e ON e.to_node = n.id
              AND e.edge_type IN ('SUPPORTS_EVIDENCE', 'CONTRADICTS_EVIDENCE')
              AND e.deleted_at IS NULL
            WHERE n.type = 'hypothesis' AND n.deleted_at IS NULL
            GROUP BY n.id, n.label, n.hypothesis_status, n.confidence
            HAVING SUM(CASE WHEN e.edge_type = 'CONTRADICTS_EVIDENCE' THEN 1 ELSE 0 END) >
                   SUM(CASE WHEN e.edge_type = 'SUPPORTS_EVIDENCE' THEN 1 ELSE 0 END)
            ORDER BY n.confidence DESC
        """,
        ).fetchall()

        conflicting_evidence_list = []
        for row in conflicting_evidence:
            d = dict(zip(["id", "label", "hypothesis_status", "confidence", "supporting", "contradicting"], row))
            conflicting_evidence_list.append(d)

        # 5. Source reliability scores per agent
        source_reliability = conn.execute("""
            SELECT source_agent,
                   COUNT(*) AS total_outcomes,
                   SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS accurate,
                   SUM(CASE WHEN outcome = FALSE THEN 1 ELSE 0 END) AS inaccurate,
                   CASE WHEN COUNT(*) > 0
                        THEN ROUND(CAST(SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*), 3)
                        ELSE NULL END AS p_accurate
            FROM ohm_outcomes
            GROUP BY source_agent
            ORDER BY total_outcomes DESC
        """).fetchall()

        reliability = [dict(zip(["source_agent", "total_outcomes", "accurate", "inaccurate", "p_accurate"], row)) for row in source_reliability]

        # 6. Summary statistics
        total_outcomes = conn.execute("SELECT COUNT(*) FROM ohm_outcomes").fetchone()[0]
        total_causal = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE edge_type IN ('CAUSES','PREDICTS','EXPECTS') AND deleted_at IS NULL AND layer = 'L3'").fetchone()[0]
        total_challenges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY' AND deleted_at IS NULL").fetchone()[0]
        total_l3 = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE layer = 'L3' AND deleted_at IS NULL").fetchone()[0]
        total_l2 = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE layer = 'L2' AND deleted_at IS NULL").fetchone()[0]

        challenge_ratio = round(total_challenges / max(total_l3, 1), 4)
        l3_l2_ratio = round(total_l3 / max(total_l2, 1), 1)

        # 7. Hypothesis verification summary
        total_hypotheses = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND deleted_at IS NULL").fetchone()[0]
        verified_hypotheses = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND hypothesis_status = 'verified' AND deleted_at IS NULL").fetchone()[0]
        tested_hypotheses = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND hypothesis_status = 'tested' AND deleted_at IS NULL").fetchone()[0]
        pruned_hypotheses = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND hypothesis_status = 'pruned' AND deleted_at IS NULL").fetchone()[0]
        superseded_hypotheses = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND hypothesis_status = 'superseded' AND deleted_at IS NULL").fetchone()[0]

        total_tests_edges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'TESTS' AND deleted_at IS NULL").fetchone()[0]
        total_supports_evidence = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'SUPPORTS_EVIDENCE' AND deleted_at IS NULL").fetchone()[0]
        total_contradicts_evidence = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'CONTRADICTS_EVIDENCE' AND deleted_at IS NULL").fetchone()[0]

        self._json_response(
            200,
            {
                "unverified_edges": unverified_edges[:50],  # cap at 50 for response size
                "unverified_edge_count": len(unverified_edges),
                "high_confidence_no_obs": high_conf_nodes[:50],
                "high_confidence_no_obs_count": len(high_conf_nodes),
                "unverified_hypotheses": unverified_hypotheses_list[:50],
                "unverified_hypotheses_count": len(unverified_hypotheses_list),
                "conflicting_evidence_hypotheses": conflicting_evidence_list[:50],
                "conflicting_evidence_hypotheses_count": len(conflicting_evidence_list),
                "source_reliability": reliability,
                "summary": {
                    "total_outcomes_recorded": total_outcomes,
                    "total_causal_edges": total_causal,
                    "challenge_ratio": challenge_ratio,
                    "l3_l2_ratio": l3_l2_ratio,
                    "days_threshold": days_threshold,
                    "confidence_threshold": confidence_threshold,
                    "verification_rate": round(total_outcomes / max(total_causal, 1), 3),
                    "total_hypotheses": total_hypotheses,
                    "verified_hypotheses": verified_hypotheses,
                    "tested_hypotheses": tested_hypotheses,
                    "pruned_hypotheses": pruned_hypotheses,
                    "superseded_hypotheses": superseded_hypotheses,
                    "total_tests_edges": total_tests_edges,
                    "total_supports_evidence": total_supports_evidence,
                    "total_contradicts_evidence": total_contradicts_evidence,
                    "hypothesis_verification_rate": round(verified_hypotheses / max(total_hypotheses, 1), 3),
                },
            },
        )

    def _post_admin_verification_decay(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/verification-decay — Run verification-aware confidence decay.

        Requires admin role.
        """
        self._require_admin()
        from ohm.graph.methods import apply_verification_decay

        dry_run = body.get("dry_run", True)  # Default to dry run for safety
        unverified_half_life = float(body.get("unverified_half_life_days", 30.0))
        verified_half_life = float(body.get("verified_half_life_days", 365.0))
        min_confidence = float(body.get("min_confidence", 0.1))
        grace_days = float(body.get("verification_grace_days", 14.0))

        result = apply_verification_decay(
            self.current_store.conn,
            unverified_half_life_days=unverified_half_life,
            verified_half_life_days=verified_half_life,
            min_confidence=min_confidence,
            verification_grace_days=grace_days,
            dry_run=dry_run,
        )
        self._json_response(200, result)

    def _post_admin_apply_decay(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/apply-decay — Apply confidence decay to L3/L4 edges (OHM-2x2u).

        Requires admin role.
        """
        self._require_admin()
        from ohm.queries import apply_decay_to_edges

        dry_run = body.get("dry_run", True)
        half_life_days = float(body.get("half_life_days", 30.0))
        floor = float(body.get("floor", 0.1))

        result = apply_decay_to_edges(
            self.current_store.conn,
            half_life_days=half_life_days,
            floor=floor,
            dry_run=dry_run,
            created_by=agent,
        )
        self._json_response(200, result)

    def _get_admin_snapshots(self, path: str, qs: dict) -> None:
        """GET /admin/snapshots — list DuckLake snapshots."""
        snapshots = self.current_store.list_snapshots()
        self._json_response(200, {"snapshots": snapshots, "count": len(snapshots)})

    def _post_admin_vacuum_lake(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/vacuum-lake — run VACUUM on DuckLake to prune old snapshots.

        Requires admin role.
        """
        self._require_admin()
        body.get("keep_versions", 10) if body else 10
        try:
            # Check if DuckLake is attached
            attached = self.current_store.conn.execute("SELECT database_name FROM duckdb_databases() WHERE database_name = 'ohm_lake'").fetchone()
            if not attached:
                self._json_response(200, {"status": "skipped", "message": "No DuckLake attached"})
                return

            # Get snapshot count before
            snap_before = self.current_store.conn.execute("SELECT COUNT(*) FROM ducklake_snapshots('ohm_lake')").fetchone()[0]

            # Run VACUUM — DuckLake uses VACUUM on the attached database alias
            try:
                self.current_store.conn.execute("VACUUM ohm_lake")
            except Exception:
                # DuckLake may need ALTER DATABASE for snapshot pruning
                # Try CHECKPOINT on local DB instead (flushes WAL)
                self.current_store.conn.execute("CHECKPOINT")

            # Get snapshot count after
            snap_after = self.current_store.conn.execute("SELECT COUNT(*) FROM ducklake_snapshots('ohm_lake')").fetchone()[0]

            # Also CHECKPOINT local DB
            self.current_store.conn.execute("CHECKPOINT")

            # Recheck health
            dlh = self.current_store.check_ducklake_health(alias="ohm_lake")
            total_orphans = sum(dlh.get("orphan_counts", {}).values())

            self._json_response(
                200,
                {
                    "status": "ok",
                    "snapshots_before": snap_before,
                    "snapshots_after": snap_after,
                    "snapshots_pruned": snap_before - snap_after,
                    "orphan_rows": total_orphans,
                    "sync_degraded": dlh.get("sync_degraded", False),
                },
            )
        except Exception as e:
            self._json_response(500, {"error": "vacuum_failed", "message": str(e)})

    def _get_resolve(self, path: str, qs: dict) -> None:
        """GET /resolve?query= — resolve a query to a node via alias matching, then fuzzy fallback (OHM-g0kv.4, OHM-tr71.9).

        Resolution chain:
        1. Exact alias match via resolve_node_by_alias()
        2. Prefix alias match via query_aliases()
        3. Fuzzy search via fuzzy_search() (Jaro-Winkler similarity)
        """
        from ohm.queries import resolve_node_by_alias, query_aliases

        query = qs.get("query", [""])[0]
        if not query:
            self._json_response(400, {"error": "validation_error", "message": "query parameter is required"})
            return

        # 1. Exact alias match
        node = resolve_node_by_alias(self.current_store.conn, query=query)
        if node is not None:
            self._json_response(200, {"resolved": node})
            return

        # 2. Prefix alias match
        prefix = qs.get("prefix", ["true"])[0].lower() in ("true", "1", "yes")
        if prefix:
            from ohm.validation import normalize_alias

            norm = normalize_alias(query)
            aliases = query_aliases(self.current_store.conn, prefix=norm)
            if aliases:
                self._json_response(200, {"resolved": None, "suggestions": aliases, "count": len(aliases)})
                return

        # 3. Fuzzy search fallback (OHM-tr71.9)
        fuzzy_limit = min(int(qs.get("fuzzy_limit", [5])[0]), 20)
        fuzzy_threshold = max(0.3, min(1.0, float(qs.get("fuzzy_threshold", [0.6])[0])))
        from ohm.queries import fuzzy_search

        fuzzy_results = fuzzy_search(
            self.current_store.conn,
            query=query,
            limit=fuzzy_limit,
            threshold=fuzzy_threshold,
            include_l0=False,
        )
        if fuzzy_results:
            suggestions = [{"id": r.get("id", ""), "label": r.get("label", ""), "type": r.get("type"), "similarity": r.get("distance", 0)} for r in fuzzy_results if r.get("label")]
            self._json_response(
                200,
                {
                    "resolved": None,
                    "suggestions": suggestions,
                    "count": len(suggestions),
                    "fallback": "fuzzy",
                },
            )
            return

        self._json_response(404, {"error": "not_found", "message": f"No match for '{query}' via alias, prefix, or fuzzy search"})

    def _get_alias_duplicates(self, path: str, qs: dict) -> None:
        """GET /admin/alias-duplicates — find duplicate nodes via alias/content hash (OHM-g0kv.5)."""
        from ohm.methods import detect_alias_duplicates

        limit = int(qs.get("limit", [50])[0])
        result = detect_alias_duplicates(self.current_store.conn, limit=limit)
        self._json_response(200, {"duplicates": result, "count": len(result)})

    def _get_admin_duplicates(self, path: str, qs: dict) -> None:
        """GET /admin/duplicates — combined duplicate detection (OHM-z2gp).

        Runs all three duplicate detection strategies and returns a unified
        response:
          - alias_collisions: same normalized label → different nodes
          - content_hash_collisions: same content hash → different nodes
          - semantic_duplicates: embedding cosine similarity ≥ threshold

        Query params:
            threshold: cosine similarity threshold (default 0.85)
            limit: max pairs per strategy (default 50)
        """
        from ohm.methods import detect_alias_duplicates, detect_semantic_duplicates

        limit = int(qs.get("limit", [50])[0])
        threshold = float(qs.get("threshold", [0.85])[0])

        alias_dups = detect_alias_duplicates(self.current_store.conn, limit=limit)
        semantic_dups = detect_semantic_duplicates(self.current_store.conn, similarity_threshold=threshold, limit=limit)

        # Split alias dups by kind
        alias_collisions = [d for d in alias_dups if d.get("kind") == "alias_collision"]
        hash_collisions = [d for d in alias_dups if d.get("kind") == "content_hash_collision"]

        self._json_response(
            200,
            {
                "alias_collisions": alias_collisions,
                "content_hash_collisions": hash_collisions,
                "semantic_duplicates": semantic_dups,
                "summary": {
                    "total": len(alias_collisions) + len(hash_collisions) + len(semantic_dups),
                    "alias_collisions": len(alias_collisions),
                    "content_hash_collisions": len(hash_collisions),
                    "semantic_duplicates": len(semantic_dups),
                    "threshold": threshold,
                },
            },
        )

    def _post_admin_hooks_stage(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/hooks/<stage> — run hooks for an ingestion stage (OHM-tjkx).

        Body: the payload to pass to each hook on stdin.
        Response: list of HookResult dicts with exit_code, stdout, stderr,
        duration_ms, and timed_out.
        """
        from ohm.hooks import HookRunner, VALID_HOOK_EVENTS
        from ohm.exceptions import ValidationError

        prefix = "/admin/hooks/"
        if not path.startswith(prefix):
            raise ValidationError("Invalid hook path")
        event = path[len(prefix) :]

        if event not in VALID_HOOK_EVENTS:
            raise ValidationError(f"Invalid hook event: {event!r}. Must be one of: {', '.join(sorted(VALID_HOOK_EVENTS))}")

        runner = HookRunner(self.current_store.conn)
        results = runner.run_hooks(event, body or {})
        self._json_response(
            200,
            {
                "event": event,
                "hooks_run": len(results),
                "results": [
                    {
                        "hook_id": r.hook_id,
                        "exit_code": r.exit_code,
                        "success": r.success,
                        "stdout": r.stdout[:1000],
                        "stderr": r.stderr[:1000],
                        "duration_ms": round(r.duration_ms, 2),
                        "timed_out": r.timed_out,
                    }
                    for r in results
                ],
            },
        )

    def _post_admin_merge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/merge — merge duplicate nodes (OHM-g0kv.6).

        Requires admin role.
        """
        self._require_admin()
        from ohm.exceptions import NodeNotFoundError

        keep_id = body.get("keep", "")
        merge_id = body.get("merge", "")
        if not keep_id or not merge_id:
            self._json_response(400, {"error": "validation_error", "message": "Both 'keep' and 'merge' fields are required"})
            return

        try:
            result = self.current_store.merge_nodes(keep_id, merge_id, merged_by=agent)
            self._json_response(200, result)
        except NodeNotFoundError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})
        except ValueError as e:
            self._json_response(400, {"error": "validation_error", "message": str(e)})

    def _post_admin_backfill_aliases(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/backfill-aliases — populate ohm_aliases for all existing nodes.

        Iterates all non-deleted nodes, registers normalize_alias(label) and
        normalize_alias(node_id) as aliases. Returns count of aliases created.

        OHM-g0kv Feature A.
        """
        from ohm.queries import register_alias
        from ohm.validation import normalize_alias

        conn = self.current_store.conn
        rows = conn.execute("SELECT id, label FROM ohm_nodes WHERE deleted_at IS NULL").fetchall()

        created = 0
        skipped = 0
        errors = []
        for node_id, label in rows:
            try:
                # Register normalized label as alias
                norm_label = normalize_alias(label)
                if norm_label:
                    result = register_alias(conn, alias_norm=norm_label, node_id=node_id)
                    if result.get("created"):
                        created += 1
                    else:
                        skipped += 1

                # Register normalized node_id as alias
                norm_id = normalize_alias(node_id)
                if norm_id and norm_id != norm_label:
                    result = register_alias(conn, alias_norm=norm_id, node_id=node_id)
                    if result.get("created"):
                        created += 1
                    else:
                        skipped += 1
            except Exception as e:
                errors.append({"node_id": node_id, "error": str(e)})

        self.current_store._increment_graph_generation()

        self._json_response(
            200,
            {
                "status": "ok",
                "total_nodes": len(rows),
                "aliases_created": created,
                "aliases_skipped": skipped,
                "errors": errors[:10],
            },
        )

    def _post_admin_backfill_content_hashes(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/backfill-content-hashes — populate ohm_content_hashes for source nodes.

        Iterates all source-type nodes, computes SHA-256 of url (or label+url if
        url is empty), and registers the content hash. Returns count of hashes created.

        OHM-g0kv Feature B.
        """
        from ohm.queries import register_content_hash
        from ohm.validation import compute_content_hash

        conn = self.current_store.conn
        rows = conn.execute("SELECT id, label, url FROM ohm_nodes WHERE type = 'source' AND deleted_at IS NULL").fetchall()

        created = 0
        skipped = 0
        errors = []
        for node_id, label, url in rows:
            try:
                # Compute hash from url, or label+url if url is empty
                if url:
                    content = url
                else:
                    content = f"{label or ''}{url or ''}"
                if not content.strip():
                    skipped += 1
                    continue

                content_hash = compute_content_hash(content)
                result = register_content_hash(conn, node_id=node_id, content_hash=content_hash)
                if result.get("created"):
                    created += 1
                else:
                    skipped += 1
            except Exception as e:
                errors.append({"node_id": node_id, "error": str(e)})

        self.current_store._increment_graph_generation()

        self._json_response(
            200,
            {
                "status": "ok",
                "total_source_nodes": len(rows),
                "hashes_created": created,
                "hashes_skipped": skipped,
                "errors": errors[:10],
            },
        )

    def _post_admin_backfill_source_urls(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/backfill-source-urls — copy source node URLs to observations.

        Finds observations without source_url, checks if the source node
        (referenced via REFERENCES edge from the parent node) has a url field,
        and if so copies the source node url to the observation's source_url.

        OHM-wdrg Feature B.
        """
        conn = self.current_store.conn

        # Find observations without source_url
        obs_rows = conn.execute("SELECT id, node_id FROM ohm_observations WHERE deleted_at IS NULL AND (source_url IS NULL OR source_url = '')").fetchall()

        updated = 0
        not_found = 0
        errors = []

        for obs_id, node_id in obs_rows:
            try:
                # Look for REFERENCES edges from the parent node to source nodes
                ref_edges = conn.execute(
                    "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'REFERENCES' AND deleted_at IS NULL",
                    [node_id],
                ).fetchall()

                source_url_found = None
                for (source_node_id,) in ref_edges:
                    row = conn.execute(
                        "SELECT url FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL AND url IS NOT NULL AND url != ''",
                        [source_node_id],
                    ).fetchone()
                    if row and row[0]:
                        source_url_found = row[0]
                        break

                if source_url_found:
                    conn.execute(
                        "UPDATE ohm_observations SET source_url = ? WHERE id = ? AND deleted_at IS NULL",
                        [source_url_found, obs_id],
                    )
                    self.current_store._log_change("ohm_observations", obs_id, "UPDATE", "L2", agent_name=agent)
                    updated += 1
                else:
                    not_found += 1
            except Exception as e:
                errors.append({"observation_id": obs_id, "error": str(e)})

        self.current_store._increment_graph_generation()

        self._json_response(
            200,
            {
                "status": "ok",
                "total_observations": len(obs_rows),
                "updated": updated,
                "no_source_url_found": not_found,
                "errors": errors[:10],
            },
        )

    def _get_fragment_resonance(self, path: str, qs: dict) -> None:
        """GET /admin/fragment-resonance — detect cross-agent fragment overlap (OHM-a5rz.13)."""
        from ohm.queries import detect_fragment_resonance

        min_shared = int(qs.get("min_shared", [2])[0])
        limit = int(qs.get("limit", [10])[0])
        result = detect_fragment_resonance(self.current_store.conn, min_shared=min_shared, limit=limit)
        self._json_response(200, {"resonance": result, "count": len(result)})

    def _post_admin_evict_fragments(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/evict-fragments — run fragment TTL eviction on-demand (OHM-a5rz.27).

        Requires admin role.
        """
        self._require_admin()
        from ohm.queries import evict_expired_fragments

        ttl_days = body.get("ttl_days", 30)
        try:
            ttl_days = int(ttl_days)
        except (ValueError, TypeError):
            self._json_response(400, {"error": "ttl_days must be an integer"})
            return

        result = evict_expired_fragments(self.current_store.conn, ttl_days=ttl_days)
        self._json_response(200, result)

    def _post_admin_repair_dangling(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/repair-dangling — fix edges pointing to non-existent nodes.

        Finds all edges where from_node or to_node references a node that doesn't
        exist (deleted or never created) and soft-deletes them.

        Body params:
            dry_run: If true, only report what would be fixed (default: true)
            migration: Dict mapping old node IDs to new node IDs. Edges pointing
                       to old IDs will be redirected to new IDs instead of deleted.

        Returns:
            dangling_edges: Number of dangling edges found
            redirected: Number of edges redirected (if migration provided)
            deleted: Number of edges deleted (if dry_run=false)
        """
        dry_run = body.get("dry_run", True)
        migration = body.get("migration", {})

        conn = self.current_store.conn
        with self.current_store._lock:
            # Find edges where to_node doesn't exist
            dangling_to = conn.execute("""
                SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer, e.confidence
                FROM ohm_edges e
                LEFT JOIN ohm_nodes n ON e.to_node = n.id
                WHERE e.deleted_at IS NULL AND n.id IS NULL
            """).fetchall()

            # Find edges where from_node doesn't exist
            dangling_from = conn.execute("""
                SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer, e.confidence
                FROM ohm_edges e
                LEFT JOIN ohm_nodes n ON e.from_node = n.id
                WHERE e.deleted_at IS NULL AND n.id IS NULL
            """).fetchall()

        all_dangling = list(set(dangling_to + dangling_from))
        redirected = 0
        deleted = 0
        kept = []

        for edge in all_dangling:
            edge_id, from_node, to_node, edge_type, layer, confidence = edge

            # Check if we can redirect
            new_to = migration.get(to_node, to_node)
            new_from = migration.get(from_node, from_node)
            can_redirect = to_node in migration or from_node in migration

            if can_redirect and not dry_run:
                # Delete old edge and create new one with migrated IDs
                with self.current_store._lock:
                    conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", [edge_id])
                # Create new edge with migrated IDs
                try:
                    result = self.current_store.write_edge(
                        from_node=new_from,
                        to_node=new_to,
                        edge_type=edge_type,
                        layer=layer or "L3",
                        confidence=confidence or 0.8,
                        created_by=agent,
                    )
                    redirected += 1
                except Exception:
                    kept.append(edge)
            elif not can_redirect and not dry_run:
                # Soft-delete the dangling edge
                with self.current_store._lock:
                    conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", [edge_id])
                deleted += 1
            else:
                kept.append(edge)

        result = {
            "dangling_edges": len(all_dangling),
            "redirected": redirected,
            "deleted": deleted,
            "dry_run": dry_run,
        }
        if dry_run:
            result["would_redirect"] = sum(1 for e in all_dangling if e[2] in migration or e[1] in migration)
            result["would_delete"] = len(all_dangling) - result["would_redirect"]
            result["dangling_details"] = [{"id": e[0], "from": e[1], "to": e[2], "type": e[3]} for e in all_dangling[:20]]

        self._json_response(200, result)

    def _post_admin_purge_orphans(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/purge-orphans — hard-delete orphaned nodes whose creators no longer have auth.

        Requires admin role.
        """
        self._require_admin()

        dry_run = body.get("dry_run", True)
        provenance_filter = body.get("provenance_filter")
        min_age_hours = body.get("min_age_hours", 24)
        max_observations = body.get("max_observations", 500)

        conn = self.current_store.conn

        # Get the set of currently authenticated agent names
        # self.tokens maps {token_hash: agent_name} — invert to get agent names
        configured_agents = set(self.tokens.values()) if hasattr(self, "tokens") else set()

        with self.current_store._lock:
            # Find nodes by agents not in current token config
            # If include_connected is true, find ALL such nodes (not just orphans)
            include_connected = body.get("include_connected", False)
            if include_connected:
                # Build a list of ghost agent names (in DB but not in current token config)
                ghost_agents = conn.execute("SELECT DISTINCT created_by FROM ohm_nodes WHERE deleted_at IS NULL AND created_by IS NOT NULL").fetchall()
                ghost_names = [r[0] for r in ghost_agents if r[0] not in configured_agents]
                if not ghost_names:
                    self._json_response(200, {"candidates": 0, "purged": 0, "observations_removed": 0, "edges_removed": 0, "dry_run": dry_run, "configured_agents": sorted(configured_agents), "ghost_agents": []})
                    return
                placeholders = ",".join(["?"] * len(ghost_names))
                query = f"""
                    SELECT n.id, n.created_by, n.provenance, n.created_at
                    FROM ohm_nodes n
                    WHERE n.deleted_at IS NULL AND n.created_by IN ({placeholders})
                """
                rows = conn.execute(query, ghost_names).fetchall()
            else:
                query = """
                    SELECT n.id, n.created_by, n.provenance, n.created_at,
                           COUNT(DISTINCT e.id) as edge_count,
                           (SELECT COUNT(*) FROM ohm_observations o WHERE o.node_id = n.id AND o.deleted_at IS NULL) as obs_count
                    FROM ohm_nodes n
                    LEFT JOIN ohm_edges e ON (
                        (e.from_node = n.id OR e.to_node = n.id) AND e.deleted_at IS NULL
                    )
                    WHERE n.deleted_at IS NULL
                    GROUP BY n.id, n.created_by, n.provenance, n.created_at
                    HAVING edge_count = 0
                """
                rows = conn.execute(query).fetchall()

        candidates = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)

        for row in rows:
            if include_connected:
                node_id, created_by, provenance, created_at = row
                obs_count = 0  # not queried for performance
            else:
                node_id, created_by, provenance, created_at, edge_count, obs_count = row
            # Skip if creator still has auth
            if created_by in configured_agents:
                continue
            # Skip if too recent
            try:
                created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                if created_dt > cutoff:
                    continue
            except Exception:
                continue
            # Skip if too many observations (safety)
            if obs_count > max_observations:
                continue
            # Apply provenance filter if specified
            if provenance_filter and (provenance or "") != provenance_filter and not (provenance or "").startswith(provenance_filter):
                continue
            candidates.append(
                {
                    "id": node_id,
                    "created_by": created_by,
                    "provenance": provenance,
                    "observations": obs_count,
                }
            )

        purged = 0
        obs_removed = 0
        edges_removed = 0

        if not dry_run:
            for c in candidates:
                try:
                    with self.current_store._lock:
                        # Soft-delete observations
                        r1 = conn.execute("UPDATE ohm_observations SET deleted_at = CURRENT_TIMESTAMP WHERE node_id = ? AND deleted_at IS NULL", [c["id"]])
                        obs_removed += r1.rowcount if hasattr(r1, "rowcount") else 0
                        # Soft-delete connected edges if include_connected
                        if include_connected:
                            r1b = conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL", [c["id"], c["id"]])
                            edges_removed += r1b.rowcount if hasattr(r1b, "rowcount") else 0
                        # Soft-delete the node
                        r2 = conn.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL", [c["id"]])
                        if r2.rowcount if hasattr(r2, "rowcount") else 0:
                            purged += 1
                except Exception as e:
                    logger.warning(f"Failed to purge {c['id']}: {e}")

        result = {
            "candidates": len(candidates),
            "purged": purged,
            "observations_removed": obs_removed,
            "edges_removed": edges_removed,
            "dry_run": dry_run,
            "configured_agents": sorted(configured_agents),
        }
        if dry_run and candidates:
            result["sample"] = candidates[:10]

        self._json_response(200, result)

    def _post_admin_backfill_relational_tags(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/backfill-relational-tags — backfill relational tags for all existing edges.

        Scans all non-deleted edges and adds edge-type-derived tags to both endpoints.
        This is a one-time migration for ADR-021.

        Body: {
            "dry_run": false  // if true, returns what would be updated without applying
        }
        """
        from ohm.server.relational_tags import backfill_relational_tags, RELATIONAL_TAG_MAP

        dry_run = body.get("dry_run", False)

        if dry_run:
            # Count how many edges would generate tags
            edges = self.current_store.conn.execute(
                "SELECT from_node, to_node, edge_type FROM ohm_edges WHERE deleted_at IS NULL",
            ).fetchall()
            potential_tags = 0
            for from_node, to_node, edge_type in edges:
                if edge_type in RELATIONAL_TAG_MAP:
                    for node_id in [from_node, to_node]:
                        row = self.current_store.conn.execute(
                            "SELECT tags FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                            [node_id],
                        ).fetchone()
                        if row:
                            import json

                            try:
                                existing = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
                            except (json.JSONDecodeError, TypeError):
                                existing = []
                            tag = RELATIONAL_TAG_MAP[edge_type]
                            if tag not in existing:
                                potential_tags += 1
            self._json_response(
                200,
                {
                    "dry_run": True,
                    "edges_scanned": len(edges),
                    "potential_tag_additions": potential_tags,
                    "mapped_edge_types": list(RELATIONAL_TAG_MAP.keys()),
                },
            )
            return

        result = backfill_relational_tags(self.current_store.conn)
        self._json_response(200, result)

    def _get_admin_constraint_report(self, path: str, qs: dict) -> None:
        """GET /admin/constraint-report — show constraint satisfaction rates (ADR-022).

        Returns per-layer constraint satisfaction statistics across all nodes.
        Uses batch computation (OHM-3ngi optimization) for ~100x speedup
        over per-node effective_layer() calls.

        Query parameters:
            batch: bool, default true. Set to 'false' for per-node computation
                   (slower, includes chain_validity per node).
        """
        self._require_write_auth()
        use_batch = qs.get("batch", ["true"])[0].lower() != "false"

        if use_batch:
            from ohm.graph.constraints import batch_constraint_report

            result = batch_constraint_report(self.current_store.conn)
            self._json_response(200, result)
            return

        # Slow path: per-node computation (includes chain_validity)
        from ohm.graph.constraints import (
            PROMOTION_CONSTRAINTS,
        )

        nodes = self.current_store.conn.execute("SELECT id, type FROM ohm_nodes WHERE deleted_at IS NULL").fetchall()

        layers = {"L0": {}, "L1": {}, "L2": {}, "L3": {}, "L4": {}}
        for layer_key in layers:
            layers[layer_key] = {"total": 0, "satisfied": {}, "violations": {}}

        from ohm.graph.constraints import effective_layer

        node_effective_layers = {}
        for node_id, node_type in nodes:
            eff, _ = effective_layer(self.current_store.conn, node_id)
            node_effective_layers[node_id] = eff
            if eff in layers:
                layers[eff]["total"] += 1

        transition_layer_map = {
            "L0_to_L1": "L0",
            "L1_to_L2": "L1",
            "L2_to_L3": "L2",
            "L3_to_L4": "L3",
        }

        for trans_key, src_layer in transition_layer_map.items():
            constraints = PROMOTION_CONSTRAINTS.get(trans_key, {})
            if not constraints:
                continue
            for cname, _threshold in constraints.items():
                total = 0
                satisfied = 0
                for node_id, node_type in nodes:
                    node_layer = node_effective_layers.get(node_id)
                    if node_layer != src_layer:
                        continue
                    total += 1
                    from ohm.graph.constraints import compute_constraint

                    value = compute_constraint(self.current_store.conn, node_id, cname)
                    if isinstance(_threshold, bool):
                        if bool(value) == _threshold:
                            satisfied += 1
                    elif isinstance(_threshold, (int, float)):
                        if value is not None and value >= _threshold:
                            satisfied += 1
                    else:
                        if value == _threshold:
                            satisfied += 1

                if total > 0:
                    rate = round(satisfied / total * 100, 1)
                    layers[src_layer]["satisfied"][cname] = {
                        "satisfied": satisfied,
                        "total": total,
                        "rate_pct": rate,
                    }
                    layers[src_layer]["violations"][cname] = total - satisfied

        response = {
            "constraint_report": layers,
            "summary": {},
        }
        total_nodes = sum(layer["total"] for layer in layers.values())
        total_violations = sum(sum(layer["violations"].values()) for layer in layers.values())
        response["summary"] = {
            "total_nodes": total_nodes,
            "total_violations": total_violations,
            "enforcement_mode": "advisory",
            "note": "Run with enforce_layer_gates=true in config for strict enforcement",
            "batch_computed": False,
        }
        self._json_response(200, response)

    # ── OHM-8fdb: Self-Calibration Endpoints ─────────────────────────────

    def _get_admin_learned_half_lives(self, path: str, qs: dict) -> None:
        """GET /admin/learned-half-lives — learned half-lives from supersession data.

        OHM-8fdb Feature 5: Returns learned half-lives per obs_type computed from
        supersession history. When n_samples >= 5, the learned half-life replaces
        the default. Shows comparison table with default vs learned values.
        """
        from ohm.graph.calibration import all_learned_half_lives

        conn = self.current_store.conn
        result = all_learned_half_lives(conn)

        # Compute summary
        using_learned = sum(1 for v in result.values() if not v["using_default"])
        using_default = sum(1 for v in result.values() if v["using_default"])

        self._json_response(
            200,
            {
                "learned_half_lives": result,
                "summary": {
                    "total_obs_types": len(result),
                    "using_learned": using_learned,
                    "using_default": using_default,
                    "min_samples_required": 5,
                },
            },
        )

    def _get_metrics_semantic(self, path: str, qs: dict) -> None:
        """GET /metrics/semantic — YAML-defined semantic-layer metrics.

        Returns the current values of the OHM semantic-layer metric catalog
        as JSON. Supports ?format=prometheus to return text/plain exposition.

        Query params:
          actions=true — evaluate thresholds and include a list of would-fire
          actions WITHOUT executing them (read-only).
        """
        from ohm.semantic_layer import run_metrics, run_metrics_and_actions

        conn = self.current_store.conn
        include_actions = qs.get("actions", [""])[0].lower() in ("true", "1", "yes")
        fmt = qs.get("format", [""])[0].lower()

        if include_actions:
            values = run_metrics_and_actions(conn, execute=False, use_ibis=False)
        else:
            values = {"metrics": run_metrics(conn, use_ibis=False), "actions": []}

        if fmt == "prometheus":
            lines = ["# HELP ohm_semantic_layer_metrics YAML-defined graph metrics"]
            lines.append("# TYPE ohm_semantic_layer_metrics gauge")
            for name, value in values.get("metrics", {}).items():
                lines.append(f'ohm_semantic_layer_metrics{{metric="{name}"}} {value if value is not None else "NaN"}')
            body_bytes = "\n".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        response: dict[str, Any] = {
            "metrics": values.get("metrics", {}),
            "count": len(values.get("metrics", {})),
        }
        if include_actions:
            response["actions"] = values.get("actions", [])

        self._json_response(200, response)

    def _post_metrics_semantic_actions(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /metrics/semantic/actions — evaluate thresholds and execute actions.

        Runs the semantic-layer metrics, evaluates configured thresholds, and
        creates Beads/OHM tasks for any that fire. Write auth is enforced by
        the dispatcher before this method is called.
        """
        from ohm.semantic_layer import run_metrics_and_actions

        repo_path = body.get("repo_path", "/root/olympus/OHM")
        result = run_metrics_and_actions(self.current_store.conn, repo_path=repo_path, execute=True, use_ibis=False)
        self._json_response(
            200,
            {
                "metrics": result.get("metrics", {}),
                "actions": result.get("actions", []),
                "executed": result.get("executed", []),
                "count": len(result.get("metrics", {})),
            },
        )

    # ── OHM-6lvk: Graph Health Scoring ─────────────────────────────────────

    def _get_admin_health(self, path: str, qs: dict) -> None:
        """GET /admin/health — compute a composite health score for the graph.

        Returns:
            health_score: weighted composite score (0–100)
            metrics: individual metric scores and raw values
            remediation_priorities: top-5 actions sorted by score impact
        """
        conn = self.current_store.conn

        total_nodes = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        total_edges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL").fetchone()[0]

        if total_nodes == 0:
            self._json_response(
                200,
                {
                    "health_score": 0,
                    "metrics": {},
                    "remediation_priorities": [],
                    "note": "Empty graph — no nodes to score",
                },
            )
            return

        # 1. Connectivity: ratio of nodes with >=2 edges to total (weight 0.25)
        connected = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT n.id
                FROM ohm_nodes n
                WHERE n.deleted_at IS NULL
                GROUP BY n.id
                HAVING (
                    (SELECT COUNT(*) FROM ohm_edges e WHERE e.from_node = n.id AND e.deleted_at IS NULL)
                    + (SELECT COUNT(*) FROM ohm_edges e WHERE e.to_node = n.id AND e.deleted_at IS NULL)
                ) >= 2
            )
        """).fetchone()[0]
        connectivity_ratio = connected / total_nodes

        # 2. Orphan ratio: orphans / total, inverted (weight 0.15)
        orphans = conn.execute("""
            SELECT COUNT(*) FROM ohm_nodes n
            WHERE n.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE e.deleted_at IS NULL AND (e.from_node = n.id OR e.to_node = n.id)
              )
        """).fetchone()[0]
        orphan_ratio = orphans / total_nodes
        non_orphan_ratio = 1.0 - orphan_ratio

        orphan_type_rows = conn.execute("""
            SELECT n.type, COUNT(*) as cnt
            FROM ohm_nodes n
            WHERE n.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE e.deleted_at IS NULL AND (e.from_node = n.id OR e.to_node = n.id)
              )
            GROUP BY n.type
            ORDER BY cnt DESC
        """).fetchall()
        # 3. Verification rate: verified causal edges / total causal edges (weight 0.25)
        causal_types = ("CAUSES", "PREDICTS", "EXPECTS")
        placeholders = ",".join(["?"] * len(causal_types))
        total_causal = conn.execute(
            f"SELECT COUNT(*) FROM ohm_edges WHERE edge_type IN ({placeholders}) AND deleted_at IS NULL AND layer = 'L3'",
            list(causal_types),
        ).fetchone()[0]

        if total_causal > 0:
            verified_causal = conn.execute(
                f"""
                SELECT COUNT(DISTINCT e.id) FROM ohm_edges e
                INNER JOIN ohm_outcomes o ON o.claim_node = e.from_node
                WHERE e.edge_type IN ({placeholders}) AND e.deleted_at IS NULL AND e.layer = 'L3'
            """,
                list(causal_types),
            ).fetchone()[0]
            verification_rate = verified_causal / total_causal
        else:
            verification_rate = 1.0

        # 4. Challenge health: closeness to target 5% challenge ratio (weight 0.15)
        total_l3 = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE layer = 'L3' AND deleted_at IS NULL").fetchone()[0]
        total_challenges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY' AND deleted_at IS NULL").fetchone()[0]

        challenge_target = 0.05
        if total_l3 > 0:
            challenge_ratio = total_challenges / total_l3
            # Asymmetric: penalize below target, reward above target (more challenges = healthier)
            if challenge_ratio >= challenge_target:
                challenge_score = min(1.0, 0.5 + (challenge_ratio - challenge_target) / challenge_target * 0.5)
            else:
                challenge_score = max(0.0, challenge_ratio / challenge_target)
        else:
            challenge_score = 0.0

        # 5. Source coverage: nodes with url / total (weight 0.10)
        with_url = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND url IS NOT NULL AND url != ''").fetchone()[0]
        source_coverage = with_url / total_nodes

        # 6. Layer balance: edge distribution across L1/L2/L3 (weight 0.10)
        l1_edges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE layer = 'L1' AND deleted_at IS NULL").fetchone()[0]
        l2_edges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE layer = 'L2' AND deleted_at IS NULL").fetchone()[0]
        l3_edges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE layer = 'L3' AND deleted_at IS NULL").fetchone()[0]

        if total_edges > 0:
            layer_counts = [l1_edges, l2_edges, l3_edges]
            active_layers = sum(1 for c in layer_counts if c > 0)
            if active_layers == 0:
                layer_balance_score = 0.0
            else:
                ideal_share = 1.0 / 3
                actual_shares = [c / total_edges for c in layer_counts]
                mae = sum(abs(s - ideal_share) for s in actual_shares) / 3
                layer_balance_score = max(0.0, 1.0 - mae / ideal_share)
        else:
            layer_balance_score = 0.0

        # Weights
        weights = {
            "connectivity": 0.25,
            "orphan_ratio": 0.15,
            "verification_rate": 0.25,
            "challenge_health": 0.15,
            "source_coverage": 0.10,
            "layer_balance": 0.10,
        }
        scores = {
            "connectivity": connectivity_ratio,
            "orphan_ratio": non_orphan_ratio,
            "verification_rate": verification_rate,
            "challenge_health": challenge_score,
            "source_coverage": source_coverage,
            "layer_balance": layer_balance_score,
        }

        raw_values = {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "connected_nodes": connected,
            "orphan_nodes": orphans,
            "orphan_type_breakdown": {row[0]: row[1] for row in orphan_type_rows} if orphan_type_rows else {},
            "total_causal_edges": total_causal,
            "verified_causal_edges": verified_causal if total_causal > 0 else 0,
            "total_l3_edges": total_l3,
            "total_challenges": total_challenges,
            "challenge_ratio": round(challenge_ratio, 4) if total_l3 > 0 else 0,
            "nodes_with_url": with_url,
            "l1_edges": l1_edges,
            "l2_edges": l2_edges,
            "l3_edges": l3_edges,
            "embedding_coverage": None,
            "avg_manifold_density": None,
        }

        # OHM-nnrw: embedding coverage and average manifold density
        # NOTE: The LATERAL + ORDER BY array_cosine_distance query is O(n²) and
        # hangs on large graphs (3500+ nodes). Replaced with a sampled approach
        # that computes density on a random subset of 100 nodes, each comparing
        # against 50 random peers. This gives a reasonable estimate without
        # timing out the HTTP request.
        try:
            nodes_with_embedding = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND embedding IS NOT NULL").fetchone()[0]
            raw_values["embedding_coverage"] = round(nodes_with_embedding / total_nodes, 4) if total_nodes > 0 else 0.0

            # Sampled manifold density: pick 100 random nodes with embeddings,
            # for each pick 50 random peers, compute avg cosine similarity.
            avg_density_row = conn.execute("""
                SELECT AVG(density) FROM (
                    SELECT 1.0 - AVG(array_cosine_distance(n.embedding, peer.embedding)) AS density
                    FROM (
                        SELECT id, embedding FROM ohm_nodes
                        WHERE deleted_at IS NULL AND embedding IS NOT NULL
                        ORDER BY random()
                        LIMIT 100
                    ) n,
                    LATERAL (
                        SELECT embedding FROM ohm_nodes peer
                        WHERE peer.embedding IS NOT NULL AND peer.id != n.id
                        ORDER BY random()
                        LIMIT 50
                    ) peer
                    GROUP BY n.id
                )
            """).fetchone()
            raw_values["avg_manifold_density"] = round(float(avg_density_row[0]), 4) if avg_density_row and avg_density_row[0] is not None else None
        except Exception:
            pass

        health_score = sum(weights[k] * scores[k] for k in weights)
        health_score_100 = round(health_score * 100, 1)

        metrics = {}
        for k in weights:
            metrics[k] = {
                "score": round(scores[k], 4),
                "weight": weights[k],
                "weighted_contribution": round(weights[k] * scores[k] * 100, 2),
            }

        # Remediation priorities: dependency-ordered, sorted by potential score improvement
        remediation_candidates = []

        remediation_candidates.append(
            {
                "metric": "connectivity",
                "priority": 1,
                "action": "Add edges to loosely connected nodes",
                "potential_gain": round((1.0 - scores["connectivity"]) * weights["connectivity"] * 100, 2),
                "dependency": None,
                "detail": f"{total_nodes - connected} nodes have fewer than 2 edges",
            }
        )
        remediation_candidates.append(
            {
                "metric": "verification_rate",
                "priority": 2,
                "action": "Record outcomes for causal edges",
                "potential_gain": round((1.0 - scores["verification_rate"]) * weights["verification_rate"] * 100, 2),
                "dependency": "causal_edges_exist",
                "detail": f"{total_causal - (verified_causal if total_causal > 0 else 0)} unverified causal edges" if total_causal > 0 else "No causal edges to verify",
            }
        )
        remediation_candidates.append(
            {
                "metric": "orphan_ratio",
                "priority": 3,
                "action": "Connect orphan nodes to existing nodes",
                "potential_gain": round((1.0 - scores["orphan_ratio"]) * weights["orphan_ratio"] * 100, 2),
                "dependency": None,
                "detail": f"{orphans} orphan nodes with no edges",
            }
        )
        remediation_candidates.append(
            {
                "metric": "challenge_health",
                "priority": 4,
                "action": "Add CHALLENGED_BY edges to reach 5% challenge ratio" if challenge_ratio < challenge_target else "Reduce challenge ratio — exceeds 5% target",
                "potential_gain": round((1.0 - scores["challenge_health"]) * weights["challenge_health"] * 100, 2),
                "dependency": "L3_edges_exist",
                "detail": f"Challenge ratio: {round(challenge_ratio, 4) if total_l3 > 0 else 0} (target: {challenge_target})",
            }
        )
        remediation_candidates.append(
            {
                "metric": "source_coverage",
                "priority": 5,
                "action": "Add source URLs to nodes",
                "potential_gain": round((1.0 - scores["source_coverage"]) * weights["source_coverage"] * 100, 2),
                "dependency": None,
                "detail": f"{total_nodes - with_url} nodes without source URLs",
            }
        )
        remediation_candidates.append(
            {
                "metric": "layer_balance",
                "priority": 6,
                "action": "Distribute edges across L1, L2, and L3",
                "potential_gain": round((1.0 - scores["layer_balance"]) * weights["layer_balance"] * 100, 2),
                "dependency": "edges_exist",
                "detail": f"Layer distribution: L1={l1_edges}, L2={l2_edges}, L3={l3_edges}",
            }
        )

        remediation_candidates.sort(key=lambda x: x["potential_gain"], reverse=True)
        for i, c in enumerate(remediation_candidates):
            c["rank"] = i + 1

        self._json_response(
            200,
            {
                "health_score": health_score_100,
                "metrics": metrics,
                "raw_values": raw_values,
                "remediation_priorities": remediation_candidates[:5],
            },
        )

    def _get_admin_orphan_triage(self, path: str, qs: dict) -> None:
        """GET /admin/orphan-triage — batch triage orphan nodes (OHM-jx4q).

        Scans orphan nodes and produces link suggestions for connecting them
        to the graph. Query params:
            limit: Max orphans to process (default 50)
            min_confidence: Only triage orphans with confidence >= this value
        """
        from ohm.queries import batch_orphan_triage

        conn = self.current_store.conn
        limit = int(qs.get("limit", ["50"])[0])
        min_confidence = qs.get("min_confidence", [None])[0]
        if min_confidence is not None:
            min_confidence = float(min_confidence)

        result = batch_orphan_triage(
            conn,
            limit=min(limit, 200),
            min_confidence=min_confidence,
        )
        self._json_response(200, result)

    # ── OHM-tr71: Proactive Discoverability ──────────────────────────────────

    def _get_admin_islands(self, path: str, qs: dict) -> None:
        """GET /admin/islands — find disconnected components with bridge suggestions.

        Enriches the standard /islands response with:
          - center: the most-connected node in each island
          - tags: aggregate tags across island nodes
          - bridges_suggested: candidate bridge edges to the main component

        Query params:
            min_size: Minimum island size (default 2)
            max_islands: Maximum islands to return (default 10)
            layer: Filter edges by layer
        """
        import json
        from ohm.methods import find_islands

        min_size = int(qs.get("min_size", [2])[0])
        max_islands = int(qs.get("max_islands", [10])[0])
        layer = qs.get("layer", [None])[0]

        conn = self.current_store.conn
        result = find_islands(
            conn,
            exclude_fragments=True,
            min_size=min_size,
            max_islands=max_islands,
            layer=layer,
        )

        islands = result.get("islands", [])
        result.get("main_graph_size", 0)

        # Get all node IDs of the main component for bridge suggestion
        main_component_ids = set()
        if islands:
            mainland_ids = conn.execute("SELECT n.id FROM ohm_nodes n WHERE n.deleted_at IS NULL AND n.type != 'fragment'").fetchall()
            all_node_ids = {r[0] for r in mainland_ids}
            island_node_ids = set()
            for island in islands:
                for n in island.get("nodes", []):
                    island_node_ids.add(n["id"])
            main_component_ids = all_node_ids - island_node_ids

        enriched_islands = []
        for island in islands:
            island_node_ids = {n["id"] for n in island.get("nodes", [])}
            if not island_node_ids:
                continue

            # Find center: node with most edges within the island
            center_id = None
            max_internal_degree = -1
            for nid in island_node_ids:
                internal_deg = conn.execute(
                    "SELECT COUNT(*) FROM ohm_edges e WHERE e.deleted_at IS NULL AND ((e.from_node = ? AND e.to_node IN (SELECT unnest(?::VARCHAR[]))) OR (e.to_node = ? AND e.from_node IN (SELECT unnest(?::VARCHAR[]))))",
                    [nid, list(island_node_ids), nid, list(island_node_ids)],
                ).fetchone()[0]
                if internal_deg > max_internal_degree:
                    max_internal_degree = internal_deg
                    center_id = nid

            # Collect tags from all island nodes
            tag_rows = conn.execute(
                "SELECT tags FROM ohm_nodes WHERE id IN (SELECT unnest(?::VARCHAR[])) AND deleted_at IS NULL",
                [list(island_node_ids)],
            ).fetchall()
            all_tags = set()
            for (tags_json,) in tag_rows:
                if tags_json:
                    try:
                        parsed = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
                        if isinstance(parsed, list):
                            all_tags.update(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Suggest bridges to the main component
            bridge_suggestions = []
            if main_component_ids:
                for island_nid in list(island_node_ids)[:5]:  # Limit candidates
                    tag_row = conn.execute(
                        "SELECT tags, label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                        [island_nid],
                    ).fetchone()
                    if not tag_row:
                        continue
                    island_tags_json, island_label = tag_row
                    island_tags = set()
                    if island_tags_json:
                        try:
                            parsed = json.loads(island_tags_json) if isinstance(island_tags_json, str) else (island_tags_json or [])
                            if isinstance(parsed, list):
                                island_tags = set(parsed)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # Score main component nodes by tag overlap
                    scored_bridges = []
                    for main_id in list(main_component_ids)[:50]:  # Limit for perf
                        main_row = conn.execute(
                            "SELECT tags, label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                            [main_id],
                        ).fetchone()
                        if not main_row:
                            continue
                        main_tags_json, main_label = main_row
                        main_tags = set()
                        if main_tags_json:
                            try:
                                parsed = json.loads(main_tags_json) if isinstance(main_tags_json, str) else (main_tags_json or [])
                                if isinstance(parsed, list):
                                    main_tags = set(parsed)
                            except (json.JSONDecodeError, TypeError):
                                pass

                        tag_overlap = len(island_tags & main_tags)
                        # Label similarity: case-insensitive word overlap
                        island_words = set(island_label.lower().split()) if island_label else set()
                        main_words = set(main_label.lower().split()) if main_label else set()
                        word_overlap = len(island_words & main_words)
                        score = tag_overlap * 2.0 + word_overlap * 1.0

                        if score > 0:
                            scored_bridges.append(
                                {
                                    "from": island_nid,
                                    "to": main_id,
                                    "score": round(score, 2),
                                    "shared_tags": sorted(island_tags & main_tags),
                                }
                            )

                    scored_bridges.sort(key=lambda x: x["score"], reverse=True)
                    for sb in scored_bridges[:2]:
                        bridge_suggestions.append(f"{sb['from']} → {sb['to']}")

            enriched = dict(island)
            enriched["center"] = center_id or (island["nodes"][0]["id"] if island.get("nodes") else None)
            enriched["tags"] = sorted(all_tags)[:10]
            enriched["bridges_suggested"] = bridge_suggestions[:5]
            enriched_islands.append(enriched)

        self._json_response(
            200,
            {
                "islands": enriched_islands,
                "total_islands": result.get("total_islands", 0),
                "total_orphan_nodes": result.get("orphan_count", 0),
            },
        )

    def _post_admin_sync_beads(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/sync-beads — sync Beads issues into OHM task nodes (OHM-sdrr).

        Body params (all optional):
            issues: Explicit list of Beads issue dicts to sync. If omitted,
                    issues are fetched from the ``bd`` CLI (or .beads/issues.jsonl
                    fallback).
            actor:  Agent name to attribute the writes to (default: "system").
            dry_run: If true, return what would change without modifying (default: false).

        Returns:
            Sync report: {created, updated, skipped, errors, total, dry_run}.
        """
        from ohm.integrations.beads_sync import fetch_beads_issues, sync_beads_to_ohm_tasks

        issues = body.get("issues")
        if not issues:
            issues = fetch_beads_issues()
        sync_actor = body.get("actor", "system")
        dry_run = body.get("dry_run", False)

        result = sync_beads_to_ohm_tasks(
            self.current_store.conn,
            issues,
            actor=sync_actor,
            dry_run=dry_run,
        )
        result["dry_run"] = dry_run
        self._json_response(200, result)
