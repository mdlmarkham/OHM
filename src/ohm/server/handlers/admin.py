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

    def _get_admin_snapshots(self, path: str, qs: dict) -> None:
        """GET /admin/snapshots — list DuckLake snapshots."""
        snapshots = self.current_store.list_snapshots()
        self._json_response(200, {"snapshots": snapshots, "count": len(snapshots)})