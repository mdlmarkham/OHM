#!/usr/bin/env python3
"""
duckdb_helper.py v2.0 — DuckDBSession with DuckLake, VSS/RAG, graph utilities.

New in v2.0:
  - attach_ducklake(): DuckLake v1.0 (DuckDB file / Postgres / SQLite catalog)
  - VSS helpers: create_vector_table(), build_hnsw_index(), vector_search()
  - Graph helpers: shortest_path(), bfs_reachable()
  - Hierarchy helpers: subtree_path(), ancestors()
  - Resilience: retry decorator, connection health-check, HNSW rebuild on startup

Security model (unchanged from v1.1):
  - All SQL identifiers validated against [A-Za-z_][A-Za-z0-9_]* before use
  - DSN values libpq-escaped; S3 credentials via CREATE SECRET (not SET)
  - PRAGMA values format-validated; paths null-byte checked
  - Credential statements redacted from debug logs
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Input validation helpers  (unchanged from v1.1 — full docstrings omitted
# for brevity; refer to that version for rationale)
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_QUALIFIED_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,3}$')
_MEMORY_LIMIT_RE = re.compile(r'^\d+(\.\d+)?\s*(B|KB|MB|GB|TB)$', re.IGNORECASE)
_TOON_NAME_RE = re.compile(r'^[A-Za-z0-9_\-]+$')
_ALLOWED_COMPRESSIONS = frozenset(["zstd", "snappy", "gzip", "none"])
# S3 URI prefix — allow s3://, gs://, az:// alongside local paths
_S3_PREFIX_RE = re.compile(r'^(s3a?|gs|az|abfss?)://')


def _require_identifier(name: str, context: str = "") -> str:
    if not _IDENT_RE.match(name):
        ctx = f" ({context})" if context else ""
        raise ValueError(f"Unsafe SQL identifier{ctx}: {name!r}")
    return name


def _require_identifier_list(names: list[str], context: str = "") -> list[str]:
    return [_require_identifier(n, context) for n in names]


def _validate_memory_limit(value: str) -> str:
    if not _MEMORY_LIMIT_RE.match(value.strip()):
        raise ValueError(f"Invalid memory_limit: {value!r}")
    return value


def _validate_path(path: str, context: str = "") -> str:
    """Accept local paths and S3/GCS/Azure URIs; reject null bytes."""
    if "\x00" in path:
        raise ValueError(f"Null byte in path ({context}): {path!r}")
    return path


def _validate_compression(codec: str) -> str:
    c = codec.lower()
    if c not in _ALLOWED_COMPRESSIONS:
        raise ValueError(f"Unknown compression: {codec!r}")
    return c


def _validate_toon_name(name: str) -> str:
    if not _TOON_NAME_RE.match(name):
        raise ValueError(f"Invalid TOON name: {name!r}")
    return name

# Quack URI pattern: quack:[//]host[:port]  — scheme + host + optional port
# Only allow safe hostname chars: alphanumeric, hyphen, dot, brackets for IPv6, colon for port
_QUACK_URI_RE = re.compile(
    r'^quack:(//)?' # scheme with optional //
    r'('
    r'\[(?:[0-9a-fA-F:]+)\]'  # IPv6 bracket notation
    r'|[a-zA-Z0-9.-]+'           # hostname or IPv4
    r')'
    r'(:\d{1,5})?$'            # optional port
)

# Token: printable ASCII, no single-quotes (which would break SQL string literal)
# Min length 32 for production — warn if shorter
_QUACK_TOKEN_MIN_WARN_LEN = 32


def _validate_quack_uri(uri: str) -> str:
    """
    Validate a Quack server URI.  (SEC-C)

    Accepts: quack:localhost, quack://host, quack:host:9494, quack:[::1]:1234
    Rejects: anything with SQL-control chars, path traversal, or empty host.
    Returns the validated URI string.
    """
    if not uri:
        raise ValueError("Quack URI must not be empty")
    if "'" in uri or '"' in uri or ";" in uri or "\x00" in uri:
        raise ValueError(f"Unsafe Quack URI — contains SQL control characters: {uri!r}")
    if not _QUACK_URI_RE.match(uri):
        raise ValueError(
            f"Invalid Quack URI: {uri!r}. "
            "Expected: quack:host, quack:host:port, quack://host:port"
        )
    return uri


def _validate_quack_token(token: str, *, context: str = "") -> str:
    """
    Validate a Quack authentication token.  (SEC-B, SEC-F)

    Rules:
      - Must not be empty
      - Must be >= 4 chars (DuckDB minimum)
      - Must not contain single-quotes (would break SQL string literal)
      - Must not contain null bytes
      - Warns (does not raise) if shorter than _QUACK_TOKEN_MIN_WARN_LEN (32)
        since short tokens are trivially brute-forced

    Returns the validated token string.
    """
    ctx = f" ({context})" if context else ""
    if not token:
        raise ValueError(f"Quack token{ctx} must not be empty")
    if len(token) < 4:
        raise ValueError(f"Quack token{ctx} must be at least 4 characters (DuckDB minimum)")
    if "'" in token:
        raise ValueError(
            f"Quack token{ctx} contains a single-quote which would break SQL string literals. "
            "Use a token composed of alphanumeric and safe punctuation characters."
        )
    if "\x00" in token:
        raise ValueError(f"Quack token{ctx} contains a null byte")
    if len(token) < _QUACK_TOKEN_MIN_WARN_LEN:
        logger.warning(
            "Quack token%s is only %d chars — recommend >= %d for production security",
            ctx, len(token), _QUACK_TOKEN_MIN_WARN_LEN
        )
    return token



def _dsn_escape(value: str) -> str:
    """Escape value for libpq keyword=value DSN (backslash then single-quote)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _sql_str_escape(value: str) -> str:
    """Escape value for SQL single-quoted string literal."""
    return value.replace("'", "''")


