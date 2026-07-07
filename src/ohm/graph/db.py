"""OHM database connection management.

Handles DuckDB connection lifecycle, schema initialization,
and connection configuration.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import duckdb
    from duckdb import DuckDBPyConnection

    from ohm.graph.schema import SchemaConfig


def _get_ducklake_path() -> str:
    """Return the DuckLake catalog path, env var first then config file fallback."""
    path = os.environ.get("OHM_DUCKLAKE_PATH", "")
    if path:
        return path
    config_file = pathlib.Path(os.environ.get("OHM_CONFIG", str(pathlib.Path.home() / ".ohm" / "ohmd.json")))
    if config_file.exists():
        try:
            with open(config_file) as f:
                cfg = json.load(f)
            path = cfg.get("ducklake", {}).get("path", "")
        except Exception as e:
            logger.debug("Failed to read ducklake path from config %s: %s", config_file, e, exc_info=True)
    return path


def get_default_db_path() -> pathlib.Path:
    """Return the default database path.

    Uses $OHM_DB if set, otherwise ``./ohm.db``.
    """
    env_path = os.environ.get("OHM_DB")
    if env_path:
        return pathlib.Path(env_path)
    return pathlib.Path.cwd() / "ohm.db"


def connect(db_path: str | pathlib.Path | None = None) -> "duckdb.DuckDBPyConnection":
    """Open a DuckDB connection and initialize the schema.

    Handles WAL corruption recovery (OHM-b5a): if DuckDB fails to open
    due to WAL replay errors, deletes the WAL file and retries.
    NOTE: DuckDB's WAL contains *committed* writes that have not yet been
    checkpointed into the main .duckdb file — NOT uncommitted writes.
    Deleting the WAL therefore discards committed-but-not-checkpointed data.
    This is intentional for corruption recovery; use /admin/checkpoint to
    flush safely before stopping the daemon.

    Args:
        db_path: Path to the DuckDB file. If None, uses the default path.

    Returns:
        An active DuckDB connection with the OHM schema initialized.
    """
    import duckdb

    if db_path is None:
        db_path = get_default_db_path()

    db_path_str = str(db_path)

    try:
        conn = duckdb.connect(db_path_str)
    except (duckdb.IOException, duckdb.InternalException) as e:
        # Check if this is a WAL corruption error
        # DuckDB raises InternalException for WAL replay failures
        # (e.g., "Calling DatabaseManager::GetDefaultDatabase with no
        # default database set"), not just IOException. Both must be
        # caught for reliable recovery.
        error_msg = str(e)
        if "WAL" in error_msg or "wal" in error_msg.lower() or "replay" in error_msg.lower():
            # Try DuckLake recovery first (OHM-kdk.4)
            restored = _try_ducklake_recovery(db_path_str)
            if restored:
                conn = duckdb.connect(db_path_str)
            else:
                # Fall back to WAL deletion (OHM-b5a)
                wal_path = db_path_str + ".wal"
                if os.path.exists(wal_path):
                    os.remove(wal_path)
                conn = duckdb.connect(db_path_str)
        else:
            raise

    # Load required extensions
    _load_extensions(conn)

    # Apply performance PRAGMAs (threads, enable_object_cache,
    # temp_directory) -- OHM-lqpk.3. Done before schema init so any
    # query inside initialize_schema benefits from the larger worker
    # pool.
    _apply_pragmas(conn)

    # Initialize schema
    from ohm.schema import initialize_schema

    initialize_schema(conn)

    # OHM-8n9: Auto-restore from DuckLake if tables are empty.
    # This handles the case where WAL was deleted after corruption
    # but the main DB file contained no checkpointed data.
    _auto_restore_if_empty(conn, db_path_str)

    return conn


def _apply_pragmas(conn: "duckdb.DuckDBPyConnection") -> None:
    """Apply DuckDB performance PRAGMAs to a connection (OHM-lqpk.3).

    Three knobs tuned for OHM's read-heavy analytical workload:

    - ``PRAGMA threads = N`` -- the size of the parallel worker pool
      DuckDB uses for query execution. Default: ``max(1, cpu_count // 2)``
      (DuckDB's own default is the full core count, which over-saturates
      shared production boxes). Override via ``OHM_DUCKDB_THREADS``.

    - ``PRAGMA enable_object_cache = true`` -- caches parsed query plans
      across ``execute()`` calls. Each OHM endpoint issues many similar
      queries (filters by node id, layer, edge type); caching the parsed
      plan shaves microseconds off every call. Negligible memory cost.

    - ``PRAGMA temp_directory = '<path>'`` -- where DuckDB spills memory
      when a single query exceeds ``memory_limit``. Only set when
      ``OHM_DUCKDB_TEMP_DIR`` is configured; DuckDB's own default
      (system temp) is fine for most deployments.

    - ``PRAGMA wal_autocheckpoint = N`` -- fold the WAL into the main
      .duckdb file automatically after N pages of writes (1 page = 4KB).
      DuckDB's default is 1000 pages (~4MB). For a knowledge-graph workload
      with modest write volume, the WAL never grows that large, so auto-
      checkpoint never fires and committed data stays in the WAL file only.
      Setting this to 100 (~400KB) ensures frequent folding without
      meaningful overhead. Override via ``OHM_WAL_AUTOCHECKPOINT`` (pages).

    Errors are logged and swallowed: PRAGMA tuning is a perf optimisation,
    not a correctness requirement. A container that can't write to the
    configured temp dir still runs -- it just spills to the default.
    """
    try:
        env_threads = os.environ.get("OHM_DUCKDB_THREADS")
        if env_threads is not None:
            try:
                threads = max(1, int(env_threads))
            except ValueError:
                logger.warning(
                    "OHM_DUCKDB_THREADS=%r is not an integer; using default",
                    env_threads,
                )
                threads = max(1, (os.cpu_count() or 1) // 2)
        else:
            threads = max(1, (os.cpu_count() or 1) // 2)
        conn.execute(f"PRAGMA threads = {threads}")
    except Exception:
        logger.debug("PRAGMA threads failed", exc_info=True)

    try:
        conn.execute("PRAGMA enable_object_cache = true")
    except Exception:
        logger.debug("PRAGMA enable_object_cache failed", exc_info=True)

    temp_dir = os.environ.get("OHM_DUCKDB_TEMP_DIR")
    if temp_dir:
        try:
            # PRAGMA temp_directory doesn't support parameterised args,
            # so we escape single quotes in the path and wrap it in SQL
            # string literal quotes before interpolation. The env var is
            # operator-controlled, not user-supplied, but defence-in-depth
            # matters when we interpolate.
            safe_temp_dir = temp_dir.replace("'", "''")
            conn.execute(f"PRAGMA temp_directory = '{safe_temp_dir}'")
        except Exception:
            logger.debug("PRAGMA temp_directory failed", exc_info=True)

    try:
        raw = os.environ.get("OHM_WAL_AUTOCHECKPOINT", "100")
        pages = max(1, int(raw))
        conn.execute(f"PRAGMA wal_autocheckpoint = {pages}")
    except Exception:
        logger.debug("PRAGMA wal_autocheckpoint failed", exc_info=True)


def _load_extensions(conn: "duckdb.DuckDBPyConnection") -> None:
    """Load DuckDB extensions needed by OHM.

    Always loads: json
    Optionally loads: quack (if available, for concurrent multi-writer access)

    Uses INSTALL (not FORCE INSTALL) to avoid re-downloading extensions
    that are already cached locally. FORCE INSTALL re-downloads every time,
    leaving orphaned .tmp- files if interrupted.
    """
    # Check if DuckLake is configured before loading extensions, so we can
    # warn appropriately if it's needed but unavailable.
    ducklake_configured = bool(_get_ducklake_path())

    extensions = ["json", "ducklake"]
    for ext in extensions:
        try:
            conn.execute(f"INSTALL {ext}; LOAD {ext};")
        except Exception as e:
            logger.debug("Extension %s not available: %s", ext, e, exc_info=True)

    # Try to load Quack extension (optional, for concurrent access)
    # Use INSTALL (not FORCE INSTALL) to avoid re-downloading on every call.
    # If the extension isn't cached yet, INSTALL fetches it once; subsequent
    # calls skip the download entirely.
    try:
        conn.execute("INSTALL quack FROM core_nightly; LOAD quack;")
    except Exception as e:
        logger.debug("Quack extension not available — single-writer mode: %s", e, exc_info=True)

    # Try to load DuckLake extension (optional, for lakehouse sync)
    # DuckLake provides ACID multi-table transactions, time travel, and
    # Parquet-based storage. Required for OHM-kdk (DuckLake shared backend).
    # If DuckLake is configured but the extension fails to load, warn loudly
    # so operators are aware that lakehouse features are degraded.
    try:
        conn.execute("INSTALL ducklake FROM core; LOAD ducklake;")
    except Exception as e:
        if ducklake_configured:
            logger.warning(
                "DuckLake extension not available but DuckLake is configured at %s — lakehouse features disabled: %s",
                _get_ducklake_path(),
                e,
                exc_info=True,
            )
        else:
            logger.debug("DuckLake extension not available — lakehouse features disabled: %s", e, exc_info=True)

    # Try to load VSS extension (optional, for semantic search)
    # VSS provides HNSW index for fast vector similarity search.
    # Required for semantic search (OHM-o9f). Gracefully degrades if
    # unavailable — embedding column still works without the index.
    try:
        conn.execute("INSTALL vss; LOAD vss;")
        # Enable HNSW index persistence so it survives DB restarts
        conn.execute("SET hnsw_enable_experimental_persistence = true;")
    except Exception as e:
        logger.debug("VSS extension not available — semantic search uses brute-force: %s", e, exc_info=True)


def close(conn: "duckdb.DuckDBPyConnection") -> None:
    """Close a DuckDB connection safely."""
    try:
        conn.close()
    except Exception as e:
        logger.debug("Error closing DuckDB connection: %s", e, exc_info=True)


def _try_ducklake_recovery(db_path_str: str) -> bool:
    """Try to restore from DuckLake snapshot after WAL corruption (OHM-kdk.4).

    If DuckLake is configured and has snapshots, exports the graph state
    from the latest snapshot and imports it into a fresh DuckDB database.
    This preserves data that would otherwise be lost when deleting the WAL.

    Returns True if recovery succeeded, False if DuckLake is not available.
    """
    import duckdb

    ducklake_path = _get_ducklake_path()
    if not ducklake_path:
        return False

    # Create a temp connection to load DuckLake and check snapshots
    tmp_conn = None
    try:
        tmp_conn = duckdb.connect(":memory:")
        # Load DuckLake extension
        try:
            tmp_conn.execute("INSTALL ducklake FROM core; LOAD ducklake;")
        except Exception as e:
            logger.debug("DuckLake extension unavailable during recovery: %s", e, exc_info=True)
            return False

        # Attach DuckLake catalog
        try:
            tmp_conn.execute(f"ATTACH 'ducklake:{ducklake_path}' AS ohm_lake")
        except Exception as e:
            logger.debug("Failed to attach DuckLake catalog %s: %s", ducklake_path, e, exc_info=True)
            return False

        # Get latest snapshot
        snapshots = tmp_conn.execute("SELECT snapshot_id FROM ducklake_snapshots('ohm_lake') ORDER BY snapshot_time DESC LIMIT 1").fetchone()
        if not snapshots:
            return False

        snapshot_id = int(snapshots[0])  # Security: enforce integer type before SQL interpolation

        # Export nodes and edges from snapshot
        nodes = tmp_conn.execute(f"SELECT * FROM ohm_lake.ohm_nodes AT (VERSION => {snapshot_id})").fetchall()
        edges = tmp_conn.execute(f"SELECT * FROM ohm_lake.ohm_edges AT (VERSION => {snapshot_id})").fetchall()

        node_cols = [d[0] for d in tmp_conn.description]
        edge_cols = [d[0] for d in tmp_conn.description]

        if not nodes and not edges:
            return False

        # Backup corrupted DB and create fresh one
        backup_path = db_path_str + ".corrupted"
        shutil.move(db_path_str, backup_path)
        # Also remove WAL if present
        wal_path = db_path_str + ".wal"
        if os.path.exists(wal_path):
            os.remove(wal_path)

        # Create fresh DB with OHM schema
        fresh_conn = duckdb.connect(db_path_str)
        from ohm.schema import initialize_schema

        initialize_schema(fresh_conn)

        # Import nodes
        for node in nodes:
            node_dict = dict(zip(node_cols, node))
            cols = [c for c in node_cols if c in node_dict]
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            values = [node_dict[c] for c in cols]
            # Check if already exists (including soft-deleted) to avoid PK violation
            # which causes DuckDB FatalException (uncatchable abort)
            node_id = node_dict.get("id")
            if node_id:
                existing = fresh_conn.execute("SELECT id FROM ohm_nodes WHERE id = ?", [node_id]).fetchone()
                if existing:
                    continue  # Skip duplicates
            try:
                fresh_conn.execute(
                    f"INSERT INTO ohm_nodes ({col_names}) VALUES ({placeholders})",
                    values,
                )
            except Exception as e:
                logger.debug("Skipping duplicate node %s during recovery: %s", node_id, e, exc_info=True)

        # Import edges
        for edge in edges:
            edge_dict = dict(zip(edge_cols, edge))
            cols = [c for c in edge_cols if c in edge_dict]
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            values = [edge_dict[c] for c in cols]
            # Check if already exists to avoid PK violation (DuckDB FatalException)
            edge_id = edge_dict.get("id")
            if edge_id:
                existing = fresh_conn.execute("SELECT id FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
                if existing:
                    continue
            try:
                fresh_conn.execute(
                    f"INSERT INTO ohm_edges ({col_names}) VALUES ({placeholders})",
                    values,
                )
            except Exception as e:
                logger.debug("Skipping duplicate edge %s during recovery: %s", edge_id, e, exc_info=True)

        # Log recovery
        fresh_conn.execute(
            """INSERT INTO ohm_change_feed
               (table_name, row_id, operation, agent_name, new_data)
               VALUES (?, ?, ?, ?, ?)""",
            [
                "ohm_meta",
                "recovery",
                "RECOVERY",
                "ohmd",
                json.dumps(
                    {
                        "snapshot_id": snapshot_id,
                        "nodes_restored": len(nodes),
                        "edges_restored": len(edges),
                        "corrupted_db": backup_path,
                    }
                ),
            ],
        )

        fresh_conn.close()
        return True

    except Exception as e:
        logger.debug("DuckLake recovery failed: %s", e, exc_info=True)
        return False
    finally:
        if tmp_conn:
            try:
                tmp_conn.close()
            except Exception as e:
                logger.debug("Error closing temp connection: %s", e, exc_info=True)


def _auto_restore_if_empty(conn: "duckdb.DuckDBPyConnection", db_path_str: str) -> None:
    """Check if ohm_nodes is empty and auto-restore from DuckLake (OHM-8n9/OHM-6cz).

    If the database opened successfully but contains no nodes (e.g., after
    WAL deletion that contained committed-but-not-checkpointed data),
    attempt to restore from the latest DuckLake snapshot.
    """
    try:
        node_count = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
    except Exception:
        return

    if node_count > 0:
        return

    ducklake_path = _get_ducklake_path()
    if not ducklake_path:
        return

    import duckdb as _duckdb

    tmp_conn = None
    try:
        tmp_conn = _duckdb.connect(":memory:")
        try:
            tmp_conn.execute("INSTALL ducklake FROM core; LOAD ducklake;")
        except Exception as e:
            logger.debug("DuckLake extension unavailable for auto-restore: %s", e, exc_info=True)
            return

        try:
            tmp_conn.execute(f"ATTACH 'ducklake:{ducklake_path}' AS ohm_lake")
        except Exception as e:
            logger.debug("Failed to attach DuckLake catalog for auto-restore: %s", e, exc_info=True)
            return

        snapshots = tmp_conn.execute("SELECT snapshot_id FROM ducklake_snapshots('ohm_lake') ORDER BY snapshot_time DESC LIMIT 1").fetchone()
        if not snapshots:
            return

        snapshot_id = int(snapshots[0])  # Security: enforce integer type before SQL interpolation

        nodes = tmp_conn.execute(f"SELECT * FROM ohm_lake.ohm_nodes AT (VERSION => {snapshot_id}) WHERE deleted_at IS NULL").fetchall()
        node_cols = [d[0] for d in tmp_conn.description]
        edges = tmp_conn.execute(f"SELECT * FROM ohm_lake.ohm_edges AT (VERSION => {snapshot_id}) WHERE deleted_at IS NULL").fetchall()
        edge_cols = [d[0] for d in tmp_conn.description]

        # Also restore observations if the table exists in DuckLake
        obs = []
        obs_cols = []
        try:
            obs = tmp_conn.execute(f"SELECT * FROM ohm_lake.ohm_observations AT (VERSION => {snapshot_id})").fetchall()
            obs_cols = [d[0] for d in tmp_conn.description] if obs else []
        except Exception:
            logger.debug("No ohm_observations in DuckLake snapshot, skipping")

        if not nodes and not edges:
            return

        node_count_restored = 0
        for node in nodes:
            node_dict = dict(zip(node_cols, node))
            node_id = node_dict.get("id")
            if not node_id:
                continue
            existing = conn.execute("SELECT id FROM ohm_nodes WHERE id = ?", [node_id]).fetchone()
            if existing:
                continue
            cols = [c for c in node_cols if c in node_dict and node_dict[c] is not None]
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            values = [node_dict[c] for c in cols]
            try:
                conn.execute(f"INSERT INTO ohm_nodes ({col_names}) VALUES ({placeholders})", values)
                node_count_restored += 1
            except Exception as e:
                logger.debug("Skipping duplicate node %s during auto-restore: %s", node_id, e, exc_info=True)

        edge_count_restored = 0
        for edge in edges:
            edge_dict = dict(zip(edge_cols, edge))
            edge_id = edge_dict.get("id")
            if not edge_id:
                continue
            existing = conn.execute("SELECT id FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
            if existing:
                continue
            cols = [c for c in edge_cols if c in edge_dict and edge_dict[c] is not None]
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            values = [edge_dict[c] for c in cols]
            try:
                conn.execute(f"INSERT INTO ohm_edges ({col_names}) VALUES ({placeholders})", values)
                edge_count_restored += 1
            except Exception as e:
                logger.debug("Skipping duplicate edge %s during auto-restore: %s", edge_id, e, exc_info=True)

        obs_count_restored = 0
        for observation in obs:
            obs_dict = dict(zip(obs_cols, observation))
            obs_id = obs_dict.get("id")
            if not obs_id:
                continue
            existing = conn.execute("SELECT id FROM ohm_observations WHERE id = ?", [obs_id]).fetchone()
            if existing:
                continue
            # Only insert columns that exist in production schema and are non-NULL
            cols = [c for c in obs_cols if c in obs_dict and obs_dict[c] is not None]
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            values = [obs_dict[c] for c in cols]
            try:
                conn.execute(f"INSERT INTO ohm_observations ({col_names}) VALUES ({placeholders})", values)
                obs_count_restored += 1
            except Exception as e:
                logger.debug("Skipping duplicate observation %s during auto-restore: %s", obs_id, e, exc_info=True)

        if node_count_restored > 0 or edge_count_restored > 0 or obs_count_restored:
            conn.execute("CHECKPOINT")
            conn.execute(
                """INSERT INTO ohm_change_feed
                   (table_name, row_id, operation, agent_name, new_data)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    "ohm_meta",
                    "auto_restore",
                    "AUTO_RESTORE",
                    "ohmd",
                    json.dumps(
                        {
                            "trigger": "empty_graph_on_startup",
                            "snapshot_id": snapshot_id,
                            "nodes_restored": node_count_restored,
                            "edges_restored": edge_count_restored,
                            "obs_restored": obs_count_restored,
                        }
                    ),
                ],
            )

    except Exception as e:
        logger.debug("Auto-restore from DuckLake failed: %s", e, exc_info=True)
    finally:
        if tmp_conn:
            try:
                tmp_conn.close()
            except Exception as e:
                logger.debug("Error closing temp connection after auto-restore: %s", e, exc_info=True)


