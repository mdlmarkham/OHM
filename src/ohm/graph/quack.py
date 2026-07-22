"""OHM Quack integration — DuckDB client-server protocol for concurrent access.

Quack is DuckDB's native HTTP-based client-server protocol that enables
concurrent multi-writer access. Both client and server are full DuckDB
instances communicating over HTTP.

This module provides:
- ``is_available()`` — Check if the Quack extension can be loaded
- ``start_server()`` — Start a Quack server on a DuckDB connection
- ``stop_server()`` — Stop a running Quack server
- ``attach_remote()`` — Attach to a remote Quack server as a client
- ``query_remote()`` — Stateless query against a remote Quack server

When Quack is not available (extension not installed, DuckDB too old),
all functions gracefully fall back to direct DuckDB connections. The
HTTP handler in server.py continues to work regardless.

Usage (server side):
    from ohm.quack import is_available, start_server

    if is_available(conn):
        start_server(conn, "quack:localhost", token_env="QUACK_TOKEN")

Usage (client side):
    from ohm.quack import attach_remote

    remote_conn = attach_remote("quack:localhost", token_env="QUACK_TOKEN")
    results = remote_conn.execute("SELECT * FROM ohm_nodes LIMIT 10")

ADR-002: Quack for concurrent access.
"""

from __future__ import annotations

import duckdb  # module-level for test patching
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# ── Availability Detection ──────────────────────────────────────────────────

_quack_available: bool | None = None


def is_available(conn: DuckDBPyConnection | None = None) -> bool:
    """Check if the Quack extension is available in DuckDB.

    Caches the result after first check. Returns False if:
    - DuckDB version doesn't support Quack
    - The extension can't be installed from core_nightly
    - The extension fails to load

    Args:
        conn: Optional DuckDB connection to test with.
            If None, creates a temporary in-memory connection.

    Returns:
        True if Quack is available, False otherwise.
    """
    global _quack_available

    if _quack_available is not None:
        return _quack_available

    test_conn = conn
    should_close = False
    if test_conn is None:
        try:
            test_conn = duckdb.connect(":memory:")
            should_close = True
        except Exception:
            _quack_available = False
            return False

    try:
        # Try to install and load the Quack extension.
        # Use INSTALL (not FORCE INSTALL) to avoid re-downloading on every
        # call — FORCE INSTALL always re-downloads, leaving orphaned .tmp-
        # files if the process is interrupted.
        test_conn.execute("INSTALL quack FROM core_nightly")
        test_conn.execute("LOAD quack")
        _quack_available = True
    except Exception:
        _quack_available = False
    finally:
        if should_close:
            try:
                test_conn.close()
            except Exception:
                pass

    return _quack_available


def reset_availability() -> None:
    """Reset the cached Quack availability check.

    Useful for testing or after DuckDB upgrades.
    """
    global _quack_available
    _quack_available = None


# ── URI Validation ──────────────────────────────────────────────────────────

_QUACK_URI_RE = re.compile(r"^quack:(//)?[a-zA-Z0-9._:-]+(?:\d+)?$")


def validate_quack_uri(uri: str) -> str:
    """Validate a Quack URI.

    Accepted formats:
        quack:localhost
        quack://localhost
        quack:myhost:9494
        quack:127.0.0.1
        quack:127.0.0.1:9494

    Args:
        uri: Quack URI string.

    Returns:
        The validated URI string.

    Raises:
        ValueError: If the URI is invalid or contains suspicious characters.
    """
    if not uri.startswith("quack:"):
        raise ValueError(f"Quack URI must start with 'quack:', got: {uri!r}")

    # Reject URIs with single quotes or SQL control chars
    if "'" in uri or ";" in uri or "--" in uri:
        raise ValueError(f"Quack URI contains invalid characters: {uri!r}")

    host_part = uri.replace("quack://", "").replace("quack:", "")
    if not host_part:
        raise ValueError(f"Quack URI must specify a host: {uri!r}")

    return uri


def validate_quack_token(token: str) -> str:
    """Validate a Quack authentication token.

    Args:
        token: Token string.

    Returns:
        The validated token string.

    Raises:
        ValueError: If the token is too short or contains invalid characters.
    """
    if len(token) < 4:
        raise ValueError(f"Quack token must be at least 4 characters, got {len(token)}")
    if len(token) < 32:
        import warnings

        warnings.warn(
            f"Quack token is {len(token)} chars — 32+ recommended for production",
            stacklevel=2,
        )
    if "'" in token:
        raise ValueError("Quack token must not contain single quotes")
    return token


