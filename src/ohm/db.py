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


def close(conn: "duckdb.DuckDBPyConnection") -> None:
    """Close a DuckDB connection safely."""
    try:
        conn.close()
    except Exception:
        pass
