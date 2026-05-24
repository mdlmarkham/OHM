"""
OHM Store — DuckDB connection management for local cache and shared backend.

Supports four modes:
1. Local mode: single DuckDB file for development or single-agent use
2. Agent mode: per-agent local DuckDB with DuckLake sync (recommended for multi-agent)
3. Quack mode: DuckDB connection with Quack server for concurrent multi-writer access
4. Remote mode: HTTP connection to ohmd daemon for multi-agent shared access

In agent mode, each agent owns a local DuckDB file for zero-latency reads/writes,
then syncs to a shared DuckLake mirror on heartbeat. This eliminates the single-writer
bottleneck of the centralized daemon.

Usage (agent mode):
    from ohm.store import OhmStore
    from ohm.schema import SchemaConfig

    # Each agent creates its own store
    store = OhmStore.for_agent(
        agent_name="metis",
        ducklake_path="/var/lib/ohm/ohm_lake.ducklake",
    )

    # Read/write locally (zero latency, no HTTP)
    store.write_node(id="concept-x", label="X", type="concept", ...)
    node = store.get_node("concept-x")

    # Sync with other agents on heartbeat
    result = store.sync_heartbeat()
    # → {"pushed": 3, "pulled": 7, "last_sync": "..."}
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from ohm.schema import DEFAULT_SCHEMA, SchemaConfig
from typing import Any, Optional

import duckdb

from ohm.exceptions import NodeNotFoundError, OHMError

logger = logging.getLogger(__name__)


class OhmStore:
    """Manages the OHM knowledge graph in DuckDB."""

    @classmethod
    def for_agent(
        cls,
        agent_name: str,
        ducklake_path: Optional[str] = None,
        ducklake_data_path: Optional[str] = None,
        schema: Optional["SchemaConfig"] = None,
        base_dir: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> "OhmStore":
        """Create a per-agent local DuckDB with DuckLake sync.

        Each agent gets its own local DuckDB file for zero-latency reads/writes.
        On sync_heartbeat(), local changes are pushed to the shared DuckLake
        mirror and other agents' changes are pulled back.

        This eliminates the single-writer bottleneck of the centralized daemon.
        Each agent can read and write locally without any HTTP calls.

        Args:
            agent_name: Agent name (e.g., "metis", "clio", "socrates").
                Used for attribution and to name the local DB file.
            ducklake_path: Path to the shared DuckLake catalog file.
                Defaults to OHM_DUCKLAKE_PATH env var, then
                /var/lib/ohm/ohm_lake.ducklake.
            ducklake_data_path: Path to DuckLake data directory.
                Defaults to OHM_DUCKLAKE_DATA env var, then
                /var/lib/ohm/ohm_lake_data/.
            schema: SchemaConfig for domain-specific validation.
            base_dir: Base directory for agent DB files.
                Defaults to ~/.ohm/agents/
            tenant_id: Optional tenant identifier for multi-tenant routing
                (OHM-xbbi). When provided, the DB path becomes
                {base_dir}/{agent_name}/{tenant_id}/ohm.duckdb.
                When None (default), uses {base_dir}/{agent_name}/ohm.duckdb
                for backward compatibility.

        Returns:
            OhmStore instance with local DB and DuckLake configured.
        """
        if base_dir is None:
            base_dir = os.environ.get("OHM_AGENTS_DIR", str(Path.home() / ".ohm" / "agents"))

        # Per-agent DB path — tenant-scoped when tenant_id provided (OHM-xbbi)
        if tenant_id is not None:
            db_path = os.path.join(base_dir, agent_name, tenant_id, "ohm.duckdb")
        else:
            db_path = os.path.join(base_dir, agent_name, "ohm.duckdb")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        # DuckLake paths
        if ducklake_path is None:
            ducklake_path = os.environ.get("OHM_DUCKLAKE_PATH", "/var/lib/ohm/ohm_lake.ducklake")
        if ducklake_data_path is None:
            ducklake_data_path = os.environ.get("OHM_DUCKLAKE_DATA", "/var/lib/ohm/ohm_lake_data/")

        # Create the store
        store = cls(
            db_path=db_path,
            agent_name=agent_name,
            schema=schema,
        )

        # Configure DuckLake paths for sync
        store.ducklake_path = ducklake_path
        store.ducklake_data_path = ducklake_data_path

        # Attach DuckLake if available
        if os.path.exists(ducklake_path):
            try:
                store.conn.execute(f"ATTACH IF NOT EXISTS '{ducklake_path}' AS ohm_lake (TYPE ducklake)")
                logger.info("DuckLake attached for agent %s at %s", agent_name, ducklake_path)
            except Exception as e:
                logger.warning("DuckLake attach failed for agent %s: %s", agent_name, e)

        # Pull any existing data from DuckLake
        if os.path.exists(ducklake_path):
            try:
                pulled = store.pull_from_ducklake(ducklake_path)
                logger.info("Agent %s: pulled %d rows from DuckLake", agent_name, pulled)
            except Exception as e:
                logger.warning("DuckLake pull failed for agent %s: %s", agent_name, e)

        logger.info("Agent %s: local DB at %s, DuckLake at %s", agent_name, db_path, ducklake_path)
        return store

    def __init__(
        self,
        db_path: Optional[str] = None,
        agent_name: str = "ohm",
        readonly: bool = False,
        quack: bool = False,
        quack_uri: str = "quack:localhost",
        quack_token_env: str = "QUACK_TOKEN",
        schema: Optional["SchemaConfig"] = None,
    ):
        """Initialize the store.

        Args:
            db_path: Path to DuckDB file. Defaults to ~/.ohm/ohm.duckdb
            agent_name: Name of the owning agent (for attribution)
            readonly: Open in read-only mode
            quack: Enable Quack server for concurrent multi-writer access.
                When True and Quack is available, starts a Quack server
                on this connection. Falls back to direct DuckDB if unavailable.
            quack_uri: Quack server URI (default: quack:localhost)
            quack_token_env: Environment variable for Quack token
            schema: SchemaConfig for domain-specific validation.
                Defaults to OHM schema if not provided.
        """
        self._lock = threading.RLock()
        self.agent_name = agent_name
        self.readonly = readonly
        self.quack = quack
        self.quack_uri = quack_uri
        self.quack_token_env = quack_token_env
        self.quack_started = False
        self.schema = schema or DEFAULT_SCHEMA

        if db_path is None:
            db_path = os.environ.get("OHM_DB_PATH", str(Path.home() / ".ohm" / "ohm.duckdb"))

        # DuckLake path for recovery (set by server.py from config)
        self.ducklake_path = os.environ.get("OHM_DUCKLAKE_PATH", "")
        self.ducklake_data_path = os.environ.get("OHM_DUCKLAKE_DATA", "")

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.conn = self._connect_with_wal_recovery(str(self.db_path), readonly)
        except (duckdb.FatalException, duckdb.IOException, duckdb.InternalException) as e:
            logger.error(f"DB connection failed: {e}. Attempting DuckLake recovery.")
            self._recover_from_ducklake(str(self.db_path))
            self.conn = self._connect_with_wal_recovery(str(self.db_path), readonly)
        self._init_schema()

        # OHM-8n9/OHM-6cz: Auto-restore from DuckLake if tables are empty.
        self._auto_restore_if_empty()

        # Try to load DuckDB markdown extension (optional)
        # Enables rich content features: read_markdown, md_to_text, etc.
        self.markdown_available = False
        try:
            self.conn.execute("INSTALL markdown FROM community")
            self.conn.execute("LOAD markdown")
            self.markdown_available = True
            logger.info("DuckDB markdown extension loaded — rich content features available")
        except Exception:
            logger.info("DuckDB markdown extension not available — rich content features disabled, OHM works fine without it")

        # Start Quack server if requested and available
        if self.quack and not self.readonly:
            self._start_quack()

    def _recover_from_ducklake(self, db_path_str: str) -> None:
        """Attempt to recover from a corrupted DB by rebuilding from DuckLake.

        When DuckDB raises FatalException (uncatchable abort at C level),
        the local DB file is corrupted and must be recreated. This method
        deletes the corrupted DB and WAL, then rebuilds from DuckLake mirror
        data if available.

        Args:
            db_path_str: Path to the corrupted DB file.
        """
        import shutil
        from datetime import datetime

        logger.warning("Recovering from corrupted DB: %s", db_path_str)

        # Back up the corrupted DB
        backup_path = f"{db_path_str}.corrupted.{datetime.now().strftime('%Y%m%d%H%M%S')}"
        try:
            shutil.copy2(db_path_str, backup_path)
            logger.info("Backed up corrupted DB to: %s", backup_path)
        except Exception:
            logger.warning("Could not back up corrupted DB")

        # Remove corrupted DB and WAL
        for path in [db_path_str, db_path_str + ".wal"]:
            if os.path.exists(path):
                os.remove(path)
                logger.info("Removed: %s", path)

        # Connect to fresh DB and rebuild from DuckLake
        try:
            conn = duckdb.connect(db_path_str)
            from .schema import initialize_schema

            initialize_schema(conn)
            logger.info("Initialized fresh schema")

            # Attach DuckLake if configured
            ducklake_path = self.ducklake_path
            if ducklake_path and os.path.exists(ducklake_path):
                try:
                    conn.execute(f"ATTACH IF NOT EXISTS '{ducklake_path}' AS ohm_lake (TYPE ducklake)")
                    logger.info("DuckLake attached for recovery")

                    # Column mapping for DuckLake mirror -> local schema
                    NODE_COLS = {
                        "id": "id",
                        "label": "label",
                        "type": "type",
                        "content": "content",
                        "url": "url",
                        "created_by": "created_by",
                        "created_at": "created_at",
                        "updated_at": "updated_at",
                        "updated_by": "updated_by",
                        "confidence": "confidence",
                        "visibility": "visibility",
                        "provenance": "provenance",
                        "tags": "tags",
                        "metadata": "metadata",
                        "priority": "priority",
                        "utility_scale": "utility_scale",
                        "utility_usd_per_day": "utility_usd_per_day",
                        "utility_currency": "utility_currency",
                    }
                    EDGE_COLS = {
                        "id": "id",
                        "from_node": "from_node",
                        "to_node": "to_node",
                        "edge_type": "edge_type",
                        "layer": "layer",
                        "confidence": "confidence",
                        "condition": "condition",
                        "probability": "probability",
                        "probability_p05": "probability_p05",
                        "probability_p50": "probability_p50",
                        "probability_p95": "probability_p95",
                        "confidence_p05": "confidence_p05",
                        "confidence_p50": "confidence_p50",
                        "confidence_p95": "confidence_p95",
                        "urgency": "urgency",
                        "challenge_of": "challenge_of",
                        "challenge_type": "challenge_type",
                        "provenance": "provenance",
                        "created_by": "created_by",
                        "created_at": "created_at",
                        "updated_at": "updated_at",
                        "updated_by": "updated_by",
                        "metadata": "metadata",
                    }

                    for table, col_map in [("ohm_nodes", NODE_COLS), ("ohm_edges", EDGE_COLS)]:
                        try:
                            # Build SELECT with type casts
                            cast_parts = []
                            for dl_col, local_col in col_map.items():
                                if local_col in ("confidence", "probability", "baseline", "value", "sigma"):
                                    cast_parts.append(f"CAST({dl_col} AS FLOAT) AS {local_col}")
                                elif local_col in ("created_at", "updated_at"):
                                    cast_parts.append(f"CAST({dl_col} AS TIMESTAMP) AS {local_col}")
                                elif local_col == "metadata":
                                    cast_parts.append(f"CAST({dl_col} AS JSON) AS {local_col}")
                                else:
                                    cast_parts.append(f'"{dl_col}" AS {local_col}')
                            select_str = ", ".join(cast_parts)
                            local_cols = ", ".join(col_map.values())

                            # Only pull non-deleted rows
                            conn.execute(f"INSERT INTO {table} ({local_cols}, deleted_at) SELECT {select_str}, NULL::TIMESTAMP FROM ohm_lake.{table} WHERE deleted_at IS NULL OR CAST(deleted_at AS VARCHAR) = ''")
                            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
                            logger.info("Recovered %d %s from DuckLake", count, table)
                        except Exception as e:
                            logger.warning("Failed to recover %s from DuckLake: %s", table, e)

                    # Detach DuckLake
                    try:
                        conn.execute("DETACH ohm_lake")
                    except Exception:
                        pass

                    # Checkpoint
                    conn.execute("CHECKPOINT")
                    logger.info("Recovery checkpoint complete")
                except Exception as e:
                    logger.warning("DuckLake recovery failed: %s", e)
            else:
                logger.warning("No DuckLake path configured, skipping mirror recovery")

            conn.close()
            logger.info("DB recovery complete")
        except Exception as e:
            logger.error("DB recovery failed: %s", e)
            # Last resort: just start with empty DB
            if os.path.exists(db_path_str):
                os.remove(db_path_str)
            conn = duckdb.connect(db_path_str)
            from .schema import initialize_schema

            initialize_schema(conn)
            conn.close()
            logger.info("Started with empty DB as fallback")

    def _auto_restore_if_empty(self) -> None:
        """Auto-restore from DuckLake if ohm_nodes is empty (OHM-8n9/OHM-6cz).

        Handles the case where the DB opened successfully after WAL deletion
        but contains no data (the WAL had committed-but-not-checkpointed data).
        Unlike _recover_from_ducklake, this does NOT delete the DB file —
        it just bulk-inserts from DuckLake into the existing empty tables.
        """
        if not self.ducklake_path:
            return

        try:
            node_count = self.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
        except Exception:
            return

        if node_count > 0:
            return

        logger.info("Graph is empty on startup — attempting DuckLake auto-restore")

        ducklake_path = self.ducklake_path
        if not ducklake_path or not os.path.exists(ducklake_path):
            logger.warning("DuckLake path not found, skipping auto-restore")
            return

        try:
            self.conn.execute(f"ATTACH IF NOT EXISTS '{ducklake_path}' AS ohm_lake (TYPE ducklake)")

            NODE_COLS = {
                "id": "id",
                "label": "label",
                "type": "type",
                "content": "content",
                "url": "url",
                "created_by": "created_by",
                "created_at": "created_at",
                "updated_at": "updated_at",
                "updated_by": "updated_by",
                "confidence": "confidence",
                "visibility": "visibility",
                "provenance": "provenance",
                "tags": "tags",
                "metadata": "metadata",
                "priority": "priority",
                "utility_scale": "utility_scale",
                "utility_usd_per_day": "utility_usd_per_day",
                "utility_currency": "utility_currency",
            }
            EDGE_COLS = {
                "id": "id",
                "from_node": "from_node",
                "to_node": "to_node",
                "edge_type": "edge_type",
                "layer": "layer",
                "confidence": "confidence",
                "condition": "condition",
                "probability": "probability",
                "probability_p05": "probability_p05",
                "probability_p50": "probability_p50",
                "probability_p95": "probability_p95",
                "confidence_p05": "confidence_p05",
                "confidence_p50": "confidence_p50",
                "confidence_p95": "confidence_p95",
                "urgency": "urgency",
                "challenge_of": "challenge_of",
                "challenge_type": "challenge_type",
                "provenance": "provenance",
                "created_by": "created_by",
                "created_at": "created_at",
                "updated_at": "updated_at",
                "updated_by": "updated_by",
                "metadata": "metadata",
            }

            for table, col_map in [("ohm_nodes", NODE_COLS), ("ohm_edges", EDGE_COLS)]:
                try:
                    cast_parts = []
                    for dl_col, local_col in col_map.items():
                        if local_col in ("confidence", "probability", "baseline", "value", "sigma", "utility_scale", "utility_usd_per_day"):
                            cast_parts.append(f"CAST({dl_col} AS FLOAT) AS {local_col}")
                        elif local_col in ("created_at", "updated_at"):
                            cast_parts.append(f"CAST({dl_col} AS TIMESTAMP) AS {local_col}")
                        elif local_col == "metadata":
                            cast_parts.append(f"CAST({dl_col} AS JSON) AS {local_col}")
                        else:
                            cast_parts.append(f'"{dl_col}" AS {local_col}')
                    select_str = ", ".join(cast_parts)
                    local_cols = ", ".join(col_map.values())

                    self.conn.execute(f"INSERT INTO {table} ({local_cols}, deleted_at) SELECT {select_str}, NULL::TIMESTAMP FROM ohm_lake.{table} WHERE deleted_at IS NULL OR CAST(deleted_at AS VARCHAR) = ''")
                    count = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
                    logger.info("Auto-restored %d %s from DuckLake", count, table)
                except Exception as e:
                    logger.warning("Failed to auto-restore %s from DuckLake: %s", table, e)

            try:
                self.conn.execute("DETACH ohm_lake")
            except Exception:
                pass

            self.conn.execute("CHECKPOINT")
            logger.info("DuckLake auto-restore completed")
        except Exception as e:
            logger.warning("DuckLake auto-restore failed: %s", e)

    def _init_schema(self):
        """Initialize the schema if not already present, including migrations."""
        if not self.readonly:
            from .schema import initialize_schema

            initialize_schema(self.conn)

    @staticmethod
    def _connect_with_wal_recovery(db_path_str: str, readonly: bool = False):
        """Connect to DuckDB with WAL corruption recovery (OHM-b5a).

        If DuckDB fails to open due to WAL replay errors, deletes the
        WAL file and retries. The WAL contains only uncommitted writes,
        so this is safe — the main DB file is intact.

        DuckDB raises InternalException (not IOException) for WAL
        replay failures (e.g., "Calling DatabaseManager::GetDefaultDatabase
        with no default database set"). Both exception types must be
        caught for reliable recovery.
        """
        try:
            return duckdb.connect(db_path_str, read_only=readonly)
        except (duckdb.IOException, duckdb.InternalException) as e:
            error_msg = str(e)
            if "WAL" in error_msg or "wal" in error_msg.lower() or "replay" in error_msg.lower():
                wal_path = db_path_str + ".wal"
                if os.path.exists(wal_path):
                    os.remove(wal_path)
                return duckdb.connect(db_path_str, read_only=readonly)
            raise

    def _start_quack(self) -> None:
        """Start Quack server if available. Sets self.quack_started on success."""
        try:
            from .quack import is_available, start_server

            if is_available(self.conn):
                start_server(
                    self.conn,
                    uri=self.quack_uri,
                    token_env=self.quack_token_env,
                )
                self.quack_started = True
            else:
                import sys

                print(
                    "Quack extension not available — running in single-writer mode",
                    file=sys.stderr,
                )
        except Exception as e:
            import sys

            print(f"Quack server failed to start: {e}", file=sys.stderr)
            print("Falling back to single-writer mode", file=sys.stderr)
            self.quack_started = False

    def _stop_quack(self) -> None:
        """Stop Quack server if it was started."""
        if self.quack_started:
            try:
                from .quack import stop_server

                stop_server(self.conn, uri=self.quack_uri)
            except Exception:
                pass
            self.quack_started = False

    def execute(self, sql: str, params: Optional[list] = None) -> list[dict[str, Any]]:
        """Execute a SQL query and return results as list of dicts."""
        with self._lock:
            if params:
                result = self.conn.execute(sql, params)
            else:
                result = self.conn.execute(sql)

            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()
        results = [dict(zip(columns, row)) for row in rows]
        # Deserialize known JSON columns
        _json_cols = {"tags", "metadata", "action_alternatives"}
        for row in results:
            for col in _json_cols & row.keys():
                if isinstance(row[col], str):
                    try:
                        row[col] = json.loads(row[col])
                    except (json.JSONDecodeError, ValueError):
                        pass
            # Add convenience aliases for edge fields
            if "from_node" in row:
                row["from"] = row["from_node"]
                row["to"] = row["to_node"]
                row["type"] = row["edge_type"]
        return results

    def execute_one(self, sql: str, params: Optional[list] = None) -> Optional[dict[str, Any]]:
        """Execute a query and return a single result or None."""
        with self._lock:
            results = self.execute(sql, params)
        row = results[0] if results else None
        # Add convenience aliases for edge fields
        if row and "from_node" in row:
            row["from"] = row["from_node"]
            row["to"] = row["to_node"]
            row["type"] = row["edge_type"]
        return row

    def _now(self) -> str:
        """Return current timestamp as ISO string."""
        from datetime import datetime

        return datetime.now(timezone.utc).isoformat()

    def write_node(
        self,
        id: str,
        label: str,
        type: str,
        content: Optional[str] = None,
        confidence: float = 1.0,
        visibility: str = "team",
        provenance: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        priority: Optional[str] = None,
        url: Optional[str] = None,
        task_status: Optional[str] = None,
        assigned_to: Optional[str] = None,
        due_date: Optional[str] = None,
        utility_scale: Optional[float] = None,
        current_best_action: Optional[str] = None,
        action_alternatives: Optional[list[str]] = None,
        utility_usd_per_day: Optional[float] = None,
        utility_currency: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create or update a node. Attributed to the given agent.

        Args:
            agent_name: Agent to attribute the write to. Defaults to self.agent_name.
            priority: Node priority (P0-P3).
            url: External URL reference for this node.
            task_status: For task nodes: open/in_progress/blocked/review/done/cancelled.
            assigned_to: For task nodes: agent assigned to this task.
            due_date: For task nodes: ISO 8601 due date string.
            utility_scale: For decision nodes: importance weight 0-1.
            current_best_action: For decision nodes: currently preferred action.
            action_alternatives: For decision nodes: list of alternative actions.
            utility_usd_per_day: For decision nodes: monetary value of resolving uncertainty (USD/day).
            utility_currency: ISO 4217 currency code for utility_usd_per_day (e.g. "USD").

        Returns a dict with the node record and a 'created' key
        indicating whether this was a new creation (True) or an
        update of an existing node (False).
        """
        actor = agent_name or self.agent_name
        metadata_json = json.dumps(metadata) if metadata else None
        tag_list = tags if tags else []
        tags_json = json.dumps(tag_list) if tag_list else None
        alternatives_json = json.dumps(action_alternatives) if action_alternatives else None
        now = self._now()

        # Check if node exists (active)
        existing = self.get_node(id)
        if existing:
            self.conn.execute(
                """
                UPDATE ohm_nodes SET
                    label = ?, type = ?, content = ?, confidence = ?,
                    visibility = ?, provenance = ?, tags = ?, metadata = ?,
                    priority = ?, url = ?, task_status = ?, assigned_to = ?,
                    due_date = ?, utility_scale = ?, current_best_action = ?,
                    action_alternatives = ?, utility_usd_per_day = ?,
                    utility_currency = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
                """,
                [
                    label,
                    type,
                    content,
                    confidence,
                    visibility,
                    provenance,
                    tags_json,
                    metadata_json,
                    priority,
                    url,
                    task_status,
                    assigned_to,
                    due_date,
                    utility_scale,
                    current_best_action,
                    alternatives_json,
                    utility_usd_per_day,
                    utility_currency,
                    now,
                    actor,
                    id,
                ],
            )
            self._log_change("ohm_nodes", id, "UPDATE", None, agent_name=actor)
            result = self.get_node(id) or {}
            result["created"] = False

            # Auto-generate embedding in background (best-effort, non-blocking)
            threading.Thread(target=self._auto_embed_node, args=(id, label, content), daemon=True).start()

            return result

        # Check if node exists but is soft-deleted (primary key collision avoidance)
        soft_deleted = self.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [id]).fetchone()
        if soft_deleted:
            # Reactivate: update the soft-deleted row with new data and clear deleted_at
            self.conn.execute(
                """
                UPDATE ohm_nodes SET
                    label = ?, type = ?, content = ?, confidence = ?,
                    visibility = ?, provenance = ?, tags = ?, metadata = ?,
                    priority = ?, url = ?, created_by = ?, task_status = ?,
                    assigned_to = ?, due_date = ?, utility_scale = ?,
                    current_best_action = ?, action_alternatives = ?,
                    utility_usd_per_day = ?, utility_currency = ?,
                    updated_at = ?, updated_by = ?,
                    deleted_at = NULL
                WHERE id = ?
                """,
                [
                    label,
                    type,
                    content,
                    confidence,
                    visibility,
                    provenance,
                    tags_json,
                    metadata_json,
                    priority,
                    url,
                    actor,
                    task_status,
                    assigned_to,
                    due_date,
                    utility_scale,
                    current_best_action,
                    alternatives_json,
                    utility_usd_per_day,
                    utility_currency,
                    now,
                    actor,
                    id,
                ],
            )
            self._log_change("ohm_nodes", id, "UPDATE", None, agent_name=actor)
            result = self.get_node(id) or {}
            result["created"] = False

            # Auto-generate embedding in background (best-effort, non-blocking)
            threading.Thread(target=self._auto_embed_node, args=(id, label, content), daemon=True).start()

            return result

        # New node
        self.conn.execute(
            """
            INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence,
                                   visibility, provenance, tags, metadata, priority, url,
                                   task_status, assigned_to, due_date,
                                   utility_scale, current_best_action, action_alternatives,
                                   utility_usd_per_day, utility_currency,
                                   created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                id,
                label,
                type,
                content,
                actor,
                confidence,
                visibility,
                provenance,
                tags_json,
                metadata_json,
                priority,
                url,
                task_status,
                assigned_to,
                due_date,
                utility_scale,
                current_best_action,
                alternatives_json,
                utility_usd_per_day,
                utility_currency,
                now,
                now,
            ],
        )
        self._log_change("ohm_nodes", id, "INSERT", None, agent_name=actor)
        result = self.get_node(id) or {}
        result["created"] = True

        # Auto-generate embedding in background (best-effort, non-blocking)
        threading.Thread(target=self._auto_embed_node, args=(id, label, content), daemon=True).start()

        return result

    def _auto_embed_node(self, node_id: str, label: str, content: str | None = None) -> None:
        """Best-effort embedding generation for a single node.

        Generates an embedding from label + content (if available).
        Silently skips if Ollama is unavailable or embedding fails.
        Never raises — embedding is not critical for node creation.
        """
        try:
            from .queries import generate_embedding

            # Prefer label + content for richer embeddings
            text = label
            if content:
                text = f"{label}: {content[:800]}"  # Use up to 800 chars for richer embeddings (model supports ~2000)
            embedding = generate_embedding(text)
            if embedding:
                self.conn.execute(
                    "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
                    [embedding, node_id],
                )
                logger.debug("Auto-generated embedding for node %s", node_id)
        except Exception as e:
            # Best-effort: never fail node creation because of embedding issues
            logger.debug("Auto-embed failed for node %s: %s", node_id, e)

    def deep_content(self, node_id: str) -> dict[str, Any]:
        """Retrieve deep content for a node.

        If the node has a URL pointing to a local markdown file and the
        DuckDB markdown extension is available, reads the full file and
        returns parsed content. Otherwise, returns the node's content field.

        This is the bridge between OHM as index and Zettelkasten as archive:
        - OHM stores 500-800 char summaries for semantic search
        - The url field points to the full note
        - deep_content() follows the link and returns the full note

        Args:
            node_id: The node to retrieve deep content for.

        Returns:
            Dict with node data plus 'deep_content' (full file content)
            and 'deep_content_type' ('markdown', 'text', or 'none').
        """
        # Get the node
        node = self.get_node(node_id)
        if not node:
            raise NodeNotFoundError(f"Node not found: {node_id}")

        result = dict(node)
        result["deep_content"] = None
        result["deep_content_type"] = "none"
        result["deep_content_source"] = None

        url = node.get("url")
        if not url:
            # No URL — just return the content as-is
            result["deep_content"] = node.get("content", "")
            result["deep_content_type"] = "text"
            return result

        # Try to read local file
        file_path = None
        if url.startswith("file://"):
            file_path = url[7:]
        elif url.startswith("/"):
            file_path = url
        else:
            # Not a local file — return content as-is
            result["deep_content"] = node.get("content", "")
            result["deep_content_type"] = "text"
            result["deep_content_source"] = url
            return result

        # Check if file exists
        if not os.path.exists(file_path):
            result["deep_content"] = node.get("content", "")
            result["deep_content_type"] = "text"
            result["deep_content_source"] = f"file not found: {file_path}"
            return result

        # If markdown extension is available and file is .md, parse it
        if self.markdown_available and file_path.endswith(".md"):
            try:
                # Read full content
                full_content = Path(file_path).read_text(encoding="utf-8")

                # Try to extract plain text for embeddings/search
                plain_text = self.conn.execute("SELECT md_to_text(?)", [full_content]).fetchone()
                plain_text = plain_text[0] if plain_text else full_content

                # Try to extract frontmatter metadata
                try:
                    metadata = self.conn.execute("SELECT md_extract_metadata(?)", [full_content]).fetchone()
                    result["frontmatter"] = metadata[0] if metadata else None
                except Exception:
                    result["frontmatter"] = None

                result["deep_content"] = plain_text
                result["deep_content_type"] = "markdown"
                result["deep_content_source"] = file_path
                return result
            except Exception as e:
                logger.debug("Markdown extension parsing failed for %s: %s", file_path, e)
                # Fall through to plain text read

        # Plain text fallback
        try:
            full_content = Path(file_path).read_text(encoding="utf-8")
            result["deep_content"] = full_content
            result["deep_content_type"] = "text"
            result["deep_content_source"] = file_path
            return result
        except Exception as e:
            result["deep_content"] = node.get("content", "")
            result["deep_content_type"] = "text"
            result["deep_content_source"] = f"read error: {e}"
            return result

    def write_edge(
        self,
        from_node: str,
        to_node: str,
        edge_type: str,
        layer: str,
        confidence: Optional[float] = None,
        condition: Optional[str] = None,
        provenance: Optional[str] = None,
        challenge_of: Optional[str] = None,
        challenge_type: Optional[str] = None,
        urgency: Optional[str] = None,
        probability: Optional[float] = None,
        probability_p05: Optional[float] = None,
        probability_p50: Optional[float] = None,
        probability_p95: Optional[float] = None,
        confidence_p05: Optional[float] = None,
        confidence_p50: Optional[float] = None,
        confidence_p95: Optional[float] = None,
        agent_name: Optional[str] = None,
        deduplicate: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Create an edge. Attributed to the given agent.

        Args:
            agent_name: Agent to attribute the write to. Defaults to self.agent_name.
            urgency: Edge urgency (critical, high, medium, low).
            probability: Objective likelihood of the outcome (0.0-1.0).
            probability_p05: PERT optimistic estimate for probability.
            probability_p50: PERT most-likely estimate for probability.
            probability_p95: PERT pessimistic estimate for probability.
            confidence_p05: PERT optimistic estimate for confidence.
            confidence_p50: PERT most-likely estimate for confidence.
            confidence_p95: PERT pessimistic estimate for confidence.
            deduplicate: If True, check for an existing non-deleted edge with the
                same (from_node, to_node, edge_type, layer) and update it instead
                of creating a duplicate. Default True.

        Enforces boundary rules:
        - L1/L2: any agent can write
        - L3/L4: creates with attribution, cannot overwrite
        - Challenge edges: create separate, don't modify
        """
        actor = agent_name or self.agent_name
        now = self._now()

        # Referential integrity: verify both endpoints exist (OHM-7298)
        if challenge_of is None:  # challenge edges can reference deleted/nonexistent nodes
            for node_id, role in ((from_node, "from_node"), (to_node, "to_node")):
                exists = self.conn.execute(
                    "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [node_id],
                ).fetchone()
                if not exists:
                    from ohm.exceptions import NodeNotFoundError

                    raise NodeNotFoundError(f"Edge {role} does not exist: {node_id}")

        # Compute PERT mean when PERT triple is provided but probability is not
        if probability is None and probability_p50 is not None:
            from ohm.inference.pert import compute_pert_mean

            p05 = probability_p05 if probability_p05 is not None else probability_p50
            p95 = probability_p95 if probability_p95 is not None else probability_p50
            probability = compute_pert_mean(p05, probability_p50, p95)

        # Deduplication: if an active edge with the same (from, to, type, layer)
        # already exists, update it instead of creating a duplicate
        if deduplicate and challenge_of is None:
            existing = self.conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = ? AND layer = ? AND deleted_at IS NULL LIMIT 1",
                [from_node, to_node, edge_type, layer],
            ).fetchone()
            if existing:
                # Update the existing edge with new values
                edge_id = existing[0]
                update_fields: list[str] = []
                update_params: list[Any] = []
                if confidence is not None:
                    update_fields.append("confidence = ?")
                    update_params.append(confidence)
                if condition is not None:
                    update_fields.append("condition = ?")
                    update_params.append(condition)
                if provenance is not None:
                    update_fields.append("provenance = ?")
                    update_params.append(provenance)
                if urgency is not None:
                    update_fields.append("urgency = ?")
                    update_params.append(urgency)
                if probability is not None:
                    update_fields.append("probability = ?")
                    update_params.append(probability)
                if probability_p05 is not None:
                    update_fields.append("probability_p05 = ?")
                    update_params.append(probability_p05)
                if probability_p50 is not None:
                    update_fields.append("probability_p50 = ?")
                    update_params.append(probability_p50)
                if probability_p95 is not None:
                    update_fields.append("probability_p95 = ?")
                    update_params.append(probability_p95)
                if confidence_p05 is not None:
                    update_fields.append("confidence_p05 = ?")
                    update_params.append(confidence_p05)
                if confidence_p50 is not None:
                    update_fields.append("confidence_p50 = ?")
                    update_params.append(confidence_p50)
                if confidence_p95 is not None:
                    update_fields.append("confidence_p95 = ?")
                    update_params.append(confidence_p95)
                update_fields.append("updated_at = ?")
                update_params.append(now)
                update_fields.append("updated_by = ?")
                update_params.append(actor)

                if update_fields:
                    update_params.append(edge_id)
                    self.conn.execute(
                        f"UPDATE ohm_edges SET {', '.join(update_fields)} WHERE id = ?",
                        update_params,
                    )
                    self._log_change("ohm_edges", edge_id, "UPDATE", layer, agent_name=actor)
                    self._increment_graph_generation()

                edge = self.execute_one(
                    "SELECT * FROM ohm_edges WHERE id = ?",
                    [edge_id],
                )
                return edge

        self.conn.execute(
            """
            INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence,
                                    condition, provenance, created_by, challenge_of,
                                    challenge_type, urgency, probability,
                                    probability_p05, probability_p50, probability_p95,
                                    confidence_p05, confidence_p50, confidence_p95,
                                    created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [from_node, to_node, layer, edge_type, confidence, condition, provenance, actor, challenge_of, challenge_type, urgency, probability, probability_p05, probability_p50, probability_p95, confidence_p05, confidence_p50, confidence_p95, now, now],
        )

        edge = self.execute_one(
            "SELECT * FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = ? AND created_by = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
            [from_node, to_node, edge_type, actor],
        )

        if edge:
            self._log_change("ohm_edges", edge["id"], "INSERT", layer, agent_name=actor)
            self._increment_graph_generation()
        return edge

    def deduplicate_edges(self, layer: str | None = None) -> int:
        """Remove duplicate edges, keeping the highest-confidence one per unique combination.

        Two edges are considered duplicates if they share the same
        (from_node, to_node, edge_type, layer) and neither is deleted.
        When confidence is equal or NULL, the most recently created edge wins.

        Args:
            layer: Optional layer filter. If provided, only deduplicate edges
                in the specified layer.

        Returns:
            Number of duplicate edges removed (soft-deleted).
        """
        now = self._now()

        # Find duplicate groups: same (from_node, to_node, edge_type, layer)
        # with more than one active edge. Keep highest-confidence edge
        # (NULL confidence treated as 0); break ties by most recent created_at.
        # OHM-s0g: Use parameterized queries instead of f-string SQL for layer.

        if layer:
            keep_ids = self.conn.execute(
                """
                SELECT keep_id FROM (
                    SELECT id as keep_id, from_node, to_node, edge_type, layer, ROW_NUMBER() OVER (
                        PARTITION BY from_node, to_node, edge_type, layer
                        ORDER BY COALESCE(confidence, 0) DESC, created_at DESC
                    ) as rn
                    FROM ohm_edges
                    WHERE deleted_at IS NULL
                      AND layer = ?
                ) WHERE rn = 1
                  AND (from_node, to_node, edge_type, layer) IN (
                      SELECT from_node, to_node, edge_type, layer
                      FROM ohm_edges
                      WHERE deleted_at IS NULL
                        AND layer = ?
                      GROUP BY from_node, to_node, edge_type, layer
                      HAVING COUNT(*) > 1
                  )
            """,
                [layer, layer],
            ).fetchall()
        else:
            keep_ids = self.conn.execute("""
                SELECT keep_id FROM (
                    SELECT id as keep_id, from_node, to_node, edge_type, layer, ROW_NUMBER() OVER (
                        PARTITION BY from_node, to_node, edge_type, layer
                        ORDER BY COALESCE(confidence, 0) DESC, created_at DESC
                    ) as rn
                    FROM ohm_edges
                    WHERE deleted_at IS NULL
                ) WHERE rn = 1
                  AND (from_node, to_node, edge_type, layer) IN (
                      SELECT from_node, to_node, edge_type, layer
                      FROM ohm_edges
                      WHERE deleted_at IS NULL
                      GROUP BY from_node, to_node, edge_type, layer
                      HAVING COUNT(*) > 1
                  )
            """).fetchall()

        if not keep_ids:
            return 0

        keep_id_list = [row[0] for row in keep_ids]

        # Find all duplicate edges that are NOT in the keep list
        placeholders = ", ".join(["?"] * len(keep_id_list))
        if layer:
            params = [layer, layer] + keep_id_list
            duplicates = self.conn.execute(
                f"""
                SELECT id FROM ohm_edges
                WHERE deleted_at IS NULL
                  AND layer = ?
                  AND (from_node, to_node, edge_type, layer) IN (
                      SELECT from_node, to_node, edge_type, layer
                      FROM ohm_edges
                      WHERE deleted_at IS NULL
                        AND layer = ?
                      GROUP BY from_node, to_node, edge_type, layer
                      HAVING COUNT(*) > 1
                  )
                  AND id NOT IN ({placeholders})
            """,
                params,
            ).fetchall()
        else:
            duplicates = self.conn.execute(
                f"""
                SELECT id FROM ohm_edges
                WHERE deleted_at IS NULL
                  AND (from_node, to_node, edge_type, layer) IN (
                      SELECT from_node, to_node, edge_type, layer
                      FROM ohm_edges
                      WHERE deleted_at IS NULL
                      GROUP BY from_node, to_node, edge_type, layer
                      HAVING COUNT(*) > 1
                  )
                  AND id NOT IN ({placeholders})
            """,
                keep_id_list,
            ).fetchall()

        if not duplicates:
            return 0

        dup_ids = [row[0] for row in duplicates]
        del_placeholders = ", ".join(["?"] * len(dup_ids))
        self.conn.execute(
            f"UPDATE ohm_edges SET deleted_at = ?, updated_at = ? WHERE id IN ({del_placeholders})",
            [now, now] + dup_ids,
        )

        for dup_id in dup_ids:
            self._log_change("ohm_edges", str(dup_id), "DELETE", layer=None, agent_name=self.agent_name)

        if dup_ids:
            self._increment_graph_generation()
        return len(dup_ids)

    def challenge_edge(
        self,
        edge_id: str,
        reason: str,
        confidence: float,
        challenge_type: str = "CHALLENGED_BY",
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Challenge an existing edge. Creates a new edge referencing the original.

        Args:
            agent_name: Agent to attribute the challenge to. Defaults to self.agent_name.

        Boundary rule: cannot modify the original edge — only create a challenge.
        Enforces that only L3/L4 edges can be challenged (via enforce_challenge_boundary).
        """
        actor = agent_name or self.agent_name
        from ohm.boundary import enforce_challenge_boundary

        original = self.get_edge(edge_id)
        if not original:
            return None

        # Enforce boundary: only L3/L4 edges can be challenged
        enforce_challenge_boundary(self.conn, actor, edge_id)

        return self.write_edge(
            from_node=original["to_node"],
            to_node=original["from_node"],
            edge_type=challenge_type,
            layer=original["layer"],
            confidence=confidence,
            provenance=reason,
            challenge_of=edge_id,
            challenge_type=challenge_type,
            agent_name=actor,
        )

    def update_edge_confidence(
        self,
        edge_id: str,
        new_confidence: float,
        reason: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update confidence on an edge owned by the given agent.

        Args:
            agent_name: Agent performing the update. Defaults to self.agent_name.

        Boundary rule: only the owning agent can update their own edges.
        """
        actor = agent_name or self.agent_name
        edge = self.get_edge(edge_id)
        if not edge:
            return None

        # Enforce ownership
        if edge["created_by"] != actor:
            raise PermissionError(f"Cannot update edge {edge_id}: owned by {edge['created_by']}, not {actor}. Use challenge_edge instead.")

        now = self._now()
        self.conn.execute(
            "UPDATE ohm_edges SET confidence = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            [new_confidence, now, actor, edge_id],
        )

        self._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"], agent_name=actor)
        self._increment_graph_generation()
        return self.get_edge(edge_id)

    def write_observation(
        self,
        node_id: str,
        type: str,
        value: Optional[float] = None,
        baseline: Optional[float] = None,
        sigma: Optional[float] = None,
        source: Optional[str] = None,
        edge_id: Optional[str] = None,
        notes: Optional[str] = None,
        source_name: Optional[str] = None,
        source_url: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Create an observation. Attributed to the given agent.

        Args:
            agent_name: Agent to attribute the observation to. Defaults to self.agent_name.
            notes: Optional free-text notes for the observation.
            source_name: Name of the source (e.g., 'Reuters').
            source_url: URL of the source (e.g., 'https://reuters.com/...').
        """
        actor = agent_name or self.agent_name
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_observations
                (node_id, edge_id, type, value, baseline, sigma, source,
                 created_by, created_at, notes, source_name, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [node_id, edge_id, type, value, baseline, sigma, source, actor, now, notes, source_name, source_url],
        )

        obs = self.execute_one(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND created_by = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
            [node_id, actor],
        )
        if obs:
            self._log_change("ohm_observations", obs["id"], "INSERT", None, agent_name=actor)
        return obs

    def update_agent_state(
        self,
        current_focus: Optional[str] = None,
        active_patterns: Optional[list[str]] = None,
        available_services: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update the given agent's state in the hive mind awareness layer.

        Args:
            agent_name: Agent whose state to update. Defaults to self.agent_name.
        """
        actor = agent_name or self.agent_name
        patterns_json = json.dumps(active_patterns or [])
        services_json = json.dumps(available_services or [])
        now = self._now()

        # Check if agent state exists
        existing = self.get_agent_state(actor)
        if existing:
            self.conn.execute(
                """
                UPDATE ohm_agent_state SET
                    current_focus = ?, active_patterns = ?, available_services = ?,
                    current_session_id = ?, last_sync = ?, updated_at = ?
                WHERE agent_name = ?
                """,
                [current_focus, patterns_json, services_json, session_id, now, now, actor],
            )
        else:
            self.conn.execute(
                """
                INSERT INTO ohm_agent_state (agent_name, current_focus, active_patterns,
                                               confidence_threshold, available_services,
                                               current_session_id, last_sync, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [actor, current_focus, patterns_json, 0.7, services_json, session_id, now, now],
            )

        return self.get_agent_state(actor)

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        """Get a node by ID."""
        return self.execute_one("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id])

    def get_edge(self, edge_id: str) -> Optional[dict[str, Any]]:
        """Get an edge by ID."""
        return self.execute_one("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [edge_id])

    def delete_node(self, node_id: str, deleted_by: str) -> dict[str, Any]:
        """Soft-delete a node and all its associated edges and observations.

        Marks the node and its edges as deleted (deleted_at IS NOT NULL)
        rather than hard-deleting, because DuckDB index deletion fails when
        DuckLake mirror tables are attached ("Failed to delete all rows from
        index. Only deleted 0 out of 1 rows").

        Soft-deleted nodes are excluded from queries by default.

        Raises NodeNotFoundError if the node doesn't exist.
        """
        from ohm.exceptions import NodeNotFoundError

        node = self.get_node(node_id)
        if not node:
            raise NodeNotFoundError(f"Node not found: {node_id}")

        now = self._now()

        # Soft-delete edges (mark as deleted)
        edges_from = self.conn.execute("UPDATE ohm_edges SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE from_node = ? AND deleted_at IS NULL", [now, now, deleted_by, node_id]).fetchone()
        edges_to = self.conn.execute("UPDATE ohm_edges SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE to_node = ? AND deleted_at IS NULL", [now, now, deleted_by, node_id]).fetchone()
        edges_deleted = (edges_from[0] if edges_from else 0) + (edges_to[0] if edges_to else 0)

        # Soft-delete observations
        obs_result = self.conn.execute("UPDATE ohm_observations SET deleted_at = ? WHERE node_id = ? AND deleted_at IS NULL", [now, node_id])
        obs_deleted = obs_result.fetchone()
        obs_count = obs_deleted[0] if obs_deleted else 0

        # Soft-delete the node
        self.conn.execute("UPDATE ohm_nodes SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE id = ?", [now, now, deleted_by, node_id])
        self._log_change("ohm_nodes", node_id, "DELETE", None, agent_name=deleted_by)

        return {
            "deleted": node_id,
            "type": "node",
            "edges_removed": edges_deleted,
            "observations_removed": obs_count,
            "soft_delete": True,
        }

    def delete_edge(self, edge_id: str, deleted_by: str) -> dict[str, Any]:
        """Soft-delete an edge by ID.

        Marks the edge as deleted rather than hard-deleting, because DuckDB
        index deletion fails with DuckLake mirror tables attached.

        Raises EdgeNotFoundError if the edge doesn't exist.
        """
        from ohm.exceptions import EdgeNotFoundError

        edge = self.get_edge(edge_id)
        if not edge:
            raise EdgeNotFoundError(f"Edge not found: {edge_id}")

        now = self._now()

        # Soft-delete observations referencing this edge
        self.conn.execute("UPDATE ohm_observations SET deleted_at = ? WHERE edge_id = ? AND deleted_at IS NULL", [now, edge_id])

        # Soft-delete the edge
        self.conn.execute("UPDATE ohm_edges SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE id = ?", [now, now, deleted_by, edge_id])
        self._log_change("ohm_edges", edge_id, "DELETE", None, agent_name=deleted_by)
        self._increment_graph_generation()

        return {
            "deleted": edge_id,
            "type": "edge",
            "soft_delete": True,
        }

    def get_agent_state(self, agent_name: str) -> Optional[dict[str, Any]]:
        """Get an agent's current state."""
        return self.execute_one("SELECT * FROM ohm_agent_state WHERE agent_name = ?", [agent_name])

    def who_is_working_on(self, topic: str) -> list[dict[str, Any]]:
        """Find agents working on a topic."""
        return self.execute(
            """SELECT * FROM ohm_agent_state
               WHERE current_focus ILIKE ?
               OR active_patterns::VARCHAR ILIKE ?""",
            [f"%{topic}%", f"%{topic}%"],
        )

    def status(self) -> dict[str, Any]:
        """Get graph status: node count, edge count, agent count, last sync."""
        nc = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_nodes WHERE deleted_at IS NULL")
        ec = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_edges WHERE deleted_at IS NULL")
        oc = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_observations WHERE deleted_at IS NULL")
        ac = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_agent_state")
        node_count = nc["cnt"] if nc else 0
        edge_count = ec["cnt"] if ec else 0
        obs_count = oc["cnt"] if oc else 0
        agent_count = ac["cnt"] if ac else 0

        edges_by_layer = self.execute("SELECT layer, COUNT(*) AS cnt FROM ohm_edges WHERE deleted_at IS NULL GROUP BY layer ORDER BY layer")

        return {
            "node_count": node_count,
            "edge_count": edge_count,
            "observation_count": obs_count,
            "agent_count": agent_count,
            "edges_by_layer": {row["layer"]: row["cnt"] for row in edges_by_layer},
            "db_path": str(self.db_path),
        }

    def _log_change(
        self,
        table_name: str,
        row_id: str,
        operation: str,
        layer: Optional[str],
        agent_name: Optional[str] = None,
    ):
        """Log a change to both the change log and the change feed.

        ohm_change_log is the internal audit trail (used by push_to_ducklake).
        ohm_change_feed is the agent-facing change feed (used by listen() and SSE /events).
        Both must be populated for agents to see each other's writes.

        Args:
            agent_name: Agent to attribute the change to. Defaults to self.agent_name.
        """
        actor = agent_name or self.agent_name
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_change_log (table_name, row_id, operation, agent_name, layer, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [table_name, row_id, operation, actor, layer, now],
        )
        # Also populate the agent-facing change feed (non-critical: skip if table missing)
        try:
            self.conn.execute(
                """
                INSERT INTO ohm_change_feed (table_name, row_id, operation, agent_name, occurred_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [table_name, row_id, operation, actor, now],
            )
        except Exception:
            pass
        # Update agent's last_sync so they appear in active_agents
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM ohm_agent_state WHERE agent_name = ?",
            [actor],
        ).fetchone()
        if existing and existing[0] > 0:
            self.conn.execute(
                "UPDATE ohm_agent_state SET last_sync = ?, updated_at = ? WHERE agent_name = ?",
                [now, now, actor],
            )
        else:
            self.conn.execute(
                """INSERT INTO ohm_agent_state (agent_name, last_sync, updated_at)
                   VALUES (?, ?, ?)""",
                [actor, now, now],
            )

    def _increment_graph_generation(self) -> int:
        """Increment the graph_generation counter and return the new value.

        Called after any edge mutation (insert/update/delete) to invalidate
        cached Bayesian networks. Returns the new generation number.

        Returns:
            The new generation number after incrementing.
        """
        self.conn.execute("UPDATE ohm_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS VARCHAR) WHERE key = 'graph_generation'")
        result = self.conn.execute("SELECT CAST(value AS INTEGER) FROM ohm_meta WHERE key = 'graph_generation'").fetchone()
        return result[0] if result else 0

    def close(self):
        """Close the DuckDB connection. Stops Quack server if running.

        Runs CHECKPOINT before closing to flush WAL to the main DB file,
        reducing the risk of data loss on hard shutdown (OHM-8n9).
        """
        self._stop_quack()
        try:
            self.conn.execute("CHECKPOINT")
        except Exception:
            pass
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ── DuckLake Attachment ────────────────────────────────────────────

    def attach_ducklake(
        self,
        catalog_path: Optional[str] = None,
        data_path: Optional[str] = None,
        alias: str = "ohm_lake",
    ) -> bool:
        """Attach a DuckLake catalog to this store's connection.

        Creates mirror tables (ohm_nodes, ohm_edges, ohm_observations,
        ohm_change_feed) in the DuckLake schema without PRIMARY KEY
        constraints (DuckLake limitation). Uniqueness is enforced in
        application code.

        Args:
            catalog_path: Path to DuckLake catalog file.
                If None, uses OHM_DUCKLAKE_PATH env var.
            data_path: Path for Parquet data files.
                If None, defaults to a 'data' subdirectory next to catalog.
            alias: Database alias for the attached catalog.

        Returns:
            True if DuckLake was attached successfully, False if
            the DuckLake extension is not available.
        """
        from .db import attach_ducklake

        if catalog_path is None:
            catalog_path = os.environ.get("OHM_DUCKLAKE_PATH")
        if not catalog_path:
            return False

        return attach_ducklake(
            self.conn,
            catalog_path=catalog_path,
            data_path=data_path,
            alias=alias,
        )

    # ── DuckLake Sync ────────────────────────────────────────────────

    def sync_heartbeat(
        self,
        ducklake_path: Optional[str] = None,
        alias: str = "ohm_lake",
    ) -> dict[str, Any]:
        """Push local changes to DuckLake and pull remote changes.

        This is the main sync method called on each agent heartbeat.
        It:
        1. Pushes local changes to DuckLake (if path configured)
        2. Pulls changes from DuckLake (if path configured)
        3. Updates last_sync timestamp

        When DuckLake is attached as an alias on the current connection,
        sync uses mirror tables (ohm_lake.ohm_nodes, etc.) directly.
        Otherwise, falls back to the legacy separate-connection approach.

        Args:
            ducklake_path: Optional path to DuckLake database.
                If None, uses OHM_DUCKLAKE_PATH env var.
                If neither set, sync is a no-op (local-only mode).
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Dict with sync results: pushed_count, pulled_count, last_sync.
        """
        if ducklake_path is None:
            ducklake_path = os.environ.get("OHM_DUCKLAKE_PATH")

        pushed = 0
        pulled = 0
        last_sync = None

        # Check if DuckLake is attached on this connection (OHM-0ku fix)
        # Even without ducklake_path, if the catalog is attached, we can sync.
        ducklake_attached = False
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
            ducklake_attached = attached is not None
        except Exception:
            pass

        if ducklake_path or ducklake_attached:
            # Push local changes to DuckLake
            pushed = self.push_to_ducklake(ducklake_path or "", alias=alias)
            # Pull remote changes from DuckLake
            pulled = self.pull_from_ducklake(ducklake_path or "", alias=alias)

        # Update last_sync timestamp — ensure agent row exists
        existing = self.conn.execute(
            "SELECT 1 FROM ohm_agent_state WHERE agent_name = ?",
            [self.agent_name],
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE ohm_agent_state SET last_sync = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE agent_name = ?",
                [self.agent_name],
            )
        else:
            self.conn.execute(
                "INSERT INTO ohm_agent_state (agent_name, last_sync, updated_at) VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                [self.agent_name],
            )
        row = self.conn.execute(
            "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
            [self.agent_name],
        ).fetchone()
        if row:
            last_sync = row[0]

        return {
            "pushed": pushed,
            "pulled": pulled,
            "last_sync": last_sync,
            "agent": self.agent_name,
        }

    def push_to_ducklake(self, ducklake_path: str, alias: str = "ohm_lake") -> int:
        """Push local data to DuckLake shared backend via mirror tables.

        Uses the attached DuckLake catalog (ohm_lake alias) to sync
        local ohm_nodes, ohm_edges, and ohm_observations to mirror
        tables. Falls back to the old change-feed approach if DuckLake
        is not attached.

        The sync strategy:
        1. Check if DuckLake alias is attached
        2. If attached, use sync_to_ducklake() for mirror table sync
        3. If not attached, fall back to separate DB connection

        Args:
            ducklake_path: Path to DuckLake database (used for fallback).
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Number of rows synced.
        """
        # Check if DuckLake alias is attached on this connection
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
        except Exception:
            attached = None

        if attached:
            # New approach: sync via attached mirror tables
            result = self.sync_to_ducklake(alias=alias)
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            self._set_last_push_timestamp(now)
            return result

        # Fallback: old approach using separate DB connection
        return self._push_to_ducklake_legacy(ducklake_path)

    def sync_to_ducklake(self, alias: str = "ohm_lake") -> int:
        """Sync local data to DuckLake mirror tables.

        Copies new/changed rows from local DuckDB to the attached
        DuckLake catalog's mirror tables. Uses upsert logic:
        - INSERT rows that don't exist in DuckLake
        - UPDATE rows that have changed since last sync

        Also performs initial sync if mirror tables are empty.

        Args:
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Total number of rows synced (inserted + updated).
        """
        total_synced = 0

        # Get last sync timestamp
        last_push = self._get_last_push_timestamp()

        for table in ["ohm_nodes", "ohm_edges", "ohm_observations"]:
            try:
                # Check if mirror table has any data (for initial sync)
                mirror_count = self.conn.execute(f"SELECT COUNT(*) FROM {alias}.{table}").fetchone()[0]  # type: ignore[index]

                if mirror_count == 0:
                    # Initial sync: copy all rows
                    synced = self._initial_sync_table(table, alias)
                else:
                    # Incremental sync: copy new/changed rows
                    synced = self._incremental_sync_table(table, alias, last_push)
                total_synced += synced
            except Exception as e:
                # Mirror table may not exist yet — skip
                logger.debug(f"Sync of {table} to {alias} failed: {e}")

        # Also sync change feed
        try:
            synced = self._sync_change_feed(alias, last_push)
            total_synced += synced
        except Exception as e:
            logger.debug(f"Sync of change_feed to {alias} failed: {e}")

        return total_synced

    def _initial_sync_table(self, table: str, alias: str) -> int:
        """Perform initial full sync of a table to DuckLake mirror.

        Args:
            table: Local table name (e.g., 'ohm_nodes').
            alias: DuckLake alias.

        Returns:
            Number of rows inserted.
        """
        # Get column lists using duckdb_columns() for DuckLake tables
        local_cols = self.conn.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}' ORDER BY ordinal_position").fetchall()
        # Deduplicate while preserving order (information_schema may return duplicates)
        local_col_names = list(dict.fromkeys(c[0] for c in local_cols))

        mirror_cols = self.conn.execute(f"SELECT column_name FROM duckdb_columns() WHERE database_name = '{alias}' AND table_name = '{table}'").fetchall()
        mirror_col_names = [c[0] for c in mirror_cols]

        # Use intersection of local and mirror columns
        common_cols = [c for c in local_col_names if c in mirror_col_names]
        if not common_cols:
            return 0
        common_str = ", ".join(common_cols)

        # Delete any existing rows from mirror (handles re-sync after crash/restart)
        # then INSERT all active (non-soft-deleted) local rows
        self.conn.execute(f"DELETE FROM {alias}.{table}")

        # Filter out soft-deleted rows from sync
        if "deleted_at" in local_col_names:
            self.conn.execute(f"INSERT INTO {alias}.{table} ({common_str}) SELECT {common_str} FROM {table} WHERE deleted_at IS NULL")
        else:
            self.conn.execute(f"INSERT INTO {alias}.{table} ({common_str}) SELECT {common_str} FROM {table}")

        count = self.conn.execute(f"SELECT COUNT(*) FROM {alias}.{table}").fetchone()[0]  # type: ignore[index]
        return count

    def _incremental_sync_table(self, table: str, alias: str, last_push: str | None) -> int:
        """Perform incremental sync of changed rows to DuckLake mirror.

        Uses updated_at (or created_at for observations) to find rows
        that have changed since the last sync.

        Args:
            table: Local table name.
            alias: DuckLake alias.
            last_push: ISO timestamp of last push, or None for full sync.

        Returns:
            Number of rows synced.
        """
        # Determine timestamp column
        ts_col = "updated_at" if table != "ohm_observations" else "created_at"

        # Get column lists using duckdb_columns() for DuckLake tables
        local_cols = self.conn.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}' ORDER BY ordinal_position").fetchall()
        # Deduplicate while preserving order (information_schema may return duplicates)
        col_names = list(dict.fromkeys(c[0] for c in local_cols))

        mirror_cols = self.conn.execute(f"SELECT column_name FROM duckdb_columns() WHERE database_name = '{alias}' AND table_name = '{table}'").fetchall()
        mirror_col_names = [c[0] for c in mirror_cols]

        common_cols = [c for c in col_names if c in mirror_col_names]
        if not common_cols:
            return 0
        common_str = ", ".join(common_cols)

        # Build WHERE clause: filter soft-deleted rows + timestamp filter
        deleted_filter = " AND deleted_at IS NULL" if "deleted_at" in col_names else ""

        if last_push:
            # Find rows changed since last push (excluding soft-deleted)
            changed_rows = self.conn.execute(
                f"SELECT id FROM {table} WHERE {ts_col} > ?::TIMESTAMP{deleted_filter}",
                [last_push],
            ).fetchall()
        else:
            # No last push — sync everything (excluding soft-deleted)
            changed_rows = self.conn.execute(f"SELECT id FROM {table} WHERE 1=1{deleted_filter}").fetchall()

        if not changed_rows:
            return 0

        changed_ids = [r[0] for r in changed_rows]

        # Delete stale rows from mirror and re-insert
        # (upsert via delete + insert is simpler than MERGE for DuckLake)
        placeholders = ", ".join(["?"] * len(changed_ids))
        self.conn.execute(
            f"DELETE FROM {alias}.{table} WHERE id IN ({placeholders})",
            changed_ids,
        )

        id_placeholders = ", ".join([f"'{cid}'" for cid in changed_ids])
        self.conn.execute(f"INSERT INTO {alias}.{table} ({common_str}) SELECT {common_str} FROM {table} WHERE id IN ({id_placeholders})")

        return len(changed_ids)

    def _sync_change_feed(self, alias: str, last_push: str | None) -> int:
        """Sync local change log entries to DuckLake change feed.

        Maps local ohm_change_log columns to mirror ohm_change_feed columns:
        - row_id -> change_row_id
        - changed_at -> occurred_at
        - change_data -> new_data (old_data set to NULL)

        Args:
            alias: DuckLake alias.
            last_push: ISO timestamp of last push, or None.

        Returns:
            Number of change feed entries synced.
        """
        if last_push:
            changes = self.conn.execute(
                "SELECT table_name, row_id, operation, agent_name, change_data, changed_at FROM ohm_change_log WHERE changed_at > ?::TIMESTAMP",
                [last_push],
            ).fetchall()
        else:
            changes = self.conn.execute("SELECT table_name, row_id, operation, agent_name, change_data, changed_at FROM ohm_change_log").fetchall()

        if not changes:
            return 0

        for change in changes:
            try:
                self.conn.execute(
                    f"INSERT INTO {alias}.ohm_change_feed (table_name, change_row_id, operation, agent_name, old_data, new_data, occurred_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    [change[0], change[1], change[2], change[3], change[4], change[5]],
                )
            except Exception:
                # Duplicate or schema mismatch — skip
                pass

        return len(changes)

    def _push_to_ducklake_legacy(self, ducklake_path: str) -> int:
        """Legacy push: open separate DuckDB connection to DuckLake.

        Used as fallback when DuckLake is not attached as an alias
        on the current connection.

        Args:
            ducklake_path: Path to DuckLake database file.

        Returns:
            Number of changes pushed.
        """
        # Can't push to an empty path (would open an in-memory DB that vanishes)
        if not ducklake_path:
            return 0

        from datetime import datetime

        # Get last push timestamp for this agent
        last_push = self._get_last_push_timestamp()
        last_push_str = last_push if last_push else "1970-01-01T00:00:00Z"

        # Read local changes since last push
        changes = self.execute(
            """
            SELECT table_name, row_id, operation, layer, change_data, changed_at
            FROM ohm_change_log
            WHERE agent_name = ? AND changed_at > ?::TIMESTAMP
            ORDER BY changed_at ASC
            """,
            [self.agent_name, last_push_str],
        )

        if not changes:
            return 0

        # Connect to DuckLake and insert changes
        ducklake = duckdb.connect(ducklake_path, read_only=False)

        try:
            inserted = 0
            for change in changes:
                table_name = change["table_name"]
                row_id = change["row_id"]
                operation = change["operation"]
                change_data = change["change_data"]
                changed_at = change["changed_at"]

                # Insert into DuckLake's change feed.
                # ohm_change_feed may be absent if the DuckLake file was created
                # before this table was added — skip rather than abort the whole push.
                try:
                    ducklake.execute(
                        """
                        INSERT INTO ohm_change_feed
                        (table_name, row_id, operation, agent_name, old_data, new_data, occurred_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [table_name, row_id, operation, self.agent_name, None, change_data, changed_at],
                    )
                    inserted += 1
                except Exception as _exc:
                    logger.debug("Legacy DuckLake push skipped for row %s: %s", row_id, _exc)

            # Record push timestamp
            now = datetime.now(timezone.utc)
            self._set_last_push_timestamp(now)

            return inserted
        finally:
            ducklake.close()

    def pull_from_ducklake(self, ducklake_path: str, alias: str = "ohm_lake") -> int:
        """Pull remote changes from DuckLake shared backend.

        When DuckLake is attached as an alias, reads from the mirror
        tables directly. Otherwise, falls back to the legacy separate
        connection approach.

        Args:
            ducklake_path: Path to DuckLake database (used for fallback).
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Number of changes pulled and applied.
        """
        # Check if DuckLake alias is attached on this connection
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
        except Exception:
            attached = None

        if attached:
            # New approach: pull from attached mirror tables
            return self._pull_from_ducklake_attached(alias)

        # Fallback: legacy separate connection
        return self._pull_from_ducklake_legacy(ducklake_path)

    def _pull_from_ducklake_attached(self, alias: str = "ohm_lake") -> int:
        """Pull remote changes from attached DuckLake mirror tables.

        Reads rows from DuckLake mirror tables that don't exist in
        local tables (or have been updated by other agents) and
        applies them to the local database.

        Args:
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Number of rows pulled and applied.
        """
        pulled = 0

        for table in ["ohm_nodes", "ohm_edges", "ohm_observations"]:
            try:
                # Find rows in DuckLake that are not in local table
                # (new rows from other agents)
                new_rows = self.conn.execute(f"SELECT dl.id FROM {alias}.{table} dl LEFT JOIN {table} l ON dl.id = l.id WHERE l.id IS NULL").fetchall()

                logger.info("DuckLake pull: %s has %d new rows in mirror", table, len(new_rows))

                if new_rows:
                    # Get common columns
                    local_cols = self.conn.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}' ORDER BY ordinal_position").fetchall()
                    local_col_names = [c[0] for c in local_cols]

                    mirror_cols = self.conn.execute(f"SELECT column_name FROM information_schema.columns WHERE table_schema = '{alias}' AND table_name = '{table}' ORDER BY ordinal_position").fetchall()
                    mirror_col_names = [c[0] for c in mirror_cols]

                    common_cols = [c for c in local_col_names if c in mirror_col_names]
                    if not common_cols:
                        continue

                    # Get local column types for casting DuckLake VARCHAR values
                    local_col_types = {}
                    type_rows = self.conn.execute(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{table}' ORDER BY ordinal_position").fetchall()
                    for col_name, col_type in type_rows:
                        local_col_types[col_name] = col_type

                    new_ids = [r[0] for r in new_rows]
                    id_list = ", ".join([f"'{i}'" for i in new_ids])

                    # Build SELECT with explicit casts from DuckLake VARCHAR to local types
                    select_cols = []
                    for col in common_cols:
                        ltype = local_col_types.get(col, "VARCHAR")
                        if ltype.upper() in ("FLOAT", "DOUBLE", "REAL"):
                            select_cols.append(f"CAST(dl.{col} AS {ltype}) AS {col}")
                        elif ltype.upper() in ("TIMESTAMP", "TIMESTAMPTZ"):
                            select_cols.append(f"CAST(dl.{col} AS {ltype}) AS {col}")
                        elif ltype.upper() == "INTEGER" or ltype.upper().startswith("BIGINT"):
                            select_cols.append(f"CAST(dl.{col} AS {ltype}) AS {col}")
                        else:
                            select_cols.append(f"dl.{col} AS {col}")

                    common_str = ", ".join(common_cols)
                    select_str = ", ".join(select_cols)

                    # Insert new rows from DuckLake into local table with type casting
                    self.conn.execute(f"INSERT INTO {table} ({common_str}) SELECT {select_str} FROM {alias}.{table} dl WHERE dl.id IN ({id_list})")
                    pulled += len(new_ids)

            except Exception as exc:
                # Log the actual error instead of silently swallowing it
                import logging

                logging.getLogger("ohm").warning("DuckLake pull failed for %s: %s", table, exc)

        # Record pull timestamp
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        self._set_last_pull_timestamp(now)

        return pulled

    def _pull_from_ducklake_legacy(self, ducklake_path: str) -> int:
        """Legacy pull: open separate DuckDB connection to DuckLake.

        Used as fallback when DuckLake is not attached as an alias
        on the current connection.

        Args:
            ducklake_path: Path to DuckLake database file.

        Returns:
            Number of changes pulled and applied.
        """
        # Can't pull from an empty path (would open an in-memory DB with no tables)
        if not ducklake_path:
            return 0

        # Get last pull timestamp for this agent
        last_pull = self._get_last_pull_timestamp()
        last_pull_str = last_pull if last_pull else "1970-01-01T00:00:00Z"

        # Connect to DuckLake and read changes
        ducklake = duckdb.connect(ducklake_path, read_only=True)

        try:
            # Read remote changes since last pull (excluding our own)
            # ohm_change_feed may be absent in DuckLake catalog files (.ducklake format)
            # opened as plain DuckDB — return 0 gracefully if the table doesn't exist.
            changes = ducklake.execute(
                """
                SELECT table_name, row_id, operation, agent_name, new_data, occurred_at
                FROM ohm_change_feed
                WHERE agent_name != ? AND occurred_at > ?::TIMESTAMP
                ORDER BY occurred_at ASC
                """,
                [self.agent_name, last_pull_str],
            ).fetchall()
        except Exception as _exc:
            logger.debug("Legacy DuckLake pull skipped: %s", _exc)
            ducklake.close()
            return 0

        try:
            if not changes:
                return 0

            applied = 0
            for change in changes:
                table_name, row_id, operation, remote_agent, new_data, occurred_at = change
                self._apply_remote_change(table_name, row_id, operation, remote_agent, new_data, occurred_at)
                applied += 1

            # Record pull timestamp
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            self._set_last_pull_timestamp(now)

            return applied
        finally:
            ducklake.close()

    def _apply_remote_change(
        self,
        table_name: str,
        row_id: str,
        operation: str,
        remote_agent: str,
        new_data: Any,
        occurred_at: str | None = None,
    ) -> None:
        """Apply a remote change from DuckLake to local database.

        Only applies INSERT and UPDATE. Skips DELETE (not implemented yet).
        Uses attribution-based conflict resolution: remote agent's data
        wins if the record was created by a different agent.
        """
        import json

        if new_data:
            data = json.loads(new_data) if isinstance(new_data, str) else new_data
        else:
            data = {}

        if table_name == "ohm_nodes":
            if operation == "INSERT":
                # Check if already exists (including soft-deleted)
                existing = self.get_node(row_id)
                soft_deleted = self.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [row_id]).fetchone() if not existing else None
                if not existing and not soft_deleted:
                    self.conn.execute(
                        """
                        INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence,
                                               visibility, provenance, tags, metadata, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            row_id,
                            data.get("label", ""),
                            data.get("type", "concept"),
                            data.get("content"),
                            remote_agent,
                            data.get("confidence", 1.0),
                            data.get("visibility", "team"),
                            data.get("provenance"),
                            json.dumps(data.get("tags", [])) if data.get("tags") else None,
                            json.dumps(data.get("metadata", {})) if data.get("metadata") else None,
                            occurred_at or self._now(),
                            occurred_at or self._now(),
                        ],
                    )
            elif operation == "UPDATE":
                self.conn.execute(
                    """
                    UPDATE ohm_nodes SET
                        label = COALESCE(?, label),
                        type = COALESCE(?, type),
                        content = COALESCE(?, content),
                        confidence = COALESCE(?, confidence),
                        visibility = COALESCE(?, visibility),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    [data.get("label"), data.get("type"), data.get("content"), data.get("confidence"), data.get("visibility"), row_id],
                )

        elif table_name == "ohm_edges":
            if operation == "INSERT":
                # Check if already exists (including soft-deleted)
                existing = self.get_edge(row_id)
                soft_deleted = self.conn.execute("SELECT id FROM ohm_edges WHERE id = ? AND deleted_at IS NOT NULL", [row_id]).fetchone() if not existing else None
                if not existing and not soft_deleted:
                    self.conn.execute(
                        """
                        INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence,
                                               condition, provenance, created_by, challenge_of,
                                               challenge_type, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            row_id,
                            data.get("from_node"),
                            data.get("to_node"),
                            data.get("layer", "L3"),
                            data.get("edge_type"),
                            data.get("confidence"),
                            data.get("condition"),
                            data.get("provenance"),
                            remote_agent,
                            data.get("challenge_of"),
                            data.get("challenge_type"),
                            occurred_at or self._now(),
                            occurred_at or self._now(),
                        ],
                    )
                    self._increment_graph_generation()

    def _get_last_push_timestamp(self) -> Optional[str]:
        """Get the last push timestamp for this agent."""
        row = self.conn.execute(
            """
            SELECT value FROM ohm_meta
            WHERE key = ? || '_last_push'
            """,
            [self.agent_name],
        ).fetchone()
        return row[0] if row else None

    def _set_last_push_timestamp(self, timestamp: datetime) -> None:
        """Set the last push timestamp for this agent."""
        ts_str = timestamp.isoformat()
        self.conn.execute(
            """
            INSERT INTO ohm_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            [f"{self.agent_name}_last_push", ts_str],
        )

    def _get_last_pull_timestamp(self) -> Optional[str]:
        """Get the last pull timestamp for this agent."""
        row = self.conn.execute(
            """
            SELECT value FROM ohm_meta
            WHERE key = ? || '_last_pull'
            """,
            [self.agent_name],
        ).fetchone()
        return row[0] if row else None

    def _set_last_pull_timestamp(self, timestamp: datetime) -> None:
        """Set the last pull timestamp for this agent."""
        ts_str = timestamp.isoformat()
        self.conn.execute(
            """
            INSERT INTO ohm_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            [f"{self.agent_name}_last_pull", ts_str],
        )

    # ── DuckLake Time Travel ──────────────────────────────────────────

    def list_snapshots(self, alias: str = "ohm_lake") -> list[dict[str, Any]]:
        """List available DuckLake snapshots.

        Queries DuckLake snapshots metadata to return all historical
        snapshots with their IDs, timestamps, and commit messages.

        Args:
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            List of dicts with snapshot_id, snapshot_time, and
            commit_message. Empty list if DuckLake is not attached.
        """
        # Check if DuckLake alias is attached
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
        except Exception:
            attached = None

        if not attached:
            return []

        try:
            rows = self.conn.execute(
                "SELECT * FROM ducklake_snapshots(?) ORDER BY snapshot_id ASC",
                [alias],
            ).fetchall()
            columns = [desc[0] for desc in self.conn.description]
            return [dict(zip(columns, row)) for row in rows]
        except Exception:
            # Fallback for older DuckLake builds that expose snapshots() on
            # the attached alias rather than ducklake_snapshots(alias).
            try:
                rows = self.conn.execute(f"SELECT * FROM {alias}.snapshots() ORDER BY snapshot_id ASC").fetchall()
                columns = [desc[0] for desc in self.conn.description]
                return [dict(zip(columns, row)) for row in rows]
            except Exception as e:
                raise OHMError(f"Failed to list DuckLake snapshots: {e}") from e

    def graph_at_version(
        self,
        version: int,
        alias: str = "ohm_lake",
    ) -> dict[str, Any]:
        """Query graph state at a specific DuckLake snapshot version.

        Uses DuckLake's AT (VERSION => N) syntax to read nodes and
        edges as they existed at snapshot N.

        Args:
            version: DuckLake snapshot version number.
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Dict with 'nodes' and 'edges' lists representing the
            graph state at that snapshot version.

        Raises:
            OHMError: If DuckLake is not attached or query fails.
        """
        # Check if DuckLake alias is attached
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
        except Exception:
            attached = None

        if not attached:
            raise OHMError("DuckLake is not attached — cannot query historical state")

        try:
            nodes = self.conn.execute(f"SELECT * FROM {alias}.ohm_nodes AT (VERSION => {int(version)})").fetchall()
            node_columns = [desc[0] for desc in self.conn.description]
            nodes_dicts = [dict(zip(node_columns, row)) for row in nodes]
        except Exception as e:
            raise OHMError(f"Failed to query nodes at version {version}: {e}") from e

        try:
            edges = self.conn.execute(f"SELECT * FROM {alias}.ohm_edges AT (VERSION => {int(version)})").fetchall()
            edge_columns = [desc[0] for desc in self.conn.description]
            edges_dicts = [dict(zip(edge_columns, row)) for row in edges]
        except Exception as e:
            raise OHMError(f"Failed to query edges at version {version}: {e}") from e

        return {
            "version": version,
            "nodes": nodes_dicts,
            "edges": edges_dicts,
            "node_count": len(nodes_dicts),
            "edge_count": len(edges_dicts),
        }

    def graph_changes(
        self,
        from_version: int,
        to_version: int,
        alias: str = "ohm_lake",
    ) -> dict[str, Any]:
        """Query changes between two DuckLake snapshot versions.

        Uses DuckLake's table_changes() function to return insertions
        and deletions between two snapshots for both nodes and edges.

        Args:
            from_version: Starting snapshot version (exclusive).
            to_version: Ending snapshot version (inclusive).
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Dict with 'node_changes' and 'edge_changes' lists, each
            containing rows with snapshot_id, rowid, change_type, and
            the data columns.

        Raises:
            OHMError: If DuckLake is not attached or query fails.
        """
        # Check if DuckLake alias is attached
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
        except Exception:
            attached = None

        if not attached:
            raise OHMError("DuckLake is not attached — cannot query changes")

        try:
            node_changes = self.conn.execute(f"SELECT * FROM {alias}.table_changes('ohm_nodes', {int(from_version)}, {int(to_version)})").fetchall()
            nc_columns = [desc[0] for desc in self.conn.description]
            node_changes_dicts = [dict(zip(nc_columns, row)) for row in node_changes]
        except Exception as e:
            raise OHMError(f"Failed to query node changes between versions {from_version} and {to_version}: {e}") from e

        try:
            edge_changes = self.conn.execute(f"SELECT * FROM {alias}.table_changes('ohm_edges', {int(from_version)}, {int(to_version)})").fetchall()
            ec_columns = [desc[0] for desc in self.conn.description]
            edge_changes_dicts = [dict(zip(ec_columns, row)) for row in edge_changes]
        except Exception as e:
            raise OHMError(f"Failed to query edge changes between versions {from_version} and {to_version}: {e}") from e

        return {
            "from_version": from_version,
            "to_version": to_version,
            "node_changes": node_changes_dicts,
            "edge_changes": edge_changes_dicts,
            "node_change_count": len(node_changes_dicts),
            "edge_change_count": len(edge_changes_dicts),
        }