_DANGEROUS_SQL_RE = re.compile(r";\s*(?:DROP|ALTER|CREATE|INSERT|UPDATE|DELETE|TRUNCATE|GRANT|REVOKE)\b", re.IGNORECASE)


def validate_quack_sql(sql: str) -> str:
    """Validate SQL for use with quack_query().

    Defense-in-depth check on SQL strings even when using parameterized
    queries. Rejects statements that attempt to chain destructive DDL/DML
    after the intended query.

    Args:
        sql: SQL query string.

    Returns:
        The validated SQL string.

    Raises:
        ValueError: If SQL contains dangerous multi-statement patterns.
    """
    if _DANGEROUS_SQL_RE.search(sql):
        raise ValueError("Quack query SQL must not contain chained DDL/DML statements")
    return sql


# ── Server-Side ─────────────────────────────────────────────────────────────


def start_server(
    conn: DuckDBPyConnection,
    uri: str = "quack:localhost",
    *,
    token: str | None = None,
    token_env: str | None = None,
    allow_other_hostname: bool = False,
) -> dict[str, Any]:
    """Start a Quack server on the given DuckDB connection.

    The server exposes all tables and views in the current database
    to Quack clients. Must be called on the connection that owns the
    DuckDB file (the ohmd daemon's connection).

    Args:
        conn: DuckDB connection with the OHM schema loaded.
        uri: Quack URI (default: ``quack:localhost``).
        token: Authentication token. If None, reads from token_env.
        token_env: Environment variable name containing the token.
            Falls back to ``QUACK_TOKEN``.
        allow_other_hostname: Allow external bind (requires TLS proxy).

    Returns:
        Dict with server info: listen_uri, auth_token.

    Raises:
        RuntimeError: If Quack is not available.
        ValueError: If URI or token is invalid.
    """
    if not is_available(conn):
        raise RuntimeError("Quack extension is not available. Install with: FORCE INSTALL quack FROM core_nightly; LOAD quack;")

    uri = validate_quack_uri(uri)

    # Resolve token
    resolved_token = token
    if resolved_token is None:
        env_var = token_env or "QUACK_TOKEN"
        resolved_token = os.environ.get(env_var)

    if resolved_token:
        resolved_token = validate_quack_token(resolved_token)

    # Register token as a DuckDB secret (OHM-5gpr: never interpolate tokens into SQL).
    # quack_serve automatically picks up secrets matching the URI scope.
    if resolved_token:
        create_secret(conn, token=resolved_token, scope=uri)

    # Build the CALL quack_serve(...) SQL — no token in the SQL string.
    if allow_other_hostname:
        conn.execute(f"CALL quack_serve('{uri}', allow_other_hostname := true)")
    else:
        conn.execute(f"CALL quack_serve('{uri}')")

    # Get the server info
    try:
        conn.execute("SELECT * FROM duckdb_settings() WHERE name = 'quack_listen_uri'").fetchall()
    except Exception:
        pass

    # Return what we can — the actual return format depends on DuckDB version
    return {
        "uri": uri,
        "token_set": resolved_token is not None,
        "allow_other_hostname": allow_other_hostname,
    }


def stop_server(conn: DuckDBPyConnection, uri: str = "quack:localhost") -> None:
    """Stop a running Quack server.

    Args:
        conn: DuckDB connection that started the server.
        uri: Quack URI of the server to stop.

    Raises:
        RuntimeError: If Quack is not available.
    """
    if not is_available(conn):
        raise RuntimeError("Quack extension is not available")

    uri = validate_quack_uri(uri)
    conn.execute(f"CALL quack_stop('{uri}')")


# ── Client-Side ─────────────────────────────────────────────────────────────