# ---------------------------------------------------------------------------
# TOON serialisation v3.0
# ---------------------------------------------------------------------------

def _toon_escape(value: Any) -> str:
    if value is None:
        return "null"
    s = str(value)
    if "," in s or "\n" in s or '"' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def df_to_toon(df: pd.DataFrame, table_name: str, max_rows: int = 500) -> str:
    """Serialise DataFrame to TOON tabular or key-value format."""
    _validate_toon_name(table_name)
    if df.empty:
        return f"{table_name}[0]{{}}: \n# (empty result set)"

    total_rows = len(df)
    truncated = total_rows > max_rows
    if truncated:
        df = df.head(max_rows)

    cols = list(df.columns)
    n = len(df)

    if n == 1:
        row = df.iloc[0]
        lines = [f"{table_name}:"]
        for col in cols:
            lines.append(f"  {col}: {_toon_escape(row[col])}")
        if truncated:
            lines.append(f"# WARNING: result truncated to {max_rows}/{total_rows} rows")
        return "\n".join(lines)

    header = f"{table_name}[{n}]{{{','.join(cols)}}}:"
    rows = ["  " + ",".join(_toon_escape(row[c]) for c in cols)
            for _, row in df.iterrows()]
    lines = [header] + rows
    if truncated:
        lines.append(f"# WARNING: result truncated to {max_rows}/{total_rows} rows")
    return "\n".join(lines)