def attach_ducklake(
    conn: "duckdb.DuckDBPyConnection",
    catalog_path: str,
    data_path: str | None = None,
    alias: str = "ohm_lake",
    schema: "SchemaConfig | None" = None,
) -> bool:
    """Attach a DuckLake catalog to the connection.

    Creates the catalog if it doesn't exist. Mirror tables (ohm_nodes,
    ohm_edges, ohm_observations) are created in the DuckLake schema
    without PRIMARY KEY constraints (DuckLake limitation).

    Args:
        conn: Active DuckDB connection with DuckLake extension loaded.
        catalog_path: Path to the DuckLake catalog file
        data_path: Optional path for DuckLake data files
        alias: Database alias for the attached catalog
        schema: Optional SchemaConfig — when provided, mirror tables
            are also created for domain tables in the schema (OHM-8bli).
            Without this, only the four core OHM tables get mirrors.
            (e.g., '/var/lib/ohm/ohm_lake.ducklake').
            Uses the ducklake: protocol prefix automatically.
        data_path: Path for Parquet data files. If None, defaults to
            a 'data' subdirectory next to the catalog.
        alias: Database alias for the attached catalog (default: 'ohm_lake').

    Returns:
        True if DuckLake was attached successfully, False if the
        DuckLake extension is not available.
    """
    # Ensure DuckLake extension is loaded
    try:
        conn.execute("INSTALL ducklake FROM core")
        conn.execute("LOAD ducklake")
        logger.debug("DuckLake extension loaded for attach")
    except Exception as e:
        logger.debug("DuckLake extension install/load failed: %s", e, exc_info=True)

    # Check if DuckLake extension is loaded
    try:
        result = conn.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded = true AND extension_name = 'ducklake'").fetchone()
        if result is None:
            return False  # extension not loaded
    except Exception as e:
        logger.debug("DuckLake extension check failed: %s", e, exc_info=True)
        return False

    # Build ATTACH statement
    # DuckLake uses the ducklake: protocol for the catalog
    attach_sql = f"ATTACH 'ducklake:{catalog_path}' AS {alias}"
    if data_path:
        attach_sql += f" (DATA_PATH '{data_path}')"

    try:
        conn.execute(attach_sql)
    except Exception as e:
        # If already attached, that's fine
        err_msg = str(e).lower()
        if "already attached" in err_msg or "already exists" in err_msg:
            return True
        logger.debug("DuckLake ATTACH failed: %s", e, exc_info=True)
        return False

    # Create mirror tables in DuckLake schema (no PKs — DuckLake constraint)
    # OHM-8bli: pass schema so domain tables get mirrors too.
    _create_ducklake_tables(conn, alias, schema=schema)

    return True


