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

from __future__ import annotations

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
from ohm.graph.embeddings import EmbeddingBackend

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
                store.conn.execute("INSTALL ducklake FROM core")
                store.conn.execute("LOAD ducklake")
                store.conn.execute(f"ATTACH IF NOT EXISTS '{ducklake_path}' AS ohm_lake (TYPE ducklake)")
                logger.info("DuckLake attached for agent %s at %s", agent_name, ducklake_path)
            except Exception as e:
                logger.warning("DuckLake attach failed for agent %s: %s", agent_name, e)

        # OHM-fix-3: Create read-only connection AFTER DuckLake ATTACH
        # so both connections have matching configuration.
        store._ensure_read_conn()

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
        embedding_backend: Optional["EmbeddingBackend"] = None,
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
            embedding_backend: EmbeddingBackend instance for vector generation.
                If None, auto-detects: tries Ollama, falls back to NullBackend.
        """
        self._lock = threading.RLock()
        self._read_lock = threading.RLock()  # Separate lock for read operations
        self.agent_name = agent_name
        self.readonly = readonly
        self.quack = quack
        self.quack_uri = quack_uri
        self.quack_token_env = quack_token_env
        self.quack_started = False
        self.sync_degraded = False
        self.schema = schema or DEFAULT_SCHEMA

        # OHM-9zk7: Embedding backend (pluggable)
        if embedding_backend is None:
            from ohm.graph.embeddings import NullBackend, OllamaBackend

            ollama = OllamaBackend()
            if ollama.is_available():
                self._embedding_backend = ollama
            else:
                self._embedding_backend = NullBackend(dimensions=768)
        else:
            self._embedding_backend = embedding_backend

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

        # Read-only connection for concurrent reads (OHM concurrency fix)
        # OHM-fix-3: DuckDB requires matching configuration for concurrent
        # connections. The read_conn is initially deferred — it will be created
        # after DuckLake ATTACH in the factory function, because the DuckLake
        # ATTACH changes the write connection's internal config, and the read
        # connection must match. If created here, it would have different config.
        self._read_conn = None
        self._read_conn_ready = False  # Flag: set True after DuckLake ATTACH
        self._read_conn_deferred = True  # Will be created in _ensure_read_conn()

        try:
            self._init_schema()

            # OHM-8n9/OHM-6cz: Auto-restore from DuckLake if tables are empty.
            self._auto_restore_if_empty()

            # Try to load DuckDB markdown extension (optional)
            # Enables rich content features: read_markdown, md_to_text, etc.
            # INSTALL can hang on Windows; skip there. On Linux use SIGALRM
            # as a timeout guard.
            self.markdown_available = False
            if os.name == "posix":
                try:
                    import signal

                    def _markdown_timeout(signum, frame):
                        raise TimeoutError("DuckDB markdown extension install timed out")

                    old_handler = signal.signal(signal.SIGALRM, _markdown_timeout)
                    signal.alarm(5)
                    try:
                        self.conn.execute("INSTALL markdown FROM community")
                        self.conn.execute("LOAD markdown")
                        self.markdown_available = True
                        logger.info("DuckDB markdown extension loaded — rich content features available")
                    finally:
                        signal.alarm(0)
                        signal.signal(signal.SIGALRM, old_handler)
                except Exception:
                    logger.info("DuckDB markdown extension not available — rich content features disabled, OHM works fine without it")
            else:
                logger.info("DuckDB markdown extension skipped on Windows — rich content features disabled, OHM works fine without it")

            # Start Quack server if requested and available
            if self.quack and not self.readonly:
                self._start_quack()
        except Exception:
            self.conn.close()
            raise

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
                dl_catalog = "ohm_lake"
                dl_schema_prefix = f"__ducklake_metadata_{dl_catalog}"
                try:
                    conn.execute("INSTALL ducklake FROM core")
                    conn.execute("LOAD ducklake")
                    conn.execute(f"ATTACH IF NOT EXISTS '{ducklake_path}' AS {dl_catalog} (TYPE ducklake)")
                    logger.info("DuckLake attached for recovery")

                    # OHM-8bli: iterate the DuckLake registry (not a hardcoded list)
                    # so domain tables (e.g. topo_prospects) get recovered too.
                    for dlt in self.schema.ducklake_tables:
                        table = dlt.name
                        try:
                            dl_cols = conn.execute(f"PRAGMA table_info('{dl_schema_prefix}.{table}')").fetchall()
                            dl_col_names = {r[1] for r in dl_cols}
                            if not dl_col_names:
                                logger.warning("No columns found in DuckLake for %s, skipping", table)
                                continue

                            local_cols = conn.execute(
                                "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
                                [table],
                            ).fetchall()
                            local_col_map = {r[0]: r[1] for r in local_cols}

                            common = [c for c in dl_col_names if c in local_col_map and c != "deleted_at"]

                            select_parts = []
                            for col in common:
                                ltype = local_col_map.get(col, "VARCHAR").upper()
                                if ltype in ("FLOAT", "DOUBLE", "REAL"):
                                    select_parts.append(f"CAST({col} AS FLOAT) AS {col}")
                                elif ltype == "JSON":
                                    select_parts.append(f"CAST({col} AS JSON) AS {col}")
                                else:
                                    select_parts.append(f'"{col}" AS {col}')

                            select_str = ", ".join(select_parts)
                            insert_cols = ", ".join(common)

                            # OHM-8bli: only add deleted_at for tables that have it
                            if dlt.has_deleted_at:
                                conn.execute(f"INSERT INTO {table} ({insert_cols}, deleted_at) SELECT {select_str}, NULL::TIMESTAMP FROM {dl_schema_prefix}.{table}")
                                count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
                            else:
                                conn.execute(f"INSERT INTO {table} ({insert_cols}) SELECT {select_str} FROM {dl_schema_prefix}.{table}")
                                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # type: ignore[index]
                            logger.info("Recovered %d %s from DuckLake (%d columns)", count, table, len(common))
                        except Exception as e:
                            logger.warning("Failed to recover %s from DuckLake: %s", table, e)

                    # Detach DuckLake
                    try:
                        conn.execute(f"DETACH {dl_catalog}")
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

        dl_catalog = "ohm_lake"
        try:
            self.conn.execute("INSTALL ducklake FROM core")
            self.conn.execute("LOAD ducklake")
            self.conn.execute(f"ATTACH IF NOT EXISTS '{ducklake_path}' AS {dl_catalog} (TYPE ducklake)")

            # Try DuckLake 0.3+ metadata schema first, then fall back to catalog-qualified names
            dl_schema_prefix = f"__ducklake_metadata_{dl_catalog}"

            for dlt in self._ducklake_sync_tables():
                table = dlt.name
                try:
                    # Try metadata schema first (DuckLake 0.3+)
                    try:
                        dl_cols = self.conn.execute(f"PRAGMA table_info('{dl_schema_prefix}.{table}')").fetchall()
                    except Exception:
                        # Fall back to catalog-qualified table name
                        dl_cols = self.conn.execute(f"PRAGMA table_info('{dl_catalog}.{table}')").fetchall()

                    # If still no columns, try direct table name
                    if not dl_cols:
                        try:
                            dl_cols = self.conn.execute(f"PRAGMA table_info('{table}')").fetchall()
                        except Exception:
                            pass
                    dl_col_names = {r[1] for r in dl_cols}
                    # Determine which source table name worked
                    source_table = f"{dl_schema_prefix}.{table}"
                    if not dl_cols or len(dl_cols) == 0:
                        pass  # Will skip below
                    else:
                        # Check which prefix the columns came from
                        try:
                            self.conn.execute(f"SELECT 1 FROM {dl_schema_prefix}.{table} LIMIT 0")
                            source_table = f"{dl_schema_prefix}.{table}"
                        except Exception:
                            try:
                                self.conn.execute(f"SELECT 1 FROM {dl_catalog}.{table} LIMIT 0")
                                source_table = f"{dl_catalog}.{table}"
                            except Exception:
                                source_table = table

                    if not dl_col_names:
                        logger.warning("No columns found in DuckLake for %s, skipping", table)
                        continue

                    local_cols = self.conn.execute(
                        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
                        [table],
                    ).fetchall()
                    local_col_map = {r[0]: r[1] for r in local_cols}

                    common = [c for c in dl_col_names if c in local_col_map and c != "deleted_at"]

                    select_parts = []
                    for col in common:
                        ltype = local_col_map.get(col, "VARCHAR").upper()
                        if ltype in ("FLOAT", "DOUBLE", "REAL"):
                            select_parts.append(f"CAST({col} AS FLOAT) AS {col}")
                        elif ltype == "JSON":
                            select_parts.append(f"CAST({col} AS JSON) AS {col}")
                        else:
                            select_parts.append(f'"{col}" AS {col}')

                    select_str = ", ".join(select_parts)
                    insert_cols = ", ".join(common)

                    # OHM-8bli: only add deleted_at for tables that have it
                    if dlt.has_deleted_at:
                        self.conn.execute(f"INSERT INTO {table} ({insert_cols}, deleted_at) SELECT {select_str}, NULL::TIMESTAMP FROM {source_table}")
                        count = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
                    else:
                        self.conn.execute(f"INSERT INTO {table} ({insert_cols}) SELECT {select_str} FROM {source_table}")
                        count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # type: ignore[index]
                    logger.info("Auto-restored %d %s from DuckLake (%d columns)", count, table, len(common))
                except Exception as e:
                    logger.warning("Failed to auto-restore %s from DuckLake: %s", table, e)

            try:
                self.conn.execute(f"DETACH {dl_catalog}")
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

            initialize_schema(self.conn, self.schema)

    @staticmethod
    def _connect_with_wal_recovery(db_path_str: str, readonly: bool = False):
        """Connect to DuckDB with WAL corruption recovery (OHM-b5a).

        If DuckDB fails to open due to WAL replay errors, deletes the
        WAL file and retries. The WAL contains only uncommitted writes,
        so this is safe -- the main DB file is intact.

        DuckDB raises InternalException (not IOException) for WAL
        replay failures (e.g., "Calling DatabaseManager::GetDefaultDatabase
        with no default database set"). Both exception types must be
        caught for reliable recovery.

        OHM-lqpk.3: applies the same performance PRAGMAs (threads,
        enable_object_cache, temp_directory) as ``ohm.graph.db.connect``
        so the OhmStore path matches the canonical helper. Without this,
        the daemon's main write connection would run on DuckDB's default
        single-thread setting while one-off CLI calls benefited from the
        tuned thread pool.
        """
        from .db import _apply_pragmas

        try:
            conn = duckdb.connect(db_path_str, read_only=readonly)
        except (duckdb.IOException, duckdb.InternalException) as e:
            error_msg = str(e)
            if "WAL" in error_msg or "wal" in error_msg.lower() or "replay" in error_msg.lower():
                wal_path = db_path_str + ".wal"
                if os.path.exists(wal_path):
                    os.remove(wal_path)
                conn = duckdb.connect(db_path_str, read_only=readonly)
            else:
                raise

        if not readonly:
            # Read-only connections don't benefit from threads>1 (a single
            # query plan is serial), and PRAGMA threads on read-only can
            # raise on some DuckDB versions. Only tune write connections.
            _apply_pragmas(conn)
        return conn

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
        """Execute a SQL query and return results as list of dicts.

        Uses the write lock to serialize access to the shared DuckDB connection.
        For read-only queries, prefer read_execute() which uses the separate
        read connection when available.
        """
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

    @property
    def read_conn(self):
        """Return the read-only DuckDB connection for concurrent reads.

        Falls back to the write connection if no read connection is available.
        Read queries should use this to avoid blocking behind the write lock.
        ADR-023: If the read connection is stale/crashed, recreate it.
        """
        if self._read_conn:
            try:
                # Health check — will raise if the connection is dead
                self._read_conn.execute("SELECT 1")
                return self._read_conn
            except Exception:
                # Read connection is dead, fall back to write connection
                logger.warning("Read-only connection is stale, falling back to write connection")
                try:
                    self._read_conn = duckdb.connect(str(self.db_path), read_only=True)
                    return self._read_conn
                except Exception:
                    self._read_conn = None
                    return self.conn
        return self.conn

    def read_execute(self, sql: str, params: Optional[list] = None) -> list[dict[str, Any]]:
        """Execute a read-only query using the read connection.

        Uses the separate read-only DuckDB connection so reads don't
        block behind the write lock. Falls back to the write connection
        if the read connection is unavailable.
        ADR-023: Catches DuckDB connection errors and falls back gracefully.
        """
        conn = self._read_conn or self.conn
        if self._read_conn:
            # Read-only connection — no lock needed for reads
            try:
                if params:
                    result = conn.execute(sql, params)
                else:
                    result = conn.execute(sql)
                columns = [desc[0] for desc in result.description]
                rows = result.fetchall()
            except (duckdb.FatalException, duckdb.IOException, duckdb.InternalException) as e:
                # Read connection crashed — fall back to write connection
                logger.warning(f"Read connection error: {e}, falling back to write connection")
                try:
                    self._read_conn = duckdb.connect(str(self.db_path), read_only=True)
                except Exception:
                    self._read_conn = None
                return self.execute(sql, params)
        else:
            # Fallback: use write connection with lock
            return self.execute(sql, params)

        results = [dict(zip(columns, row)) for row in rows]
        _json_cols = {"tags", "metadata", "action_alternatives"}
        for row in results:
            for col in _json_cols & row.keys():
                if isinstance(row[col], str):
                    try:
                        row[col] = json.loads(row[col])
                    except (json.JSONDecodeError, ValueError):
                        pass
            if "from_node" in row:
                row["from"] = row["from_node"]
                row["to"] = row["to_node"]
                row["type"] = row["edge_type"]
        return results

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
        source_url: Optional[str] = None,
        task_status: Optional[str] = None,
        assigned_to: Optional[str] = None,
        due_date: Optional[str] = None,
        utility_scale: Optional[str | float] = None,
        current_best_action: Optional[str] = None,
        action_alternatives: Optional[list[str]] = None,
        utility_usd_per_day: Optional[float] = None,
        utility_currency: Optional[str] = None,
        source_tier: Optional[str] = None,
        source_author: str | None = None,
        source_institution: str | None = None,
        data_origin: str | None = None,
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
            utility_scale: For decision nodes: importance weight 0-1, or
                one of {'best' (1.0), 'neutral' (0.5), 'worst' (0.0)}.
            current_best_action: For decision nodes: currently preferred action.
            action_alternatives: For decision nodes: list of alternative actions.
            utility_usd_per_day: For decision nodes: monetary value of resolving uncertainty (USD/day).
            utility_currency: ISO 4217 currency code for utility_usd_per_day (e.g. "USD").
            source_tier: Optional quality tier for the source (ADR-028). When set,
                confidence must not exceed SOURCE_TIER_CEILINGS[tier]. None means
                tier not assessed — no ceiling applied (backward compatible).

        Returns a dict with the node record and a 'created' key
        indicating whether this was a new creation (True) or an
        update of an existing node (False).
        """
        from ohm.validation import (
            validate_confidence,
            validate_data_origin,
            validate_source_tier,
            enforce_confidence_ceiling,
            validate_task_status,
            validate_assigned_to,
        )

        confidence = validate_confidence(confidence)
        source_tier = validate_source_tier(source_tier)
        data_origin = validate_data_origin(data_origin)
        enforce_confidence_ceiling(confidence, source_tier)

        # OHM-sbtz.2: validate task-specific fields for task nodes
        if type == "task":
            if task_status is not None:
                task_status = validate_task_status(task_status)
            if assigned_to is not None:
                assigned_to = validate_assigned_to(assigned_to)

        actor = agent_name or self.agent_name
        metadata_json = json.dumps(metadata) if metadata else None
        tag_list = tags if tags else []
        tags_json = json.dumps(tag_list) if tag_list else None
        alternatives_json = json.dumps(action_alternatives) if action_alternatives else None
        now = self._now()

        # Normalize categorical utility_scale to numeric encoding
        _utility_scale_map = {"best": 1.0, "neutral": 0.5, "worst": 0.0}
        if utility_scale is not None and isinstance(utility_scale, str):
            utility_scale = _utility_scale_map.get(utility_scale, utility_scale)

        # ADR-015: source_url is an alias for url (backward compat)
        if source_url is not None and url is None:
            url = source_url

        # Check if node exists (active)
        existing = self.get_node(id)
        if existing:
            from ohm.server.boundary import check_can_update_l2_node

            check_can_update_l2_node(actor, id, self.conn)
            self.conn.execute(
                """
                UPDATE ohm_nodes SET
                    label = ?, type = ?, content = ?, confidence = ?,
                    visibility = ?, provenance = ?, tags = ?, metadata = ?,
                    priority = ?, url = ?, task_status = ?, assigned_to = ?,
                    due_date = ?, utility_scale = ?, current_best_action = ?,
                    action_alternatives = ?, utility_usd_per_day = ?,
                    utility_currency = ?, source_tier = ?,
                    source_author = ?, source_institution = ?, data_origin = ?,
                    updated_at = ?, updated_by = ?
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
                    source_tier,
                    source_author,
                    source_institution,
                    data_origin,
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
                    source_tier = ?,
                    source_author = ?, source_institution = ?, data_origin = ?,
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
                    source_tier,
                    source_author,
                    source_institution,
                    data_origin,
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
                                   source_tier,
                                   source_author, source_institution, data_origin,
                                   created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                source_tier,
                source_author,
                source_institution,
                data_origin,
                now,
                now,
            ],
        )
        self._log_change("ohm_nodes", id, "INSERT", None, agent_name=actor)
        result = self.get_node(id) or {}
        result["created"] = True

        # Auto-register alias (OHM-g0kv)
        from ohm.validation import normalize_alias

        norm = normalize_alias(label)
        if norm and norm != id:
            try:
                self.conn.execute(
                    "INSERT INTO ohm_aliases (alias_norm, node_id) VALUES (?, ?)",
                    [norm, id],
                )
            except Exception:
                pass

        # Auto-generate embedding in background (best-effort, non-blocking)
        threading.Thread(target=self._auto_embed_node, args=(id, label, content), daemon=True).start()

        return result

    def _auto_embed_node(self, node_id: str, label: str, content: str | None = None) -> None:
        """Best-effort embedding generation for a single node.

        Generates an embedding from label + content (if available).
        Uses the configured embedding backend (OHM-9zk7).
        Silently skips if embedding fails.
        Never raises — embedding is not critical for node creation.

        OHM-fix-1: Uses a dedicated DuckDB connection for the embedding update
        instead of sharing self.conn across threads. DuckDB connections are NOT
        thread-safe — using self.conn from a background thread causes SIGSEGV
        even when serialized via self._lock, because the C-level connection state
        can be corrupted between Python lock release and the next acquire.
        """
        try:
            text = label
            if content:
                text = f"{label}: {content[:800]}"
            # Ollama call is pure I/O — runs outside any lock.
            embeddings = self._embedding_backend.embed([text])
            embedding = embeddings[0] if embeddings else None
            if embedding and any(e != 0.0 for e in embedding):
                # Use a dedicated connection for this thread — DuckDB connections
                # are NOT thread-safe and sharing self.conn across threads causes
                # SIGSEGV (the root cause of 4-12 daemon crashes per day).
                import duckdb as _duckdb

                _embed_conn = _duckdb.connect(str(self.db_path))
                try:
                    _embed_conn.execute(
                        "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
                        [embedding, node_id],
                    )
                    logger.debug("Auto-generated embedding for node %s", node_id)
                finally:
                    _embed_conn.close()
        except Exception as e:
            logger.debug("Auto-embed failed for node %s: %s", node_id, e)

    def _ensure_read_conn(self) -> None:
        """Create the read-only connection after DuckLake ATTACH (OHM-fix-3).

        DuckDB requires concurrent connections to have matching configuration.
        However, when DuckLake is loaded and attached, the internal connection
        configuration changes in ways that prevent a second connection from
        opening the same file (even with read_only=True). This is a known
        DuckDB limitation with extensions that modify the catalog.

        Since the read-only connection consistently fails when DuckLake is
        active, this method gracefully accepts the fallback to the write
        connection. The write connection works fine for reads too — the only
        downside is no read/write concurrency (reads wait for the write lock).
        """
        if self._read_conn_ready or self._read_conn is not None:
            return
        try:
            self._read_conn = duckdb.connect(str(self.db_path), read_only=True)
            self._read_conn_ready = True
            logger.info("OHM read-only connection established for %s", self.db_path)
        except Exception as e:
            # Expected when DuckLake is attached: config mismatch prevents
            # a second connection. Accept the fallback gracefully.
            logger.debug("Read-only connection unavailable (expected with DuckLake): %s", e)
            self._read_conn = None
            self._read_conn_ready = True  # Don't retry on every read

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
        source_tier: Optional[str] = None,
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
            source_tier: Optional quality tier for the source (ADR-028). When set,
                confidence must not exceed SOURCE_TIER_CEILINGS[tier]. None means
                tier not assessed — no ceiling applied (backward compatible).
            deduplicate: If True, check for an existing non-deleted edge with the
                same (from_node, to_node, edge_type, layer) and update it instead
                of creating a duplicate. Default True.

        Enforces boundary rules:
        - L1/L2: any agent can write
        - L3/L4: creates with attribution, cannot overwrite
        - Challenge edges: create separate, don't modify
        """
        from ohm.validation import (
            validate_source_tier,
            enforce_confidence_ceiling,
        )

        source_tier = validate_source_tier(source_tier)
        if confidence is not None:
            enforce_confidence_ceiling(confidence, source_tier)

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

        # ADR-022: Validate edge-level constraints (advisory mode — warnings only)
        from ohm.graph.constraints import validate_edge_constraints

        _valid, _warnings, _errors = validate_edge_constraints(
            edge_type=edge_type,
            layer=layer,
            conn=self.conn,
            from_node=from_node,
            confidence=confidence,
            enforce=False,
        )
        for warn in _warnings:
            logger.warning("Edge constraint warning: %s", warn)
        for err in _errors:
            logger.error("Edge constraint violation: %s", err)

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
                                    source_tier,
                                    created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                from_node,
                to_node,
                layer,
                edge_type,
                confidence,
                condition,
                provenance,
                actor,
                challenge_of,
                challenge_type,
                urgency,
                probability,
                probability_p05,
                probability_p50,
                probability_p95,
                confidence_p05,
                confidence_p50,
                confidence_p95,
                source_tier,
                now,
                now,
            ],
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
        Enforces OHM-e0t1 lint: reason must be non-empty (ADR-018).
        """
        actor = agent_name or self.agent_name
        from ohm.boundary import enforce_challenge_boundary
        from ohm.graph.challenges import require_challenge_reason

        # OHM-e0t1: enforce non-empty reason at write time. Implements
        # the require_reasoning: True constraint that was previously
        # declared but not enforced.
        reason = require_challenge_reason(reason)

        original = self.get_edge(edge_id)
        if not original:
            return None

        # Enforce boundary: only L3/L4 edges can be challenged
        enforce_challenge_boundary(self.conn, actor, edge_id)

        # OHM-mzyc.2: dedup — check if this agent already challenged/supported this edge
        existing = self.conn.execute(
            """SELECT id, created_at FROM ohm_edges
               WHERE challenge_of = ?
                 AND edge_type = ?
                 AND created_by = ?
                 AND deleted_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            [edge_id, challenge_type, actor],
        ).fetchone()
        if existing:
            # Return the existing challenge/support edge instead of creating a duplicate
            return self.get_edge(existing[0])

        return self.write_edge(
            from_node=original["from_node"],
            to_node=original["to_node"],
            edge_type=challenge_type,  # OHM-7el6: use challenge_type as edge_type (was hardcoded to CHALLENGED_BY)
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

        from ohm.server.boundary import check_can_update_edge

        check_can_update_edge(actor, edge["created_by"], edge_id)

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
        scale: Optional[str] = None,
        agent_name: Optional[str] = None,
        half_life_days: Optional[float] = None,
        weibull_shape: Optional[float] = None,
        compression_degree: Optional[float] = None,
        compression_type: Optional[str] = None,
        beneficiary: Optional[list[str]] = None,
        revisability: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Create an observation. Attributed to the given agent.

        Args:
            agent_name: Agent to attribute the observation to. Defaults to self.agent_name.
            notes: Optional free-text notes for the observation.
            source_name: Name of the source (e.g., 'Reuters').
            source_url: URL of the source (e.g., 'https://reuters.com/...').
            scale: Measurement scale — 'probability' (0-1), 'count', 'currency', 'percent', or 'unknown'.
            half_life_days: OHM-xdd4 temporal decay override. None = use type default.
            weibull_shape: OHM-24g9 Weibull shape parameter override. None = use type default.
            compression_degree: ADR-026. 0.0-1.0, from elaboration (0) to fabrication (1).
            compression_type: ADR-026. One of: inversion, normative_inversion, retrojection, composite.
            beneficiary: ADR-026. List of agent/node IDs who benefit if the observed claim is believed.
            revisability: ADR-026. 0.0-1.0, from revisable (0) to sacred (1). How hard to decompress.
        """
        from ohm.graph.schema import VALID_OBSERVATION_SCALES, VALID_COMPRESSION_TYPES
        from ohm.graph.decay import default_half_life, default_weibull_shape

        if scale is not None and scale not in VALID_OBSERVATION_SCALES:
            raise ValueError(f"Invalid scale '{scale}' — must be one of: {', '.join(sorted(VALID_OBSERVATION_SCALES))}")
        if scale == "probability" and value is not None and (value < 0.0 or value > 1.0):
            raise ValueError(f"Observation value {value} is outside [0, 1] for scale='probability'")
        if compression_type is not None and compression_type not in VALID_COMPRESSION_TYPES:
            raise ValueError(f"Invalid compression_type '{compression_type}' — must be one of: {', '.join(sorted(VALID_COMPRESSION_TYPES))}")
        if compression_degree is not None and (compression_degree < 0.0 or compression_degree > 1.0):
            raise ValueError(f"compression_degree {compression_degree} is outside [0, 1]")
        if revisability is not None and (revisability < 0.0 or revisability > 1.0):
            raise ValueError(f"revisability {revisability} is outside [0, 1]")
        # OHM-xdd4: resolve half_life_days — explicit override > type default
        if half_life_days is None:
            half_life_days = default_half_life(type)
        # OHM-24g9: resolve weibull_shape — explicit override > type default
        if weibull_shape is None:
            weibull_shape = default_weibull_shape(type)

        actor = agent_name or self.agent_name
        now = self._now()
        beneficiary_json = json.dumps(beneficiary) if beneficiary else None
        self.conn.execute(
            """
            INSERT INTO ohm_observations
                (node_id, edge_id, type, value, baseline, sigma, source,
                 created_by, created_at, notes, source_name, source_url, scale,
                 half_life_days, weibull_shape, valid_from,
                 compression_degree, compression_type, beneficiary, revisability)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [node_id, edge_id, type, value, baseline, sigma, source, actor, now, notes, source_name, source_url, scale, half_life_days, weibull_shape, now, compression_degree, compression_type, beneficiary_json, revisability],
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

        # OHM-cwrc: upsert instead of check-then-insert. The check-then-insert
        # path raced under concurrent writes (same race that bit _log_change).
        self.conn.execute(
            """
            INSERT INTO ohm_agent_state (agent_name, current_focus, active_patterns,
                                          confidence_threshold, available_services,
                                          current_session_id, last_sync, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (agent_name) DO UPDATE SET
                current_focus = excluded.current_focus,
                active_patterns = excluded.active_patterns,
                available_services = excluded.available_services,
                current_session_id = excluded.current_session_id,
                last_sync = excluded.last_sync,
                updated_at = excluded.updated_at
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

    # ── Data Products (ADR-027 / OHM-ksi0) ───────────────────────────────

    def register_data_product(
        self,
        product_id: str,
        name: str,
        type: str,
        *,
        producer_agent: str,
        customer_id: Optional[str] = None,
        language: str = "en",
        visibility: str = "private",
        status: str = "draft",
        value_proposition: Optional[str] = None,
        description: Optional[str] = None,
        output_port_type: Optional[str] = None,
        access_format: Optional[str] = None,
        access_url: Optional[str] = None,
        authentication_method: Optional[str] = None,
        output_file_formats: Optional[str] = None,
        ohm_node_id: Optional[str] = None,
        confidence: Optional[float] = None,
        product_version: Optional[str] = None,
        odps_yaml: Optional[str] = None,
        consumers: Optional[list[str]] = None,
        auto_link: bool = True,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Insert or update an ODPS data product (ADR-027). Returns the full record.

        Upserts on the (customer_id, product_id, language) unique key. Data
        products are not graph edges, so this does not increment the graph
        generation counter.

        OHM-ovwq: When ``ohm_node_id`` is None and ``auto_link`` is True, auto-creates
        an OHM ``source`` node for the product, a ``PRODUCES`` L2 edge from the producer
        agent node, and ``CONSUMES`` L2 edges from each consumer agent node. Seeds
        ``source_reliability`` from the producer agent's outcome history.

        Args:
            consumers: Optional list of consumer agent labels to link with CONSUMES edges.
            auto_link: When True and ohm_node_id is None, auto-create the provenance
                node and edges. Set False to register a bare catalog entry.
            agent_name: Agent to attribute the write to. Defaults to self.agent_name.
        """
        import uuid as _uuid

        from ohm.graph.queries import _link_provenance, _seed_reliability

        actor = agent_name or self.agent_name
        now = self._now()

        if ohm_node_id is None and auto_link:
            ohm_node_id, source_reliability = _link_provenance(
                self.conn,
                name=name,
                product_id=product_id,
                type=type,
                producer_agent=producer_agent,
                created_by=actor,
                description=description,
                access_url=access_url,
                confidence=confidence,
                consumers=consumers,
            )
        elif ohm_node_id is None:
            source_reliability = None
        else:
            source_reliability = _seed_reliability(self.conn, producer_agent, actor)

        existing = self.execute(
            "SELECT internal_id FROM ohm_data_products WHERE customer_id IS NOT DISTINCT FROM ? AND product_id = ? AND language = ? AND deleted_at IS NULL",
            [customer_id, product_id, language],
        )
        if existing:
            internal_id = existing[0]["internal_id"]
            self.execute(
                """UPDATE ohm_data_products SET
                       name = ?, type = ?, visibility = ?, status = ?,
                       value_proposition = ?, description = ?, producer_agent = ?,
                       output_port_type = ?, access_format = ?, access_url = ?,
                       authentication_method = ?, output_file_formats = ?,
                       ohm_node_id = ?, confidence = ?, source_reliability = ?,
                       product_version = ?,
                       odps_yaml = ?, updated = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE internal_id = ?""",
                [
                    name,
                    type,
                    visibility,
                    status,
                    value_proposition,
                    description,
                    producer_agent,
                    output_port_type,
                    access_format,
                    access_url,
                    authentication_method,
                    output_file_formats,
                    ohm_node_id,
                    confidence,
                    source_reliability,
                    product_version,
                    odps_yaml,
                    now,
                    internal_id,
                ],
            )
            self._log_change("ohm_data_products", internal_id, "UPDATE", None, agent_name=actor)
            return self.execute_one("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id])

        internal_id = str(_uuid.uuid4())
        self.execute(
            """INSERT INTO ohm_data_products
               (internal_id, customer_id, product_id, name, language, visibility, status, type,
                value_proposition, description, producer_agent, output_port_type, access_format,
                access_url, authentication_method, output_file_formats, ohm_node_id, confidence,
                source_reliability, product_version, odps_yaml, created_by, created_at, updated_at, created, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)""",
            [
                internal_id,
                customer_id,
                product_id,
                name,
                language,
                visibility,
                status,
                type,
                value_proposition,
                description,
                producer_agent,
                output_port_type,
                access_format,
                access_url,
                authentication_method,
                output_file_formats,
                ohm_node_id,
                confidence,
                source_reliability,
                product_version,
                odps_yaml,
                actor,
                now,
                now,
            ],
        )
        self._log_change("ohm_data_products", internal_id, "INSERT", None, agent_name=actor)
        return self.execute_one("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id])

    def get_data_product(self, internal_id: str) -> Optional[dict[str, Any]]:
        """Get a data product by internal_id."""
        return self.execute_one(
            "SELECT * FROM ohm_data_products WHERE internal_id = ? AND deleted_at IS NULL",
            [internal_id],
        )

    def get_data_product_by_odps_id(
        self,
        product_id: str,
        *,
        customer_id: Optional[str] = None,
        language: str = "en",
    ) -> Optional[dict[str, Any]]:
        """Get a data product by its ODPS product_id (+ tenant + language)."""
        return self.execute_one(
            "SELECT * FROM ohm_data_products WHERE customer_id IS NOT DISTINCT FROM ? AND product_id = ? AND language = ? AND deleted_at IS NULL",
            [customer_id, product_id, language],
        )

    def list_data_products(
        self,
        *,
        producer_agent: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        customer_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List data products with optional filters."""
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        if producer_agent:
            conditions.append("producer_agent = ?")
            params.append(producer_agent)
        if type:
            conditions.append("type = ?")
            params.append(type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if customer_id is not None:
            conditions.append("customer_id IS NOT DISTINCT FROM ?")
            params.append(customer_id)
        where = " WHERE " + " AND ".join(conditions)
        params.append(limit)
        return self.read_execute(
            f"SELECT * FROM ohm_data_products{where} ORDER BY updated_at DESC LIMIT ?",
            params,
        )

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

        from ohm.server.boundary import check_can_delete_node

        check_can_delete_node(deleted_by, node["created_by"], node_id)

        now = self._now()

        # OHM-sdp1: capture cascaded row_ids BEFORE the UPDATEs so each
        # cascade target gets its own ohm_change_feed row (operators need
        # per-row attribution when a node delete cascades to N edges and
        # M observations — the audit feed used to record only the node row,
        # leaving edges/observations unattributed).
        edge_ids_to_delete = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
                [node_id, node_id],
            ).fetchall()
        ]
        obs_ids_to_delete = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL",
                [node_id],
            ).fetchall()
        ]

        # Soft-delete edges (mark as deleted)
        edges_from = self.conn.execute("UPDATE ohm_edges SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE from_node = ? AND deleted_at IS NULL", [now, now, deleted_by, node_id]).fetchone()
        edges_to = self.conn.execute("UPDATE ohm_edges SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE to_node = ? AND deleted_at IS NULL", [now, now, deleted_by, node_id]).fetchone()
        edges_deleted = (edges_from[0] if edges_from else 0) + (edges_to[0] if edges_to else 0)
        for eid in edge_ids_to_delete:
            self._log_change("ohm_edges", eid, "DELETE", None, agent_name=deleted_by)

        # Soft-delete observations
        obs_result = self.conn.execute("UPDATE ohm_observations SET deleted_at = ? WHERE node_id = ? AND deleted_at IS NULL", [now, node_id])
        obs_deleted = obs_result.fetchone()
        obs_count = obs_deleted[0] if obs_deleted else 0
        for oid in obs_ids_to_delete:
            self._log_change("ohm_observations", oid, "DELETE", None, agent_name=deleted_by)

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

    def merge_nodes(self, keep_id: str, merge_id: str, merged_by: str) -> dict[str, Any]:
        """Merge *merge_id* into *keep_id* and soft-delete *merge_id*.

        Re-points all edges and observations from the merge target to the
        keep target, then soft-deletes the merge node.  Duplicate edges
        (same **from**, **to**, **type**, **layer**) are silently skipped so
        the operation is idempotent.

        Returns a dict with counts of re-pointed edges, observations, and
        skipped duplicates.

        Raises:
            NodeNotFoundError: If either node does not exist.
            ValueError: If *keep_id* equals *merge_id*.
        """
        from ohm.exceptions import NodeNotFoundError

        if keep_id == merge_id:
            raise ValueError(f"keep_id equals merge_id ({keep_id!r}) — nothing to merge")

        keep_node = self.get_node(keep_id)
        if not keep_node:
            raise NodeNotFoundError(f"Keep node not found: {keep_id}")

        merge_node = self.get_node(merge_id)
        if not merge_node:
            raise NodeNotFoundError(f"Merge node not found: {merge_id}")

        now = self._now()

        # 1. Re-point edges FROM merge_id → keep_id (skip duplicates)
        dup_from = self.conn.execute(
            """UPDATE ohm_edges SET from_node = ?, updated_at = ?, updated_by = ?
               WHERE from_node = ? AND deleted_at IS NULL
                 AND (to_node, layer, edge_type) NOT IN (
                   SELECT to_node, layer, edge_type FROM ohm_edges
                   WHERE from_node = ? AND deleted_at IS NULL
                 )""",
            [keep_id, now, merged_by, merge_id, keep_id],
        )
        from_updated = dup_from.fetchone()
        from_count = from_updated[0] if from_updated else 0

        # 2. Re-point edges TO merge_id → keep_id (skip duplicates)
        dup_to = self.conn.execute(
            """UPDATE ohm_edges SET to_node = ?, updated_at = ?, updated_by = ?
               WHERE to_node = ? AND deleted_at IS NULL
                 AND (from_node, layer, edge_type) NOT IN (
                   SELECT from_node, layer, edge_type FROM ohm_edges
                   WHERE to_node = ? AND deleted_at IS NULL
                 )""",
            [keep_id, now, merged_by, merge_id, keep_id],
        )
        to_updated = dup_to.fetchone()
        to_count = to_updated[0] if to_updated else 0

        # 3. Re-point observations (no updated_at column on ohm_observations)
        obs_updated = self.conn.execute(
            "UPDATE ohm_observations SET node_id = ? WHERE node_id = ? AND deleted_at IS NULL",
            [keep_id, merge_id],
        )
        obs_count = obs_updated.fetchone()
        obs_total = obs_count[0] if obs_count else 0

        # 4. Delete edges from merge that would be exact dupes (already
        #    covered by the UPDATE NOT IN above, but also delete any edges
        #    that point FROM → TO identically to an existing edge from keep).
        _skipped = self.conn.execute(
            """UPDATE ohm_edges SET deleted_at = ?, updated_at = ?, updated_by = ?
               WHERE id IN (
                 SELECT e.id FROM ohm_edges e
                 JOIN ohm_edges k ON e.from_node = k.from_node
                   AND e.to_node = k.to_node
                   AND e.layer = k.layer
                   AND e.edge_type = k.edge_type
                 WHERE e.from_node = ? AND e.deleted_at IS NULL
                   AND k.from_node = ? AND k.deleted_at IS NULL
               )""",
            [now, now, merged_by, merge_id, keep_id],
        )

        # 5. Soft-delete the merge node
        self.conn.execute(
            "UPDATE ohm_nodes SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            [now, now, merged_by, merge_id],
        )
        self._log_change("ohm_nodes", merge_id, "MERGE", None, agent_name=merged_by)

        return {
            "keep": keep_id,
            "merged": merge_id,
            "edges_repointed": from_count + to_count,
            "observations_repointed": obs_total,
            "merged_by": merged_by,
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

        from ohm.server.boundary import check_can_delete_edge

        check_can_delete_edge(deleted_by, edge["created_by"], edge_id)

        now = self._now()

        # OHM-sdp1: capture cascaded observation row_ids BEFORE the UPDATE
        # so each gets its own ohm_change_feed entry. Mirrors the
        # delete_node cascade audit fix.
        obs_ids_to_delete = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM ohm_observations WHERE edge_id = ? AND deleted_at IS NULL",
                [edge_id],
            ).fetchall()
        ]

        # Soft-delete observations referencing this edge
        self.conn.execute("UPDATE ohm_observations SET deleted_at = ? WHERE edge_id = ? AND deleted_at IS NULL", [now, edge_id])
        for oid in obs_ids_to_delete:
            self._log_change("ohm_observations", oid, "DELETE", None, agent_name=deleted_by)

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

    def load_webhook_subscriptions(self) -> dict[str, dict[str, dict]]:
        """Load persisted webhook subscriptions (OHM-whbk).

        Returns the same nested dict shape that the server's in-memory
        ``_webhook_registry`` uses: ``{customer_id: {agent: {url, events}}}``.
        Customer id ``""`` (empty string) represents the single-tenant
        default; callers should translate that to ``None`` for the in-memory
        key.
        """
        import json as _json

        result: dict[str, dict[str, dict]] = {}
        try:
            rows = self.conn.execute("SELECT customer_id, agent, url, events FROM ohm_webhook_subscriptions").fetchall()
        except Exception:
            return result
        for row in rows:
            cid = row[0] or ""
            agent = row[1]
            url = row[2]
            try:
                events = _json.loads(row[3]) if row[3] else []
            except (ValueError, TypeError):
                events = []
            result.setdefault(cid, {})[agent] = {"url": url, "events": events}
        return result

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
        # Update agent's last_sync so they appear in active_agents.
        # OHM-cwrc: use ON CONFLICT upsert instead of check-then-insert; the
        # check-then-insert path raced under concurrent writes and produced
        # ConstraintException('Duplicate key "agent_name: ..."').
        self.conn.execute(
            """
            INSERT INTO ohm_agent_state (agent_name, last_sync, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (agent_name) DO UPDATE SET
                last_sync = excluded.last_sync,
                updated_at = excluded.updated_at
            """,
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
            schema=self.schema,  # OHM-8bli: pass schema for domain table mirrors
        )

    # ── DuckLake Sync ────────────────────────────────────────────────

    def _ducklake_table(self, table: str, alias: str) -> str:
        """Return a safely quoted table reference for DuckLake sync."""
        return f'"{alias}"."{table}"'

    def _ducklake_sync_tables(self) -> tuple:
        """Return the registry of tables to mirror in DuckLake (OHM-8bli).

        Excludes the change feed (``ohm_change_feed``) which is synced
        separately via ``_sync_change_feed()`` because it uses
        ``occurred_at`` instead of ``updated_at``/``created_at`` and
        has no ``deleted_at`` column. The returned tuple is in
        declared registry order; each entry is a :class:`DuckLakeTable`
        with the per-table pk/timestamp config.
        """
        return tuple(dlt for dlt in self.schema.ducklake_tables if dlt.name != "ohm_change_feed")

    # Minimum seconds between DuckLake syncs (avoid syncing on every write)
    _MIN_SYNC_INTERVAL_SECONDS: float = 30.0

    def sync_heartbeat(
        self,
        ducklake_path: Optional[str] = None,
        alias: str = "ohm_lake",
        force: bool = False,
    ) -> dict[str, Any]:
        """Push local changes to DuckLake and pull remote changes.

        This is the main sync method called on each agent heartbeat.
        It:
        1. Pushes local changes to DuckLake (if path configured)
        2. Pulls changes from DuckLake (if path configured)

        Throttled to run at most once every _MIN_SYNC_INTERVAL_SECONDS
        unless force=True.
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

        # Throttle: skip sync if last sync was less than _MIN_SYNC_INTERVAL_SECONDS ago
        import time

        now_ts = time.time()
        last_sync_ts = getattr(self, "_last_sync_ts", 0.0)
        if not force and (now_ts - last_sync_ts) < self._MIN_SYNC_INTERVAL_SECONDS:
            return {
                "pushed": 0,
                "pulled": 0,
                "last_sync": None,
                "ducklake_attached": False,
                "throttled": True,
                "reason": f"sync throttled (last sync {now_ts - last_sync_ts:.1f}s ago, min interval {self._MIN_SYNC_INTERVAL_SECONDS}s)",
            }

        pushed = 0
        pulled = 0
        last_sync = None

        # Mark sync start time
        self._last_sync_ts = now_ts

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
            # Push local changes to DuckLake (failures don't block pull)
            try:
                pushed = self.push_to_ducklake(ducklake_path or "", alias=alias)
            except Exception as exc:
                logger.warning("DuckLake push failed (pull will still proceed): %s", exc)
                pushed = 0

            # Pull remote changes from DuckLake (failures don't block return)
            try:
                pulled = self.pull_from_ducklake(ducklake_path or "", alias=alias)
            except Exception as exc:
                logger.warning("DuckLake pull failed: %s", exc)
                pulled = 0

        # Check DuckLake health after sync (OHM-qiio)
        # Only run expensive health check every 5th sync to reduce memory pressure
        self._sync_count = getattr(self, "_sync_count", 0) + 1
        if (ducklake_path or ducklake_attached) and self._sync_count % 5 == 0:
            try:
                dlh = self.check_ducklake_health(alias=alias)
                self.sync_degraded = dlh.get("sync_degraded", False)
                if self.sync_degraded:
                    logger.warning("DuckLake sync degraded: %s", dlh.get("errors", []))
            except Exception:
                self.sync_degraded = True
        elif ducklake_path or ducklake_attached:
            # Skip health check this cycle — still report sync_degraded from last check
            pass

        # Update last_sync timestamp — ensure agent row exists.
        # OHM-cwrc: upsert instead of check-then-insert (same race fix as
        # _log_change and update_agent_state).
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_agent_state (agent_name, last_sync, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (agent_name) DO UPDATE SET
                last_sync = excluded.last_sync,
                updated_at = excluded.updated_at
            """,
            [self.agent_name, now, now],
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
            "ducklake_attached": ducklake_attached,
            "ducklake_path": ducklake_path,
        }

    def check_ducklake_health(self, alias: str = "ohm_lake") -> dict[str, Any]:
        """Check DuckLake sync health and detect corruption (OHM-qiio).

        Compares local and DuckLake row counts, checks for orphaned records,
        and verifies sync freshness. Returns a health dict with sync_degraded flag.

        Returns:
            Dict with: healthy, sync_degraded, local_counts, ducklake_counts,
            orphan_counts, last_push, last_pull, staleness_seconds, errors.
        """

        health = {
            "healthy": True,
            "sync_degraded": False,
            "local_counts": {},
            "ducklake_counts": {},
            "orphan_counts": {},
            "last_push": None,
            "last_pull": None,
            "staleness_seconds": None,
            "errors": [],
        }

        # Check if DuckLake is attached
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
        except Exception as e:
            health["healthy"] = False
            health["sync_degraded"] = True
            health["errors"].append(f"Cannot check DuckLake attachment: {e}")
            return health

        if not attached:
            # No DuckLake configured — not degraded, just local-only
            health["local_counts"] = self._table_counts()
            return health

        # Compare row counts
        # Note: DuckLake tables don't have deleted_at, so we use unfiltered counts
        # and compare against local active (non-deleted) counts.
        # OHM-8bli: iterate the registry instead of a hardcoded 3-table list.
        for dlt in self._ducklake_sync_tables():
            table = dlt.name
            try:
                # OHM-8bli: only filter on deleted_at for tables that have it
                if dlt.has_deleted_at:
                    local_count = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
                else:
                    local_count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # type: ignore[index]
                # DuckLake tables don't have deleted_at column — use unfiltered count
                ducklake_count = self.conn.execute(f"SELECT COUNT(*) FROM {self._ducklake_table(table, alias)}").fetchone()[0]
                health["local_counts"][table] = local_count
                health["ducklake_counts"][table] = ducklake_count

                # Detect orphans: rows in DuckLake not in local active rows
                # DuckLake doesn't have deleted_at, so we compare against local active rows only.
                # OHM-8bli: handle tables that have no deleted_at column.
                if dlt.has_deleted_at:
                    orphan_count = self.conn.execute(f"""
                        SELECT COUNT(*) FROM {self._ducklake_table(table, alias)} dl
                        WHERE NOT EXISTS (
                            SELECT 1 FROM {table} l
                            WHERE l.id = dl.id AND l.deleted_at IS NULL
                        )
                    """).fetchone()[0]  # type: ignore[index]
                else:
                    orphan_count = self.conn.execute(f"""
                        SELECT COUNT(*) FROM {self._ducklake_table(table, alias)} dl
                        WHERE NOT EXISTS (
                            SELECT 1 FROM {table} l WHERE l.id = dl.id
                        )
                    """).fetchone()[0]  # type: ignore[index]
                health["orphan_counts"][table] = orphan_count

            except Exception as e:
                health["errors"].append(f"{table} count error: {e}")

        # Check sync timestamps
        try:
            row = self.conn.execute(
                "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
                [self.agent_name],
            ).fetchone()
            if row and row[0]:
                from datetime import datetime, timezone

                last_sync = row[0]
                if isinstance(last_sync, str):
                    last_sync = datetime.fromisoformat(last_sync)
                health["last_push"] = str(last_sync)
                health["last_pull"] = str(last_sync)
                # Normalize timezone for staleness calculation
                now_utc = datetime.now(timezone.utc)
                if last_sync.tzinfo is None:
                    last_sync = last_sync.replace(tzinfo=timezone.utc)
                staleness = (now_utc - last_sync).total_seconds()
                health["staleness_seconds"] = staleness

                # Stale if last sync > 5 minutes (configurable)
                if staleness > 300:
                    health["sync_degraded"] = True
                    health["errors"].append(f"Last sync {staleness:.0f}s ago (>300s threshold)")

        except Exception as e:
            health["errors"].append(f"Timestamp check error: {e}")

        # Determine overall health
        total_orphans = sum(health["orphan_counts"].values())
        if total_orphans > 0:
            health["sync_degraded"] = True
            health["errors"].append(f"{total_orphans} orphaned rows in DuckLake")

        if health["errors"]:
            health["healthy"] = not health["sync_degraded"]

        return health

    def _table_counts(self) -> dict[str, int]:
        """Get row counts for all mirrored tables (excluding soft-deleted).

        OHM-8bli: iterates the DuckLake registry instead of a hardcoded
        three-table list, so domain tables (e.g. topo_prospects) get
        included in the health report.
        """
        counts = {}
        for dlt in self._ducklake_sync_tables():
            table = dlt.name
            try:
                if dlt.has_deleted_at:
                    counts[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
                else:
                    counts[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # type: ignore[index]
            except Exception:
                counts[table] = -1
        return counts

    def repair_from_ducklake(self, alias: str = "ohm_lake") -> dict[str, Any]:
        """Repair local DuckDB from DuckLake mirror (OHM-qiio).

        Rebuilds local tables from the DuckLake shared backend. Used when
        local data is corrupted or missing. The DuckLake mirror is the
        source of truth.

        Strategy:
        1. Detect which rows are missing locally but exist in DuckLake
        2. INSERT missing rows from DuckLake mirror
        3. UPDATE rows where DuckLake has newer timestamps
        4. SOFT-DELETE rows that are deleted in DuckLake but not locally
        5. Verify row counts match after repair

        Args:
            alias: Database alias for the attached DuckLake catalog.

        Returns:
            Dict with: inserted, updated, soft_deleted, verified, errors.
        """
        result = {"inserted": 0, "updated": 0, "soft_deleted": 0, "verified": True, "errors": []}

        # Check if DuckLake is attached
        try:
            attached = self.conn.execute(
                "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                [alias],
            ).fetchone()
        except Exception as e:
            result["errors"].append(f"Cannot check DuckLake attachment: {e}")
            result["verified"] = False
            return result

        if not attached:
            result["errors"].append("DuckLake not attached — cannot repair")
            result["verified"] = False
            return result

        # OHM-8bli: per-table config now comes from the DuckLakeTable
        # registry on self.schema.ducklake_tables. Each entry carries its
        # own primary_key, timestamp_col, and has_deleted_at flag.
        for dlt in self._ducklake_sync_tables():
            table = dlt.name
            pk = dlt.primary_key
            ts_col = dlt.timestamp_col
            mirror = self._ducklake_table(table, alias)
            try:
                # Get column list for explicit INSERT
                cols = self.conn.execute(f"DESCRIBE {table}").fetchall()
                col_names = [c[0] for c in cols]
                # OHM-8bli: only insert columns that exist in BOTH the
                # local table AND the DuckLake mirror (mirror columns are
                # all-VARCHAR and may be a subset for domain tables).
                mirror_col_rows = self.conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_catalog = ? AND table_name = ? ORDER BY ordinal_position",
                    [alias, table],
                ).fetchall()
                mirror_col_names = {r[0] for r in mirror_col_rows}
                insert_cols = [c for c in col_names if c in mirror_col_names]
                if not insert_cols:
                    # No columns in common — nothing to insert. Skip.
                    continue
                ", ".join(insert_cols)

                # 1. Insert rows in DuckLake but not local
                # OHM-8bli: only filter on deleted_at for tables that have it
                if dlt.has_deleted_at:
                    missing_count = self.conn.execute(f"""
                        SELECT COUNT(*) FROM {mirror} dl
                        WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {pk} = dl.{pk} AND deleted_at IS NULL)
                    """).fetchone()[0]  # type: ignore[index]
                else:
                    missing_count = self.conn.execute(f"""
                        SELECT COUNT(*) FROM {mirror} dl
                        WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {pk} = dl.{pk})
                    """).fetchone()[0]  # type: ignore[index]

                if missing_count > 0:
                    # OHM-8bli: cast VARCHAR mirror columns to the local
                    # column type so the INSERT succeeds. Use a CAST
                    # expression per column.
                    local_type_rows = self.conn.execute(
                        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='main' AND table_name=?",
                        [table],
                    ).fetchall()
                    local_type_map = {r[0]: r[1].upper() for r in local_type_rows}
                    select_parts = []
                    for c in insert_cols:
                        ltype = local_type_map.get(c, "VARCHAR")
                        if ltype in ("FLOAT", "DOUBLE", "REAL"):
                            select_parts.append(f"CAST(dl.{c} AS DOUBLE) AS {c}")
                        elif ltype in ("INTEGER", "BIGINT"):
                            select_parts.append(f"CAST(dl.{c} AS BIGINT) AS {c}")
                        elif ltype in ("TIMESTAMP", "TIMESTAMPTZ", "DATETIME"):
                            select_parts.append(f"CAST(dl.{c} AS TIMESTAMP) AS {c}")
                        elif ltype == "BOOLEAN":
                            select_parts.append(f"CAST(dl.{c} AS BOOLEAN) AS {c}")
                        else:
                            select_parts.append(f"dl.{c} AS {c}")
                    ", ".join(select_parts)
                    # deleted_at needs a default if the table has it but
                    # the mirror doesn't carry a value. Use NULL.
                    final_cols = list(insert_cols)
                    final_selects = list(select_parts)
                    if dlt.has_deleted_at and "deleted_at" not in insert_cols:
                        final_cols.append("deleted_at")
                        final_selects.append("CAST(NULL AS TIMESTAMP) AS deleted_at")
                    fc = ", ".join(final_cols)
                    fs = ", ".join(final_selects)
                    self.conn.execute(f"""
                        INSERT INTO {table} ({fc})
                        SELECT {fs} FROM {mirror} dl
                        WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {pk} = dl.{pk}{" AND deleted_at IS NULL" if dlt.has_deleted_at else ""})
                    """)
                result["inserted"] += missing_count

                # 2. Update rows where DuckLake has newer timestamps
                # OHM-8bli: only update if the table has BOTH a timestamp
                # column AND a deleted_at column (the UPDATE gates on
                # active rows). Cast VARCHAR to TIMESTAMP for the
                # comparison.
                if dlt.timestamp_col in col_names and dlt.has_deleted_at:
                    set_clause = ", ".join(f"{c} = dl.{c}" for c in insert_cols if c != pk and c != "deleted_at")
                    if set_clause:
                        # Use TRY_CAST to avoid hard errors on bad data.
                        updated_count = self.conn.execute(f"""
                            SELECT COUNT(*) FROM {mirror} dl
                            JOIN {table} ON {table}.{pk} = dl.{pk}
                            WHERE {table}.deleted_at IS NULL
                            AND TRY_CAST(dl.{ts_col} AS TIMESTAMP) >
                                COALESCE({table}.{ts_col}, TRY_CAST(dl.{ts_col} AS TIMESTAMP))
                        """).fetchone()[0]  # type: ignore[index]

                        if updated_count > 0:
                            self.conn.execute(f"""
                                UPDATE {table} SET {set_clause}
                                FROM {mirror} dl
                                WHERE {table}.{pk} = dl.{pk}
                                AND {table}.deleted_at IS NULL
                                AND TRY_CAST(dl.{ts_col} AS TIMESTAMP) >
                                    COALESCE({table}.{ts_col}, TRY_CAST(dl.{ts_col} AS TIMESTAMP))
                            """)
                        result["updated"] += updated_count

                # 3. Soft-delete rows absent in DuckLake but still active locally
                # (DuckLake is source of truth — rows not in mirror should be deleted locally)
                # OHM-8bli: respect dlt.has_deleted_at — only tables with
                # deleted_at get the soft-delete treatment.
                if dlt.has_deleted_at:
                    soft_deleted_count = self.conn.execute(f"""
                        SELECT COUNT(*) FROM {table}
                        WHERE deleted_at IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM {mirror} dl
                            WHERE dl.{pk} = {table}.{pk}
                        )
                    """).fetchone()[0]  # type: ignore[index]

                    if soft_deleted_count > 0:
                        from datetime import datetime, timezone

                        now = datetime.now(timezone.utc).isoformat()
                        self.conn.execute(
                            f"""
                            UPDATE {table} SET deleted_at = ?
                            WHERE deleted_at IS NULL
                            AND NOT EXISTS (
                                SELECT 1 FROM {mirror} dl
                                WHERE dl.{pk} = {table}.{pk}
                            )
                        """,
                            [now],
                        )
                    result["soft_deleted"] += soft_deleted_count

            except Exception as e:
                result["errors"].append(f"{table} repair error: {e}")
                result["verified"] = False

        # 4. Verify — check that counts match after repair
        # OHM-8bli: iterate the registry, only filter on deleted_at where
        # the table actually has that column.
        for dlt in self._ducklake_sync_tables():
            table = dlt.name
            try:
                if dlt.has_deleted_at:
                    local_count = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
                else:
                    local_count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # type: ignore[index]
                dl_count = self.conn.execute(f"SELECT COUNT(*) FROM {self._ducklake_table(table, alias)}").fetchone()[0]  # type: ignore[index]
                if local_count != dl_count:
                    result["verified"] = False
                    result["errors"].append(f"{table} count mismatch after repair: local={local_count}, ducklake={dl_count}")
            except Exception as e:
                result["errors"].append(f"{table} verification error: {e}")
                result["verified"] = False

        # Checkpoint after repair
        try:
            self.conn.execute("CHECKPOINT")
        except Exception:
            pass

        return result

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

        # OHM-8bli: iterate the DuckLake registry, not a hardcoded 3-table list
        for dlt in self._ducklake_sync_tables():
            table = dlt.name
            try:
                # Check if mirror table has any data (for initial sync)
                mirror_count = self.conn.execute(f"SELECT COUNT(*) FROM {self._ducklake_table(table, alias)}").fetchone()[0]  # type: ignore[index]

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
        mirror = self._ducklake_table(table, alias)
        self.conn.execute(f"DELETE FROM {mirror}")

        # Filter out soft-deleted rows from sync
        if "deleted_at" in local_col_names:
            self.conn.execute(f"INSERT INTO {mirror} ({common_str}) SELECT {common_str} FROM {table} WHERE deleted_at IS NULL")
        else:
            self.conn.execute(f"INSERT INTO {mirror} ({common_str}) SELECT {common_str} FROM {table}")

        count = self.conn.execute(f"SELECT COUNT(*) FROM {mirror}").fetchone()[0]
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
        mirror = self._ducklake_table(table, alias)
        self.conn.execute(
            f"DELETE FROM {mirror} WHERE id IN ({placeholders})",
            changed_ids,
        )

        id_placeholders = ", ".join(["?"] * len(changed_ids))
        self.conn.execute(f"INSERT INTO {mirror} ({common_str}) SELECT {common_str} FROM {table} WHERE id IN ({id_placeholders})", changed_ids)

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
                    f"INSERT INTO {self._ducklake_table('ohm_change_feed', alias)} (table_name, change_row_id, operation, agent_name, old_data, new_data, occurred_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
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
        try:
            ducklake = duckdb.connect(ducklake_path, read_only=False)
        except Exception as exc:
            logger.warning("Legacy DuckLake push failed to open connection to %s: %s", ducklake_path, exc)
            return 0

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

        # OHM-8bli: iterate the DuckLake registry instead of a hardcoded list
        for dlt in self._ducklake_sync_tables():
            table = dlt.name
            pk = dlt.primary_key
            try:
                mirror = self._ducklake_table(table, alias)
                # Find rows in DuckLake that are not in local table
                # (new rows from other agents). OHM-8bli: use the
                # registry's primary_key instead of hardcoded 'id'.
                new_rows = self.conn.execute(f"SELECT dl.{pk} FROM {mirror} dl LEFT JOIN {table} l ON dl.{pk} = l.{pk} WHERE l.{pk} IS NULL").fetchall()

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
                    id_list = ", ".join(["?"] * len(new_ids))

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

                    self.conn.execute(f"INSERT INTO {table} ({common_str}) SELECT {select_str} FROM {mirror} dl WHERE dl.id IN ({id_list})", new_ids)
                    pulled += len(new_ids)

            except Exception as exc:
                logger.warning("DuckLake pull failed for %s: %s", table, exc)

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
        try:
            ducklake = duckdb.connect(ducklake_path, read_only=True)
        except Exception as exc:
            logger.warning("Legacy DuckLake pull failed to open connection to %s: %s", ducklake_path, exc)
            return 0

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
            graph state at that snapshot version. Returns empty
            results with degraded=True if DuckLake is not attached
            or the query fails.
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
            return {
                "version": version,
                "nodes": [],
                "edges": [],
                "node_count": 0,
                "edge_count": 0,
                "degraded": True,
                "error": "DuckLake is not attached — cannot query historical state",
            }

        try:
            nodes = self.conn.execute(f"SELECT * FROM {alias}.ohm_nodes AT (VERSION => {int(version)})").fetchall()
            node_columns = [desc[0] for desc in self.conn.description]
            nodes_dicts = [dict(zip(node_columns, row)) for row in nodes]
            nodes_dicts = [n for n in nodes_dicts if n.get("deleted_at") is None]
        except Exception as e:
            return {
                "version": version,
                "nodes": [],
                "edges": [],
                "node_count": 0,
                "edge_count": 0,
                "degraded": True,
                "error": f"Failed to query nodes at version {version}: {e}",
            }

        try:
            edges = self.conn.execute(f"SELECT * FROM {alias}.ohm_edges AT (VERSION => {int(version)})").fetchall()
            edge_columns = [desc[0] for desc in self.conn.description]
            edges_dicts = [dict(zip(edge_columns, row)) for row in edges]
            edges_dicts = [e for e in edges_dicts if e.get("deleted_at") is None]
        except Exception as e:
            return {
                "version": version,
                "nodes": nodes_dicts,
                "edges": [],
                "node_count": len(nodes_dicts),
                "edge_count": 0,
                "degraded": True,
                "error": f"Failed to query edges at version {version}: {e}",
            }

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
            the data columns. Returns empty results with degraded=True
            if DuckLake is not attached or the query fails.
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
            return {
                "from_version": from_version,
                "to_version": to_version,
                "node_changes": [],
                "edge_changes": [],
                "degraded": True,
                "error": "DuckLake is not attached — cannot query changes",
            }

        try:
            node_changes = self.conn.execute(f"SELECT * FROM {alias}.table_changes('ohm_nodes', {int(from_version)}, {int(to_version)})").fetchall()
            nc_columns = [desc[0] for desc in self.conn.description]
            node_changes_dicts = [dict(zip(nc_columns, row)) for row in node_changes]
        except Exception:
            node_changes_dicts = []

        try:
            edge_changes = self.conn.execute(f"SELECT * FROM {alias}.table_changes('ohm_edges', {int(from_version)}, {int(to_version)})").fetchall()
            ec_columns = [desc[0] for desc in self.conn.description]
            edge_changes_dicts = [dict(zip(ec_columns, row)) for row in edge_changes]
        except Exception:
            edge_changes_dicts = []

        return {
            "from_version": from_version,
            "to_version": to_version,
            "node_changes": node_changes_dicts,
            "edge_changes": edge_changes_dicts,
        }
