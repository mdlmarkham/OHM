"""OHM database connection management.

Handles DuckDB connection lifecycle, schema initialization,
and connection configuration.
"""

from __future__ import annotations

import os
import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


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
    except duckdb.IOException as e:
        # Check if this is a WAL corruption error
        error_msg = str(e)
        if "WAL" in error_msg or "wal" in error_msg.lower():
            # WAL corruption — delete WAL and retry
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


def close(conn: "duckdb.DuckDBPyConnection") -> None:
    """Close a DuckDB connection safely."""
    try:
        conn.close()
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
        conn.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded = true AND extension_name = 'ducklake'").fetchone()
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
                priority      VARCHAR
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
                metadata        VARCHAR
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
                source_url  VARCHAR
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
