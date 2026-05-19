"""OHM database connection management.

Handles DuckDB connection lifecycle, schema initialization,
and connection configuration.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb
    from duckdb import DuckDBPyConnection


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
    The WAL contains only uncommitted writes, so this is safe — the
    main DB file is intact.

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

    # Initialize schema
    from ohm.schema import initialize_schema
    initialize_schema(conn)

    return conn


def _load_extensions(conn: "duckdb.DuckDBPyConnection") -> None:
    """Load DuckDB extensions needed by OHM.

    Always loads: json
    Optionally loads: quack (if available, for concurrent multi-writer access)

    Uses INSTALL (not FORCE INSTALL) to avoid re-downloading extensions
    that are already cached locally. FORCE INSTALL re-downloads every time,
    leaving orphaned .tmp- files if interrupted.
    """
    extensions = ["json"]
    for ext in extensions:
        try:
            conn.execute(f"INSTALL {ext}; LOAD {ext};")
        except Exception:
            pass  # Extension may already be installed

    # Try to load Quack extension (optional, for concurrent access)
    # Use INSTALL (not FORCE INSTALL) to avoid re-downloading on every call.
    # If the extension isn't cached yet, INSTALL fetches it once; subsequent
    # calls skip the download entirely.
    try:
        conn.execute("INSTALL quack FROM core_nightly; LOAD quack;")
    except Exception:
        pass  # Quack not available — fall back to single-writer mode

    # Try to load DuckLake extension (optional, for lakehouse sync)
    # DuckLake provides ACID multi-table transactions, time travel, and
    # Parquet-based storage. Required for OHM-kdk (DuckLake shared backend).
    try:
        conn.execute("INSTALL ducklake FROM core; LOAD ducklake;")
    except Exception:
        pass  # DuckLake not available — lakehouse features disabled

    # Try to load VSS extension (optional, for semantic search)
    # VSS provides HNSW index for fast vector similarity search.
    # Required for semantic search (OHM-o9f). Gracefully degrades if
    # unavailable — embedding column still works without the index.
    try:
        conn.execute("INSTALL vss; LOAD vss;")
        # Enable HNSW index persistence so it survives DB restarts
        conn.execute("SET hnsw_enable_experimental_persistence = true;")
    except Exception:
        pass  # VSS not available — semantic search falls back to brute-force


def close(conn: "duckdb.DuckDBPyConnection") -> None:
    """Close a DuckDB connection safely."""
    try:
        conn.close()
    except Exception:
        pass


def _try_ducklake_recovery(db_path_str: str) -> bool:
    """Try to restore from DuckLake snapshot after WAL corruption (OHM-kdk.4).

    If DuckLake is configured and has snapshots, exports the graph state
    from the latest snapshot and imports it into a fresh DuckDB database.
    This preserves data that would otherwise be lost when deleting the WAL.

    Returns True if recovery succeeded, False if DuckLake is not available.
    """
    import duckdb

    ducklake_path = os.environ.get("OHM_DUCKLAKE_PATH")
    if not ducklake_path:
        return False

    # Create a temp connection to load DuckLake and check snapshots
    tmp_conn = None
    try:
        tmp_conn = duckdb.connect(":memory:")
        # Load DuckLake extension
        try:
            tmp_conn.execute("INSTALL ducklake FROM core; LOAD ducklake;")
        except Exception:
            return False

        # Attach DuckLake catalog
        try:
            tmp_conn.execute(f"ATTACH 'ducklake:{ducklake_path}' AS ohm_lake")
        except Exception:
            return False

        # Get latest snapshot
        snapshots = tmp_conn.execute(
            "SELECT snapshot_id FROM ducklake_snapshots('ohm_lake') "
            "ORDER BY snapshot_time DESC LIMIT 1"
        ).fetchone()
        if not snapshots:
            return False

        snapshot_id = snapshots[0]

        # Export nodes and edges from snapshot
        nodes = tmp_conn.execute(
            f"SELECT * FROM ohm_lake.ohm_nodes AT (VERSION => {snapshot_id})"
        ).fetchall()
        edges = tmp_conn.execute(
            f"SELECT * FROM ohm_lake.ohm_edges AT (VERSION => {snapshot_id})"
        ).fetchall()

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
            try:
                fresh_conn.execute(
                    f"INSERT INTO ohm_nodes ({col_names}) VALUES ({placeholders})",
                    values,
                )
            except Exception:
                pass  # Skip duplicates

        # Import edges
        for edge in edges:
            edge_dict = dict(zip(edge_cols, edge))
            cols = [c for c in edge_cols if c in edge_dict]
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            values = [edge_dict[c] for c in cols]
            try:
                fresh_conn.execute(
                    f"INSERT INTO ohm_edges ({col_names}) VALUES ({placeholders})",
                    values,
                )
            except Exception:
                pass  # Skip duplicates

        # Log recovery
        fresh_conn.execute(
            """INSERT INTO ohm_change_feed
               (table_name, row_id, operation, agent_name, new_data)
               VALUES (?, ?, ?, ?, ?)""",
            [
                "ohm_meta", "recovery",
                "RECOVERY",
                "ohmd",
                json.dumps({
                    "snapshot_id": snapshot_id,
                    "nodes_restored": len(nodes),
                    "edges_restored": len(edges),
                    "corrupted_db": backup_path,
                }),
            ],
        )

        fresh_conn.close()
        return True

    except Exception:
        return False
    finally:
        if tmp_conn:
            try:
                tmp_conn.close()
            except Exception:
                pass


def attach_ducklake(
    conn: "duckdb.DuckDBPyConnection",
    catalog_path: str,
    data_path: str | None = None,
    alias: str = "ohm_lake",
) -> bool:
    """Attach a DuckLake catalog to the connection.

    Creates the catalog if it doesn't exist. Mirror tables (ohm_nodes,
    ohm_edges, ohm_observations) are created in the DuckLake schema
    without PRIMARY KEY constraints (DuckLake limitation).

    Args:
        conn: Active DuckDB connection with DuckLake extension loaded.
        catalog_path: Path to the DuckLake catalog file
            (e.g., '/var/lib/ohm/ohm_lake.ducklake').
            Uses the ducklake: protocol prefix automatically.
        data_path: Path for Parquet data files. If None, defaults to
            a 'data' subdirectory next to the catalog.
        alias: Database alias for the attached catalog (default: 'ohm_lake').

    Returns:
        True if DuckLake was attached successfully, False if the
        DuckLake extension is not available.
    """
    # Check if DuckLake extension is loaded
    try:
        conn.execute(
            "SELECT extension_name FROM duckdb_extensions()"
            " WHERE loaded = true AND extension_name = 'ducklake'"
        ).fetchone()
    except Exception:
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
        raise

    # Create mirror tables in DuckLake schema (no PKs — DuckLake constraint)
    _create_ducklake_tables(conn, alias)

    return True


def _create_ducklake_tables(conn: "DuckDBPyConnection", alias: str) -> None:
    """Create OHM mirror tables in DuckLake schema.

    DuckLake does NOT support PRIMARY KEY or UNIQUE constraints.
    All columns use VARCHAR to avoid type-mismatch issues with
    Parquet serialization. Node/edge uniqueness is enforced in
    application code (ohmd upsert logic).
    """
    mirror_tables = {
        "ohm_nodes": """
            CREATE TABLE IF NOT EXISTS {alias}.ohm_nodes (
                id            VARCHAR,
                label         VARCHAR,
                type          VARCHAR,
                content       VARCHAR,
                url           VARCHAR,
                created_by    VARCHAR,
                created_at    VARCHAR,
                updated_at    VARCHAR,
                updated_by    VARCHAR,
                confidence    VARCHAR,
                visibility    VARCHAR,
                provenance    VARCHAR,
                tags          VARCHAR,
                metadata      VARCHAR,
                priority      VARCHAR,
                deleted_at    VARCHAR
            )
        """,
        "ohm_edges": """
            CREATE TABLE IF NOT EXISTS {alias}.ohm_edges (
                id              VARCHAR,
                from_node       VARCHAR,
                to_node         VARCHAR,
                layer           VARCHAR,
                edge_type       VARCHAR,
                confidence      VARCHAR,
                probability     VARCHAR,
                urgency         VARCHAR,
                condition       VARCHAR,
                provenance      VARCHAR,
                created_by      VARCHAR,
                created_at      VARCHAR,
                updated_at      VARCHAR,
                updated_by      VARCHAR,
                challenge_of    VARCHAR,
                challenge_type  VARCHAR,
                metadata        VARCHAR,
                deleted_at      VARCHAR
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
    }

    for table_name, ddl in mirror_tables.items():
        try:
            conn.execute(ddl.format(alias=alias))
        except Exception:
            # Table may already exist — safe to ignore
            pass