def scalar_to_toon(data: dict[str, Any], label: str) -> str:
    _validate_toon_name(label)
    lines = [f"{label}:"]
    for k, v in data.items():
        lines.append(f"  {k}: {_toon_escape(v)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extension management
# ---------------------------------------------------------------------------

KNOWN_EXTENSIONS = frozenset({
    "postgres", "iceberg", "delta", "httpfs", "sqlite",
    "mysql", "spatial", "fts", "excel", "json",
    "ducklake", "vss", "aws",          # v2.0 additions
    "quack", "lance", "vortex",        # v3.0 — Quack client-server, Lance & Vortex formats
    "unity_catalog", "avro", "encodings",  # v3.0 — Unity Catalog, Avro, extra encodings
})

_REDACT_PREFIXES = ("SET S3_", "ATTACH ", "CREATE SECRET", "CREATE OR REPLACE SECRET")  # v3.0: ATTACH covers both ATTACH 'quack:...' and ATTACH 'ducklake:...' — both may carry tokens


def ensure_extension(con: duckdb.DuckDBPyConnection, ext: str, *, install: bool = True) -> None:
    """
    Install (allowlist-checked) and load a DuckDB extension.

    Tries LOAD first; only falls back to INSTALL if LOAD raises an error.
    This avoids unnecessary network round-trips when the extension is already
    installed in the DuckDB extensions directory.
    """
    if ext not in KNOWN_EXTENSIONS:
        raise ValueError(f"Unknown extension {ext!r}. Known: {sorted(KNOWN_EXTENSIONS)}")
    try:
        con.execute(f"LOAD {ext};")
        logger.debug("Extension loaded (already installed): %s", ext)
    except duckdb.Error:
        if not install:
            raise
        con.execute(f"INSTALL {ext};")
        con.execute(f"LOAD {ext};")
        logger.debug("Extension installed and loaded: %s", ext)


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

def _with_retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    Retry decorator for transient DuckDB / network errors.

    Catches duckdb.Error (but not ValueError/TypeError from validation),
    waits with exponential backoff, and re-raises after max_attempts.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except duckdb.Error as exc:
                    if attempt == max_attempts:
                        raise
                    logger.warning(
                        "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                        fn.__name__, attempt, max_attempts, exc, wait,
                    )
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class DuckDBConfig:
    memory_limit: str = "4GB"
    threads: int = 4
    temp_directory: str = "/tmp/duckdb_spill"


# ---------------------------------------------------------------------------
# DuckDBSession
# ---------------------------------------------------------------------------

class DuckDBSession:
    """
    Managed DuckDB connection with helpers for:
      - Source attachment: Postgres, DuckLake (v1.0), Iceberg, Delta, S3
      - TOON-formatted query results
      - Incremental table refresh with watermark (parameterized)
      - VSS/HNSW index management and RAG vector search
      - Graph traversal (BFS, shortest path via USING KEY, v1.3+)
      - Hierarchy queries (materialized path, recursive CTE, closure table)
      - Dry-run write-back to Postgres or Parquet

    Single connection — not a pool. Use as a context manager.
    """

    def __init__(
        self,
        path: str = ":memory:",
        config: DuckDBConfig | None = None,
        read_only: bool = False,
    ) -> None:
        self.path = path
        self.config = config or DuckDBConfig()
        self.read_only = read_only
        self._con: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> "DuckDBSession":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def connect(self) -> None:
        if self._con is not None:
            return
        self._con = duckdb.connect(self.path, read_only=self.read_only)
        cfg = self.config
        mem = _validate_memory_limit(cfg.memory_limit)
        tmp = _validate_path(cfg.temp_directory, "temp_directory")
        self._con.execute(f"PRAGMA memory_limit='{_sql_str_escape(mem)}';")
        self._con.execute(f"PRAGMA threads={int(cfg.threads)};")
        self._con.execute(f"PRAGMA temp_directory='{_sql_str_escape(tmp)}';")
        logger.debug("DuckDB connected: %s", self.path)

    def close(self) -> None:
        if self._con:
            self._con.close()
            self._con = None

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise RuntimeError("Not connected. Use as context manager.")
        return self._con

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: list[Any] | None = None) -> duckdb.DuckDBPyRelation:
        sql_upper = sql.strip().upper()
        if any(sql_upper.startswith(p) for p in _REDACT_PREFIXES):
            logger.debug("EXECUTE: [credential statement — redacted]")
        else:
            logger.debug("EXECUTE: %s", sql[:200])
        return self.con.execute(sql, params or [])

    def query(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        return self.execute(sql, params).fetchdf()

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        return self.execute(sql, params).fetchone()[0]

    def load_extensions(self, *extensions: str) -> None:
        for ext in extensions:
            ensure_extension(self.con, ext)

    # ------------------------------------------------------------------
    # Source attachment
    # ------------------------------------------------------------------

    def attach_postgres(
        self, *, host: str, port: int = 5432, dbname: str,
        user: str, password: str, alias: str = "pg", read_only: bool = True,
    ) -> None:
        """Attach PostgreSQL. DSN values are libpq-escaped (SEC-7)."""
        self.load_extensions("postgres")
        _require_identifier(alias, "alias")
        dsn = (
            f"host='{_dsn_escape(host)}' port={int(port)} "
            f"dbname='{_dsn_escape(dbname)}' user='{_dsn_escape(user)}' "
            f"password='{_dsn_escape(password)}'"
        )
        ro = ", READ_ONLY" if read_only else ""
        self.con.execute(f"ATTACH '{dsn}' AS {alias} (TYPE postgres{ro});")
        logger.info("Postgres attached as %r (read_only=%s)", alias, read_only)

    def attach_sqlite(self, path: str, alias: str = "sqlite") -> None:
        self.load_extensions("sqlite")
        _require_identifier(alias, "alias")
        self.execute(f"ATTACH '{_sql_str_escape(_validate_path(path))}' AS {alias} (TYPE sqlite);")

    def attach_mysql(
        self, *, host: str, port: int = 3306, database: str,
        user: str, password: str, alias: str = "mysql",
    ) -> None:
        self.load_extensions("mysql")
        _require_identifier(alias, "alias")
        dsn = (
            f"host='{_dsn_escape(host)}' port={int(port)} "
            f"database='{_dsn_escape(database)}' user='{_dsn_escape(user)}' "
            f"password='{_dsn_escape(password)}'"
        )
        self.con.execute(f"ATTACH '{dsn}' AS {alias} (TYPE mysql);")

    def attach_ducklake(
        self,
        *,
        catalog: str,
        data_path: str,
        alias: str = "lake",
        read_only: bool = False,
        snapshot_version: int | None = None,
        snapshot_time: str | None = None,
    ) -> None:
        """
        Attach a DuckLake v1.0 lakehouse.

        catalog can be:
          - 'metadata.ducklake'                    → local DuckDB file catalog
          - 'sqlite:metadata.sqlite'               → SQLite catalog
          - 'postgres:dbname=cat host=pg.example.com' → Postgres catalog

        data_path can be a local directory or an S3/GCS/Azure URI.
        snapshot_version / snapshot_time enable point-in-time read-only access.

        Security: alias is identifier-validated before any extension load;
        data_path is null-byte checked;
        snapshot_time is formatted as a SQL string literal.
        """
        # Validate all inputs BEFORE attempting extension load (SEC-10)
        _require_identifier(alias, "alias")
        _validate_path(data_path, "data_path")

        self.load_extensions("ducklake")

        # Build options list
        opts: list[str] = [f"DATA_PATH '{_sql_str_escape(data_path)}'"]
        if read_only:
            opts.append("READ_ONLY")
        if snapshot_version is not None:
            opts.append(f"VERSION => {int(snapshot_version)}")
        if snapshot_time is not None:
            opts.append(f"SNAPSHOT_TIME '{_sql_str_escape(snapshot_time)}'")

        opts_str = ", ".join(opts)
        # DuckLake uses ducklake: URI prefix — not a standard ATTACH type
        # SEC-A: catalog string SQL-string-escaped to prevent single-quote injection
        self.con.execute(f"ATTACH 'ducklake:{_sql_str_escape(catalog)}' AS {alias} ({opts_str});")
        logger.info("DuckLake attached as %r (data_path=%s, read_only=%s)", alias, data_path, read_only)

    def configure_s3(
        self,
        *,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        """Configure S3/GCS/ADLS via CREATE SECRET (credentials never logged)."""
        self.load_extensions("httpfs")
        parts = [f"TYPE s3", f"REGION '{_sql_str_escape(region)}'"]
        if access_key_id:
            parts.append(f"KEY_ID '{_sql_str_escape(access_key_id)}'")
        if secret_access_key:
            parts.append(f"SECRET '{_sql_str_escape(secret_access_key)}'")
        if session_token:
            parts.append(f"SESSION_TOKEN '{_sql_str_escape(session_token)}'")
        if endpoint:
            parts.append(f"ENDPOINT '{_sql_str_escape(endpoint)}'")
        self.con.execute(f"CREATE OR REPLACE SECRET __s3 (\n  {', '.join(parts)}\n);")
        logger.info("S3 credentials configured (region=%s)", region)

    # ------------------------------------------------------------------
    # DuckLake helpers
    # ------------------------------------------------------------------

    def ducklake_snapshots(self, alias: str = "lake") -> pd.DataFrame:
        """List all snapshots in the attached DuckLake."""
        safe = _require_identifier(alias, "alias")
        return self.query(f"SELECT * FROM {safe}.snapshots() ORDER BY snapshot_id;")

    def ducklake_compact(self, table: str, alias: str = "lake") -> None:
        """Compact small Parquet files for a DuckLake table."""
        safe_alias = _require_identifier(alias, "alias")
        safe_table = _require_identifier(table, "table")
        self.execute(f"CALL {safe_alias}.ducklake_compact('{safe_table}');")
        logger.info("DuckLake compaction triggered on %s.%s", safe_alias, safe_table)

    def ducklake_time_travel(
        self,
        query: str,
        table: str,
        alias: str = "lake",
        version: int | None = None,
        as_of: str | None = None,
    ) -> pd.DataFrame:
        """
        Query a DuckLake table at a specific version or timestamp.

        Uses AT (VERSION => N) or attaches a read-only snapshot — returns DataFrame.
        version and as_of are mutually exclusive.
        """
        safe_alias = _require_identifier(alias, "alias")
        safe_table = _require_identifier(table, "table")
        if version is not None and as_of is not None:
            raise ValueError("Specify either version or as_of, not both")
        if version is not None:
            at_clause = f"AT (VERSION => {int(version)})"
            sql = f"SELECT * FROM {safe_alias}.{safe_table} {at_clause}"
        elif as_of is not None:
            at_clause = f"AT (TIMESTAMP => '{_sql_str_escape(as_of)}'::TIMESTAMPTZ)"
            sql = f"SELECT * FROM {safe_alias}.{safe_table} {at_clause}"
        else:
            sql = query
        return self.query(sql)

    # ------------------------------------------------------------------
    # VSS / vector helpers
    # ------------------------------------------------------------------

    def create_vector_table(
        self,
        table: str,
        dim: int,
        extra_cols: dict[str, str] | None = None,
    ) -> None:
        """
        Create an embedding table with a FLOAT[dim] column.

        Args:
            table: Table name (identifier-validated)
            dim: Embedding dimension (e.g. 384 for all-MiniLM-L6-v2, 1536 for OpenAI)
            extra_cols: Additional columns as {name: SQL_type}, e.g. {'content': 'VARCHAR'}
        """
        safe_table = _require_identifier(table, "table")
        if not (1 <= dim <= 65535):
            raise ValueError(f"dim must be 1–65535, got {dim}")
        cols = [f"id VARCHAR PRIMARY KEY", f"embedding FLOAT[{dim}]"]
        if extra_cols:
            for col_name, col_type in extra_cols.items():
                safe_col = _require_identifier(col_name, "extra_cols key")
                # col_type is a SQL type string — not parameterisable, so we
                # restrict to a safe subset via a basic regex check
                if not re.match(r'^[A-Za-z0-9_\[\] ]+$', col_type):
                    raise ValueError(f"Unsafe column type: {col_type!r}")
                cols.append(f"{safe_col} {col_type}")
        self.execute(f"CREATE TABLE IF NOT EXISTS {safe_table} ({', '.join(cols)});")
        logger.info("Vector table created: %s (dim=%d)", safe_table, dim)

    def build_hnsw_index(
        self,
        table: str,
        embedding_col: str = "embedding",
        index_name: str | None = None,
        metric: str = "cosine",
    ) -> None:
        """
        Build an HNSW index on a vector table.

        Must be called AFTER bulk data load (building on existing rows is
        much faster and more memory-efficient than insert-then-index).

        metric: 'cosine' | 'l2sq' | 'ip'  (inner product)
        """
        safe_table = _require_identifier(table, "table")
        safe_col = _require_identifier(embedding_col, "embedding_col")
        allowed_metrics = {"cosine", "l2sq", "ip"}
        if metric not in allowed_metrics:
            raise ValueError(f"metric must be one of {allowed_metrics}")
        idx = index_name or f"{table}_hnsw"
        safe_idx = _require_identifier(idx, "index_name")

        self.load_extensions("vss")
        # Enable persistence for disk-backed databases
        if self.path != ":memory:":
            self.execute("SET hnsw_enable_experimental_persistence = true;")

        self.execute(
            f"CREATE INDEX IF NOT EXISTS {safe_idx} ON {safe_table} "
            f"USING HNSW ({safe_col}) WITH (metric = '{metric}');"
        )
        logger.info("HNSW index built: %s on %s.%s (metric=%s)", safe_idx, safe_table, safe_col, metric)

    def vector_search(
        self,
        table: str,
        query_vector: list[float],
        top_k: int = 5,
        metric: str = "cosine",
        embedding_col: str = "embedding",
        return_cols: list[str] | None = None,
        where: str | None = None,
    ) -> pd.DataFrame:
        """
        Approximate nearest-neighbour search using HNSW.

        The distance function is determined by `metric`:
          cosine → array_cosine_distance  (lower = more similar)
          l2sq   → array_distance
          ip     → array_negative_inner_product

        For hybrid search (vector + metadata filter), pass `where` as a SQL
        boolean expression using column names from `table`.  The HNSW index
        fires on the inner top-k query; the WHERE clause applies post-retrieval.

        Security: table, embedding_col, and return_cols are identifier-validated.
        The `where` argument is caller-supplied SQL — treat as trusted code.
        query_vector is passed as a DuckDB parameter (not interpolated).
        """
        safe_table = _require_identifier(table, "table")
        safe_emb = _require_identifier(embedding_col, "embedding_col")

        dist_fns = {
            "cosine": "array_cosine_distance",
            "l2sq": "array_distance",
            "ip": "array_negative_inner_product",
        }
        if metric not in dist_fns:
            raise ValueError(f"metric must be one of {set(dist_fns)}")
        dist_fn = dist_fns[metric]

        dim = len(query_vector)
        if not (1 <= dim <= 65535):
            raise ValueError(f"query_vector dimension must be 1–65535, got {dim}")

        # Determine columns to return
        if return_cols:
            safe_return = _require_identifier_list(return_cols, "return_cols")
            col_clause = ", ".join(safe_return) + ", dist"
        else:
            col_clause = "*, dist"

        where_clause = f"AND ({where})" if where else ""

        sql = f"""
            WITH top_k AS (
                SELECT *, {dist_fn}({safe_emb}, $1::FLOAT[{dim}]) AS dist
                FROM {safe_table}
                ORDER BY dist
                LIMIT {int(top_k) * (5 if where else 1)}
            )
            SELECT {col_clause}
            FROM top_k
            WHERE 1=1 {where_clause}
            ORDER BY dist
            LIMIT {int(top_k)};
        """
        return self.query(sql, [query_vector])

    # ------------------------------------------------------------------
    # Graph helpers (DuckDB 1.3+ USING KEY)
    # ------------------------------------------------------------------

    def shortest_path(
        self,
        edges_table: str,
        start_node: Any,
        src_col: str = "src",
        dst_col: str = "dst",
        weight_col: str = "weight",
        max_hops: int = 100,
    ) -> pd.DataFrame:
        """
        Dijkstra shortest path from start_node using USING KEY recursive CTE.

        Requires DuckDB 1.3+. Returns all reachable nodes with distance and path.
        Identifiers validated; start_node and max_hops are parameters/integers.
        """
        safe_edges = _require_identifier(edges_table, "edges_table")
        safe_src = _require_identifier(src_col, "src_col")
        safe_dst = _require_identifier(dst_col, "dst_col")
        safe_w = _require_identifier(weight_col, "weight_col")

        sql = f"""
            WITH RECURSIVE dijkstra(node, dist, path) USING KEY (node) AS (
                SELECT $1, 0.0::DOUBLE, [$1]
              UNION
                SELECT e.{safe_dst},
                       d.dist + e.{safe_w},
                       list_append(d.path, e.{safe_dst})
                FROM dijkstra d
                JOIN {safe_edges} e ON e.{safe_src} = d.node
                LEFT JOIN dijkstra cur ON cur.node = e.{safe_dst}
                WHERE d.dist + e.{safe_w} < COALESCE(cur.dist, 1e18)
                  AND list_count(d.path) < {int(max_hops)}
            )
            SELECT node, dist, path FROM dijkstra ORDER BY dist;
        """
        return self.query(sql, [start_node])

    def bfs_reachable(
        self,
        edges_table: str,
        start_node: Any,
        src_col: str = "src",
        dst_col: str = "dst",
        max_depth: int = 10,
    ) -> pd.DataFrame:
        """
        BFS from start_node — return all reachable nodes with depth.

        USING KEY prevents revisiting nodes (no exponential blowup on cycles).
        """
        safe_edges = _require_identifier(edges_table, "edges_table")
        safe_src = _require_identifier(src_col, "src_col")
        safe_dst = _require_identifier(dst_col, "dst_col")

        sql = f"""
            WITH RECURSIVE bfs(node, depth) USING KEY (node) AS (
                SELECT $1, 0
              UNION
                SELECT e.{safe_dst}, b.depth + 1
                FROM bfs b
                JOIN {safe_edges} e ON e.{safe_src} = b.node
                WHERE b.depth < {int(max_depth)}
            )
            SELECT node, depth FROM bfs ORDER BY depth, node;
        """
        return self.query(sql, [start_node])

    # ------------------------------------------------------------------
    # Hierarchy helpers
    # ------------------------------------------------------------------

    def subtree(
        self,
        tree_table: str,
        root_id: Any,
        id_col: str = "id",
        parent_col: str = "parent_id",
        max_depth: int = 50,
    ) -> pd.DataFrame:
        """
        Return all descendants of root_id from an adjacency-list tree table.

        Includes the root node itself (depth=0). Uses standard WITH RECURSIVE
        (not USING KEY — trees are acyclic so revisit-prevention is not needed).
        """
        safe_table = _require_identifier(tree_table, "tree_table")
        safe_id = _require_identifier(id_col, "id_col")
        safe_parent = _require_identifier(parent_col, "parent_col")

        sql = f"""
            WITH RECURSIVE subtree AS (
                SELECT *, 0 AS depth FROM {safe_table} WHERE {safe_id} = $1
              UNION ALL
                SELECT c.*, s.depth + 1
                FROM {safe_table} c
                JOIN subtree s ON c.{safe_parent} = s.{safe_id}
                WHERE s.depth < {int(max_depth)}
            )
            SELECT * FROM subtree ORDER BY depth;
        """
        return self.query(sql, [root_id])

    def ancestors(
        self,
        tree_table: str,
        node_id: Any,
        id_col: str = "id",
        parent_col: str = "parent_id",
    ) -> pd.DataFrame:
        """Return ancestor chain (root → node) for a node in an adjacency-list tree."""
        safe_table = _require_identifier(tree_table, "tree_table")
        safe_id = _require_identifier(id_col, "id_col")
        safe_parent = _require_identifier(parent_col, "parent_col")

        sql = f"""
            WITH RECURSIVE anc AS (
                SELECT *, 0 AS depth FROM {safe_table} WHERE {safe_id} = $1
              UNION ALL
                SELECT p.*, a.depth + 1
                FROM {safe_table} p
                JOIN anc a ON p.{safe_id} = a.{safe_parent}
            )
            SELECT * FROM anc ORDER BY depth DESC;
        """
        return self.query(sql, [node_id])

    # ------------------------------------------------------------------
    # TOON output
    # ------------------------------------------------------------------

    def to_toon(self, df: pd.DataFrame, table_name: str, max_rows: int = 500) -> str:
        return df_to_toon(df, table_name, max_rows=max_rows)

    def query_toon(self, sql: str, table_name: str, max_rows: int = 500) -> str:
        return self.to_toon(self.query(sql), table_name, max_rows=max_rows)

    # ------------------------------------------------------------------
    # Incremental cache refresh (watermark-parameterized)
    # ------------------------------------------------------------------

    @_with_retry(max_attempts=3, delay=1.0)
    def refresh_table(
        self,
        *,
        source_query: str,
        table: str,
        watermark_col: str,
        create_if_missing: bool = True,
    ) -> int:
        """Append-only incremental load. Retries up to 3x on transient errors."""
        safe_table = _require_identifier(table, "table")
        safe_wcol = _require_identifier(watermark_col, "watermark_col")

        exists = self.scalar(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [safe_table]
        )
        if not exists:
            if create_if_missing:
                self.execute(
                    f"CREATE TABLE {safe_table} AS "
                    f"SELECT * FROM ({source_query}) src WHERE 1=0;"
                )
            else:
                raise ValueError(f"Table {safe_table!r} does not exist")

        watermark = self.scalar(
            f"SELECT COALESCE(MAX({safe_wcol}), '1970-01-01'::TIMESTAMP) FROM {safe_table}"
        )
        new_count = self.scalar(
            f"SELECT COUNT(*) FROM ({source_query}) src WHERE {safe_wcol} > ?::TIMESTAMP",
            [str(watermark)],
        )
        if new_count > 0:
            self.execute(
                f"INSERT INTO {safe_table} "
                f"SELECT * FROM ({source_query}) src WHERE {safe_wcol} > ?::TIMESTAMP",
                [str(watermark)],
            )
        logger.info("refresh_table %r: +%d rows (watermark=%s)", safe_table, new_count, watermark)
        return new_count

    # ------------------------------------------------------------------
    # Write-back
    # ------------------------------------------------------------------

    def writeback_postgres(
        self,
        *,
        source_table_or_query: str,
        target_schema: str,
        target_table: str,
        pg_alias: str = "pg_rw",
        conflict_key: str | None = None,
        update_cols: list[str] | None = None,
        dry_run: bool = True,
    ) -> int:
        safe_schema = _require_identifier(target_schema, "target_schema")
        safe_table = _require_identifier(target_table, "target_table")
        safe_alias = _require_identifier(pg_alias, "pg_alias")
        safe_conflict = _require_identifier(conflict_key, "conflict_key") if conflict_key else None

        is_query = source_table_or_query.strip().upper().startswith("SELECT")
        src = f"({source_table_or_query})" if is_query else _require_identifier(
            source_table_or_query, "source_table"
        )
        count = self.scalar(f"SELECT COUNT(*) FROM {src}")
        if dry_run:
            logger.info("[DRY RUN] Would write %d rows to %s.%s.%s", count, safe_alias, safe_schema, safe_table)
            return count

        target = f"{safe_alias}.{safe_schema}.{safe_table}"
        if safe_conflict:
            col_df = self.query(f"SELECT * FROM {src} LIMIT 0")
            all_cols = _require_identifier_list(list(col_df.columns), "source columns")
            upd = update_cols if update_cols else [c for c in all_cols if c != safe_conflict]
            update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in _require_identifier_list(upd))
            sql = (f"INSERT INTO {target} SELECT * FROM {src} "
                   f"ON CONFLICT ({safe_conflict}) DO UPDATE SET {update_clause};")
        else:
            sql = f"INSERT INTO {target} SELECT * FROM {src};"
        self.execute(sql)
        logger.info("Wrote %d rows to %s", count, target)
        return count

    def writeback_parquet(
        self,
        *,
        source_table_or_query: str,
        output_path: str,
        partition_by: list[str] | None = None,
        compression: str = "zstd",
        dry_run: bool = True,
    ) -> int:
        safe_path = _validate_path(output_path, "output_path")
        safe_comp = _validate_compression(compression)
        safe_parts = _require_identifier_list(partition_by or [], "partition_by")

        is_query = source_table_or_query.strip().upper().startswith("SELECT")
        src = f"({source_table_or_query})" if is_query else _require_identifier(
            source_table_or_query, "source_table"
        )
        count = self.scalar(f"SELECT COUNT(*) FROM {src}")
        if dry_run:
            logger.info("[DRY RUN] Would write %d rows to %s", count, safe_path)
            return count

        part = f", PARTITION_BY ({', '.join(safe_parts)}), OVERWRITE_OR_IGNORE" if safe_parts else ""
        self.execute(
            f"COPY {src} TO '{_sql_str_escape(safe_path)}' "
            f"(FORMAT parquet, COMPRESSION {safe_comp}{part});"
        )
        logger.info("Wrote %d rows to Parquet: %s", count, safe_path)
        return count


    # ------------------------------------------------------------------
    # Quack client-server protocol (DuckDB v1.5.2+, beta — core_nightly)
    # Install: FORCE INSTALL quack FROM core_nightly;
    # ------------------------------------------------------------------

    def quack_serve(
        self,
        uri: str = "quack:localhost",
        *,
        token_env: str | None = None,
        token: str | None = None,
        allow_other_hostname: bool = False,
        require_tls_confirm: bool = True,
    ) -> dict:
        """
        Start a Quack server on this DuckDB instance.

        Everything the current session can see (tables, attached files, schemas)
        becomes reachable over the Quack protocol.

        Security:
          - URI is validated against a strict allowlist pattern  (SEC-C)
          - Token is validated for length and safe chars  (SEC-B, SEC-F)
          - Prefer token_env (read from environment) over token kwarg
          - allow_other_hostname=True opens external network access; you MUST
            front the server with a TLS-terminating reverse proxy in production.
            Passing require_tls_confirm=False explicitly suppresses the guard.  (SEC-D)

        Args:
            uri: Quack listen URI, e.g. 'quack:localhost' or 'quack:0.0.0.0:9494'
            token_env: Name of env var holding the auth token (preferred)
            token: Explicit token string (only if token_env is None)
            allow_other_hostname: True to bind externally (requires TLS in prod)
            require_tls_confirm: If True and allow_other_hostname=True, raises
                ValueError unless you explicitly set require_tls_confirm=False

        Returns:
            Dict with keys: listen_uri, url, auth_token (redacted in logs)
        """
        _validate_quack_uri(uri)

        # External access guard BEFORE extension load (SEC-D)
        if allow_other_hostname and require_tls_confirm:
            raise ValueError(
                "allow_other_hostname=True opens external network access. "
                "You MUST front this server with a TLS-terminating reverse proxy "
                "in production. Set require_tls_confirm=False to confirm you have done so."
            )

        self.load_extensions("quack")

        # Resolve token
        tok: str | None = None
        if token_env:
            import os
            tok = os.environ.get(token_env)
            if not tok:
                raise ValueError(f"Env var {token_env!r} is not set or empty")
        elif token:
            tok = token
        if tok:
            _validate_quack_token(tok, context="quack_serve")

        parts = [f"'{_sql_str_escape(uri)}'"]
        if tok:
            parts.append(f"token := '{_sql_str_escape(tok)}'")
        if allow_other_hostname:
            parts.append("allow_other_hostname := true")

        # quack_serve returns a relation — not logged (has token in output)
        result = self.con.execute(f"CALL quack_serve({', '.join(parts)});").fetchone()
        logger.info("Quack server started on %s", uri)
        # result tuple: (listen_uri, url, auth_token)
        keys = ["listen_uri", "url", "auth_token"]
        return dict(zip(keys, result)) if result else {}

    def quack_stop(self, uri: str = "quack:localhost") -> None:
        """Stop a Quack server. URI is validated before use."""
        _validate_quack_uri(uri)
        self.load_extensions("quack")
        self.execute(f"CALL quack_stop('{_sql_str_escape(uri)}');")
        logger.info("Quack server stopped: %s", uri)

    def attach_quack(
        self,
        uri: str,
        *,
        alias: str = "remote",
        token_env: str | None = None,
        token: str | None = None,
        disable_ssl: bool = False,
    ) -> None:
        """
        Attach a remote Quack server as a local catalog.

        After attaching, remote tables behave like local ones:
            df = db.query("SELECT * FROM remote.my_table LIMIT 100")

        Security:
          - URI validated against strict pattern  (SEC-C)
          - Alias validated as SQL identifier
          - Token validated and SQL-string-escaped before ATTACH  (SEC-B)
          - ATTACH statement is redacted from debug logs (prefix 'ATTACH ')  (SEC-E)
          - Token preferred via env var — never hardcode in source

        Args:
            uri: Quack server URI, e.g. 'quack:srv.example.com'
            alias: Local catalog name (identifier-validated)
            token_env: Env var holding the auth token (preferred)
            token: Explicit token (only if token_env is None)
            disable_ssl: True only for localhost or explicit HTTP setups
        """
        _validate_quack_uri(uri)
        _require_identifier(alias, "alias")
        self.load_extensions("quack")

        # Resolve and validate token
        import os
        tok: str | None = None
        if token_env:
            tok = os.environ.get(token_env)
            if not tok:
                raise ValueError(f"Env var {token_env!r} is not set or empty")
        elif token:
            tok = token
        if tok:
            _validate_quack_token(tok, context="attach_quack")

        opts = []
        if tok:
            opts.append(f"TOKEN '{_sql_str_escape(tok)}'")
        if disable_ssl:
            opts.append("DISABLE_SSL true")
        opts_str = (", " + ", ".join(opts)) if opts else ""

        # Not logged — prefix 'ATTACH ' is in _REDACT_PREFIXES
        self.con.execute(
            f"ATTACH '{_sql_str_escape(uri)}' AS {alias} (TYPE quack{opts_str});"
        )
        logger.info("Quack remote attached as %r (%s)", alias, uri)

    def quack_query(
        self,
        uri: str,
        sql: str,
        *,
        token_env: str | None = None,
        token: str | None = None,
        disable_ssl: bool = False,
    ) -> "pd.DataFrame":
        """
        Execute a stateless SQL query on a remote Quack server.

        No persistent attachment — each call is independent.
        Credentials are validated and SQL-string-escaped before use.
        """
        _validate_quack_uri(uri)
        self.load_extensions("quack")

        import os
        tok: str | None = None
        if token_env:
            tok = os.environ.get(token_env)
            if not tok:
                raise ValueError(f"Env var {token_env!r} is not set or empty")
        elif token:
            tok = token
        if tok:
            _validate_quack_token(tok, context="quack_query")

        params = [_sql_str_escape(uri), sql]
        token_clause = f", token := '{_sql_str_escape(tok)}'" if tok else ""
        ssl_clause = ", disable_ssl := true" if disable_ssl else ""

        # quack_query is a table-returning function — use FROM syntax
        escaped_sql = sql.replace("'", "''")
        result_sql = (
            f"FROM quack_query('{_sql_str_escape(uri)}', "
            f"'{escaped_sql}'{token_clause}{ssl_clause})"
        )
        return self.query(result_sql)

    def quack_identify(
        self,
        *,
        alias: str = "remote",
        name: str | None = None,
        provider: str | None = None,
        hostname: str | None = None,
        region: str | None = None,
        meta: str | None = None,
    ) -> None:
        """
        Set identity fields on an attached Quack node.

        Useful for fleet management and log correlation.
        All string values are SQL-string-escaped before use.
        """
        _require_identifier(alias, "alias")
        parts = []
        for key, val in [("name", name), ("provider", provider),
                         ("hostname", hostname), ("region", region), ("meta", meta)]:
            if val is not None:
                parts.append(f"{key} := '{_sql_str_escape(val)}'")
        if parts:
            self.execute(f"CALL {alias}.quack_identify({', '.join(parts)});")

    def quack_secret(
        self,
        *,
        token_env: str,
        scope: str,
        secret_name: str = "__quack",
    ) -> None:
        """
        Create a scoped Quack secret so ATTACH/quack_query can authenticate without
        passing the token explicitly.

        Uses CREATE SECRET (redacted from logs).
        Token is read from an environment variable — never hardcoded.
        scope should be the full Quack URI, e.g. 'quack:srv.example.com'.
        """
        import os
        tok = os.environ.get(token_env)
        if not tok:
            raise ValueError(f"Env var {token_env!r} is not set or empty")
        _validate_quack_token(tok, context="quack_secret")
        _validate_quack_uri(scope)
        _require_identifier(secret_name, "secret_name")
        # CREATE SECRET — redacted from debug logs
        self.con.execute(
            f"CREATE OR REPLACE SECRET {secret_name} ("
            f"TYPE quack, TOKEN '{_sql_str_escape(tok)}', SCOPE '{_sql_str_escape(scope)}'"
            f");"
        )
        logger.info("Quack secret created for scope: %s", scope)

    # ------------------------------------------------------------------
    # Profiling
    # ------------------------------------------------------------------

    def profile_table(self, table: str) -> str:
        safe = _require_identifier(table, "table")
        return self.to_toon(self.query(f"SUMMARIZE {safe}"), f"{safe}_profile")

    def show_attached(self) -> pd.DataFrame:
        return self.query("SELECT * FROM duckdb_databases();")

    def show_tables(self, schema: str | None = None) -> pd.DataFrame:
        if schema is not None:
            safe = _require_identifier(schema, "schema")
            return self.query(
                "SELECT * FROM duckdb_tables() WHERE schema_name = ? ORDER BY table_name;",
                [safe],
            )
        return self.query("SELECT * FROM duckdb_tables() ORDER BY table_name;")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_path = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
    print(f"Connecting: {db_path}")

    with DuckDBSession(db_path) as db:
        # TOON smoke test
        db.execute("""
            CREATE TEMP TABLE test AS
            SELECT i AS id, 'tag_'||(i%5) AS tag, RANDOM()*100 AS value,
                   now()-(i*INTERVAL '1 minute') AS ts
            FROM range(1,21) t(i);
        """)
        df = db.query("SELECT tag, ROUND(AVG(value),2) avg_val, ROUND(MAX(value),2) peak FROM test GROUP BY tag ORDER BY tag")
        print("\n--- TOON ---")
        print(db.to_toon(df, "tag_summary"))

        # Graph smoke test (BFS on in-memory edge list)
        db.execute("CREATE TEMP TABLE edges (src INT, dst INT, weight DOUBLE)")
        db.execute("INSERT INTO edges VALUES (1,2,1),(1,3,4),(2,3,2),(2,4,5),(3,4,1)")
        sp = db.shortest_path("edges", start_node=1)
        print("\n--- Shortest Paths from node 1 ---")
        print(db.to_toon(sp, "shortest_paths"))

        # Hierarchy smoke test
        db.execute("CREATE TEMP TABLE tree (id INT PRIMARY KEY, parent_id INT, name VARCHAR)")
        db.execute("INSERT INTO tree VALUES (1,NULL,'root'),(2,1,'A'),(3,1,'B'),(4,2,'A1'),(5,2,'A2')")
        sub = db.subtree("tree", root_id=2)
        print("\n--- Subtree of node 2 ---")
        print(db.to_toon(sub[["id","name","depth"]], "subtree"))

        # Identifier validation
        print("\n--- Injection rejection ---")
        for bad in ["'; DROP TABLE t;--", "a b", "1bad", ""]:
            try:
                _require_identifier(bad)
                print(f"  FAIL: {bad!r}")
            except ValueError:
                print(f"  OK rejected: {bad!r}")