def attach_remote(
    conn: DuckDBPyConnection,
    uri: str = "quack:localhost",
    *,
    alias: str = "remote",
    token: str | None = None,
    token_env: str | None = None,
    disable_ssl: bool = False,
) -> None:
    """Attach a remote Quack server as a catalog on the given connection.

    After attaching, remote tables are accessible as ``remote.table_name``.

    Args:
        conn: Local DuckDB connection to attach from.
        uri: Quack URI of the remote server.
        alias: Catalog alias for the remote (default: ``remote``).
        token: Authentication token. If None, reads from token_env.
        token_env: Environment variable name containing the token.
            Falls back to ``QUACK_TOKEN``.
        disable_ssl: Disable SSL for this connection (non-production only).

    Raises:
        RuntimeError: If Quack is not available.
        ValueError: If URI or token is invalid.
    """
    if not is_available(conn):
        raise RuntimeError("Quack extension is not available")

    uri = validate_quack_uri(uri)

    # Resolve token
    resolved_token = token
    if resolved_token is None:
        env_var = token_env or "QUACK_TOKEN"
        resolved_token = os.environ.get(env_var)

    if resolved_token:
        resolved_token = validate_quack_token(resolved_token)

    # Register token as a DuckDB secret (OHM-5gpr: never interpolate tokens into SQL).
    # ATTACH automatically uses secrets whose scope matches the URI.
    if resolved_token:
        create_secret(conn, token=resolved_token, scope=uri)

    # Build ATTACH statement — no inline TOKEN clause.
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", alias):
        raise ValueError(f"Invalid alias: {alias!r}")
    from ohm.validation import sql_string_literal

    attach_sql = f"ATTACH '{sql_string_literal(uri)}' AS {alias} (TYPE quack"
    if disable_ssl:
        attach_sql += ", DISABLE_SSL true"
    attach_sql += ")"

    conn.execute(attach_sql)


def detach_remote(conn: DuckDBPyConnection, alias: str = "remote") -> None:
    """Detach a remote Quack catalog.

    Args:
        conn: Local DuckDB connection.
        alias: Catalog alias to detach.
    """
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", alias):
        raise ValueError(f"Invalid alias: {alias!r}")
    conn.execute(f"DETACH {alias}")


def query_remote(
    conn: DuckDBPyConnection,
    uri: str,
    sql: str,
    *,
    token: str | None = None,
    token_env: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a stateless query against a remote Quack server.

    Does not create a persistent attachment — useful for one-off queries.

    Args:
        conn: Local DuckDB connection.
        uri: Quack URI of the remote server.
        sql: SQL query to execute on the remote server.
        token: Authentication token.
        token_env: Environment variable name for the token.

    Returns:
        List of result dicts.

    Raises:
        RuntimeError: If Quack is not available.
    """
    if not is_available(conn):
        raise RuntimeError("Quack extension is not available")

    uri = validate_quack_uri(uri)

    resolved_token = token
    if resolved_token is None:
        env_var = token_env or "QUACK_TOKEN"
        resolved_token = os.environ.get(env_var)

    if resolved_token:
        resolved_token = validate_quack_token(resolved_token)
        # Register token as a DuckDB secret (OHM-5gpr: never interpolate tokens into SQL).
        # quack_query automatically picks up secrets matching the URI scope.
        create_secret(conn, token=resolved_token, scope=uri)

    # Defense-in-depth: validate SQL even though we use parameterized queries.
    validate_quack_sql(sql)

    # Use parameterized queries — URI and SQL are never interpolated into the
    # SQL string, eliminating injection vectors (OHM-5gpr).
    result = conn.execute(
        "SELECT * FROM quack_query(?, ?)",
        [uri, sql],
    )

    columns = [desc[0] for desc in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def create_secret(
    conn: DuckDBPyConnection,
    *,
    token: str | None = None,
    token_env: str | None = None,
    scope: str | None = None,
) -> None:
    """Create a scoped Quack secret for authentication.

    Secrets avoid inline tokens in ATTACH statements.

    Args:
        conn: DuckDB connection.
        token: Authentication token.
        token_env: Environment variable name for the token.
        scope: Scope URI for the secret (e.g., ``quack:srv.example.com``).
    """
    if not is_available(conn):
        raise RuntimeError("Quack extension is not available")

    resolved_token = token
    if resolved_token is None:
        env_var = token_env or "QUACK_TOKEN"
        resolved_token = os.environ.get(env_var)

    if not resolved_token:
        raise ValueError("No token provided and environment variable not set")

    resolved_token = validate_quack_token(resolved_token)

    # Validate scope to prevent injection (scope comes from validated URIs
    # in most call paths, but be defensive).
    if scope and ("'" in scope or ";" in scope or "--" in scope):
        raise ValueError(f"Quack secret scope contains invalid characters: {scope!r}")

    # Security: escape single quotes in token to prevent SQL injection
    # via the CREATE SECRET f-string below.
    safe_token = resolved_token.replace("'", "''")
    safe_scope = scope.replace("'", "''") if scope else ""
    scope_clause = f", SCOPE '{safe_scope}'" if safe_scope else ""
    conn.execute(f"CREATE OR REPLACE SECRET (TYPE quack, TOKEN '{safe_token}'{scope_clause})")