def _create_ducklake_tables(
    conn: "DuckDBPyConnection",
    alias: str,
    schema: "SchemaConfig | None" = None,
) -> None:
    """Create OHM mirror tables in DuckLake schema (OHM-8bli).

    DuckLake does NOT support PRIMARY KEY or UNIQUE constraints.
    All columns use VARCHAR to avoid type-mismatch issues with
    Parquet serialization. Node/edge uniqueness is enforced in
    application code (ohmd upsert logic).

    The four core OHM tables (ohm_nodes, ohm_edges, ohm_observations,
    ohm_change_feed) use explicit VARCHAR DDL with hand-picked column
    lists — these columns are part of the OHM core API and must not
    change shape. Any additional tables from the DuckLake registry
    (e.g. domain tables like topo_prospects from OHM-vl8o) get their
    mirror DDL generated dynamically from information_schema.columns.
    """
    # Core OHM tables: explicit VARCHAR DDL. Do not regenerate from
    # information_schema — the column set is the public API of the
    # core schema and must stay stable across DuckLake versions.
    core_mirror_ddl: dict[str, str] = {
        "ohm_nodes": """
            CREATE TABLE IF NOT EXISTS {alias}.ohm_nodes (
                id                      VARCHAR,
                label                   VARCHAR,
                type                    VARCHAR,
                content                 VARCHAR,
                url                     VARCHAR,
                created_by              VARCHAR,
                created_at              VARCHAR,
                updated_at              VARCHAR,
                updated_by              VARCHAR,
                confidence              VARCHAR,
                visibility              VARCHAR,
                provenance              VARCHAR,
                tags                    VARCHAR,
                metadata                VARCHAR,
                priority                VARCHAR,
                utility_scale           VARCHAR,
                current_best_action     VARCHAR,
                action_alternatives     VARCHAR,
                utility_usd_per_day     VARCHAR,
                utility_currency        VARCHAR,
                deleted_at              VARCHAR
            )
        """,
        "ohm_edges": """
            CREATE TABLE IF NOT EXISTS {alias}.ohm_edges (
                id                  VARCHAR,
                from_node           VARCHAR,
                to_node             VARCHAR,
                layer               VARCHAR,
                edge_type           VARCHAR,
                confidence          VARCHAR,
                probability         VARCHAR,
                probability_p05     VARCHAR,
                probability_p50     VARCHAR,
                probability_p95     VARCHAR,
                confidence_p05      VARCHAR,
                confidence_p50      VARCHAR,
                confidence_p95      VARCHAR,
                urgency             VARCHAR,
                condition           VARCHAR,
                provenance          VARCHAR,
                created_by          VARCHAR,
                created_at          VARCHAR,
                updated_at          VARCHAR,
                updated_by          VARCHAR,
                challenge_of        VARCHAR,
                challenge_type      VARCHAR,
                metadata            VARCHAR,
                deleted_at          VARCHAR
            )
        """,
        "ohm_observations": """
            CREATE TABLE IF NOT EXISTS {alias}.ohm_observations (
                id          VARCHAR,
                node_id     VARCHAR,
                edge_id     VARCHAR,
                type        VARCHAR,
                value       VARCHAR,
                baseline    VARCHAR,
                sigma       VARCHAR,
                source      VARCHAR,
                created_by  VARCHAR,
                created_at  VARCHAR,
                metadata    VARCHAR,
                notes       VARCHAR,
                source_name VARCHAR,
                source_url  VARCHAR,
                deleted_at  VARCHAR
            )
        """,
        "ohm_change_feed": """
            CREATE TABLE IF NOT EXISTS {alias}.ohm_change_feed (
                id          BIGINT,
                table_name  VARCHAR,
                change_row_id  VARCHAR,
                operation   VARCHAR,
                agent_name  VARCHAR,
                old_data    VARCHAR,
                new_data    VARCHAR,
                occurred_at VARCHAR
            )
        """,
        "ohm_outcomes": """
            CREATE TABLE IF NOT EXISTS {alias}.ohm_outcomes (
                id           VARCHAR,
                source_agent VARCHAR,
                claim_node   VARCHAR,
                outcome      VARCHAR,
                recorded_by  VARCHAR,
                recorded_at  VARCHAR,
                notes        VARCHAR,
                claimed_by   VARCHAR,
                verified_by  VARCHAR,
                domain       VARCHAR
            )
        """,
    }

    for table_name, ddl in core_mirror_ddl.items():
        try:
            conn.execute(ddl.format(alias=alias))
        except Exception as e:
            logger.debug("Skipping mirror table %s (may already exist): %s", table_name, e, exc_info=True)

    # OHM-8bli: For domain tables in the registry, generate the mirror
    # DDL dynamically from information_schema.columns. The result is
    # all-VARCHAR (no PKs) to match the core table convention.
    if schema is not None:
        # Tables that already got explicit DDL above — skip.
        core_names = set(core_mirror_ddl.keys())
        try:
            for dlt in schema.ducklake_tables:
                if dlt.name in core_names:
                    continue
                try:
                    cols = conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
                        [dlt.name],
                    ).fetchall()
                except Exception:
                    continue
                if not cols:
                    continue
                col_lines = ", ".join(f"{c[0]} VARCHAR" for c in cols)
                mirror_sql = f"CREATE TABLE IF NOT EXISTS {alias}.{dlt.name} ({col_lines})"
                try:
                    conn.execute(mirror_sql)
                except Exception as e:
                    logger.debug(
                        "Skipping mirror table %s (may already exist): %s",
                        dlt.name,
                        e,
                        exc_info=True,
                    )
        except Exception as e:
            # If schema.ducklake_tables is unavailable, skip the
            # dynamic DDL — core tables still work.
            logger.debug("DuckLake domain table DDL skipped: %s", e, exc_info=True)
