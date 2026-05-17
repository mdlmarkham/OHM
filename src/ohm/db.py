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

    Args:
        db_path: Path to the DuckDB file. If None, uses the default path.

    Returns:
        An active DuckDB connection with the OHM schema initialized.
    """
    import duckdb

    if db_path is None:
        db_path = get_default_db_path()

    conn = duckdb.connect(str(db_path))

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
    """
    extensions = ["json"]
    for ext in extensions:
        try:
            conn.execute(f"INSTALL {ext}; LOAD {ext};")
        except Exception:
            pass  # Extension may already be installed

    # Try to load Quack extension (optional, for concurrent access)
    try:
        conn.execute("FORCE INSTALL quack FROM core_nightly; LOAD quack;")
    except Exception:
        pass  # Quack not available — fall back to single-writer mode


def close(conn: "duckdb.DuckDBPyConnection") -> None:
    """Close a DuckDB connection safely."""
    try:
        conn.close()
    except Exception:
        pass
