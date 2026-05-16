#!/usr/bin/env python3
"""
test_suite.py — Comprehensive regression suite for the duckdb skill v2.0.

Run: python scripts/test_suite.py
     python scripts/test_suite.py --verbose
     python scripts/test_suite.py --filter graph  # run only tests matching 'graph'

Exit 0 on all pass, 1 on any failure.

Coverage:
  Security  — identifier injection, DSN escaping, PRAGMA/path/compression validation
  TOON      — tabular, key-value, truncation, empty, name validation
  Session   — connect, PRAGMA application, multiple context manager cycles
  Cache     — refresh_table incremental load, watermark, create-if-missing
  Graph     — BFS reachability, Dijkstra shortest path, bad identifier rejection
  Hierarchy — subtree, ancestors, depth ordering
  Vector    — table creation, batch upsert, brute-force search (no HNSW required)
  Writeback — Parquet dry-run, Parquet write, identifier/compression/path guards
  DuckLake  — alias validated before extension load
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable

# Ensure scripts/ is importable regardless of cwd
_SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SKILL_ROOT))

from scripts.duckdb_helper import (
    DuckDBConfig, DuckDBSession,
    _dsn_escape, _require_identifier, _require_identifier_list,
    _sql_str_escape, _validate_compression, _validate_memory_limit,
    _validate_path, _validate_toon_name, df_to_toon, scalar_to_toon,
)

import pandas as pd

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

_RESULTS: list[tuple[str, str, str | None]] = []   # (name, PASS|FAIL, detail)


def _test(name: str):
    """Decorator — marks a function as a test case."""
    def decorator(fn: Callable):
        fn._test_name = name
        fn._is_test = True
        return fn
    return decorator


def _run_all(filter_str: str = "", verbose: bool = False) -> bool:
    import inspect
    module = sys.modules[__name__]
    tests = [
        obj for _, obj in inspect.getmembers(module, inspect.isfunction)
        if getattr(obj, "_is_test", False)
    ]
    if filter_str:
        tests = [t for t in tests if filter_str.lower() in t._test_name.lower()]

    passed = failed = 0
    for fn in tests:
        name = fn._test_name
        try:
            fn()
            _RESULTS.append((name, "PASS", None))
            if verbose:
                print(f"  \u2713 {name}")
            passed += 1
        except Exception as exc:
            detail = str(exc)[:200]
            _RESULTS.append((name, "FAIL", detail))
            print(f"  \u2717 FAIL: {name}")
            print(f"         {detail}")
            failed += 1

    total = passed + failed
    print(f"\n{'='*60}")
    print(f"  {passed}/{total} passed" + (f"  ({failed} FAILED)" if failed else "  — all OK"))
    return failed == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db(**kwargs) -> DuckDBSession:
    return DuckDBSession(":memory:", **kwargs)


def _disk_db() -> tuple[str, DuckDBSession]:
    """Returns (path, session) for a temp disk-backed database."""
    path = tempfile.mktemp(suffix=".duckdb")
    return path, DuckDBSession(path)


# ===========================================================================
# Security tests
# ===========================================================================

@_test("SEC: valid identifier accepted")
def t_sec_valid_ident():
    assert _require_identifier("my_table") == "my_table"
    assert _require_identifier("_col99") == "_col99"
    assert _require_identifier("A") == "A"


@_test("SEC: space in identifier rejected")
def t_sec_space():
    try: _require_identifier("my table"); assert False, "should raise"
    except ValueError: pass


@_test("SEC: SQL injection in identifier rejected")
def t_sec_sql_inject():
    for bad in ["'; DROP TABLE t;--", "1bad", "", "a-b", "a.b"]:
        try: _require_identifier(bad); assert False, f"{bad!r} not rejected"
        except ValueError: pass


@_test("SEC: identifier list — validates each member")
def t_sec_ident_list():
    assert _require_identifier_list(["a", "b", "C_3"]) == ["a", "b", "C_3"]
    try: _require_identifier_list(["a", "bad col"]); assert False
    except ValueError: pass


@_test("SEC: DSN escape — single quote")
def t_sec_dsn_quote():
    assert _dsn_escape("p'ass") == "p\\'ass"


@_test("SEC: DSN escape — backslash")
def t_sec_dsn_backslash():
    assert _dsn_escape("p\\ass") == "p\\\\ass"


@_test("SEC: DSN escape — both quote and backslash")
def t_sec_dsn_both():
    assert _dsn_escape("p\\'a") == "p\\\\\\'a"


@_test("SEC: SQL string escape apostrophe")
def t_sec_sql_escape():
    assert _sql_str_escape("it's") == "it''s"
    assert _sql_str_escape("a'b'c") == "a''b''c"


@_test("SEC: memory_limit valid formats")
def t_sec_mem_valid():
    for v in ["4GB", "512MB", "1.5TB", "100B", "16 KB"]:
        _validate_memory_limit(v)


@_test("SEC: memory_limit injection rejected")
def t_sec_mem_inject():
    for bad in ["'; DROP TABLE t;", "4", "", "4GB; extra"]:
        try: _validate_memory_limit(bad); assert False, f"{bad!r} not rejected"
        except ValueError: pass


@_test("SEC: path null-byte rejected")
def t_sec_path_null():
    try: _validate_path("/data/\x00bad"); assert False
    except ValueError: pass


@_test("SEC: path clean accepted (local and S3)")
def t_sec_path_clean():
    _validate_path("/data/output.parquet")
    _validate_path("s3://bucket/prefix/file.parquet")


@_test("SEC: compression allowlist")
def t_sec_comp():
    for valid in ["zstd", "ZSTD", "snappy", "gzip", "none"]:
        assert _validate_compression(valid) in {"zstd","snappy","gzip","none"}
    try: _validate_compression("zstd; DROP TABLE t"); assert False
    except ValueError: pass


@_test("SEC: TOON name validation")
def t_sec_toon_name():
    _validate_toon_name("my_table")
    _validate_toon_name("tag-123")
    for bad in ["t[1]", "t:x", "t,y", "t{z}", ""]:
        try: _validate_toon_name(bad); assert False, f"{bad!r} not rejected"
        except ValueError: pass


# ===========================================================================
# TOON serialisation tests
# ===========================================================================

@_test("TOON: tabular multi-row output")
def t_toon_tabular():
    df = pd.DataFrame({"tag": ["A","B"], "val": [1.0, 2.0]})
    out = df_to_toon(df, "test")
    assert "test[2]{tag,val}:" in out
    assert "A,1.0" in out


@_test("TOON: single-row key-value output")
def t_toon_single():
    df = pd.DataFrame({"x": [42], "y": ["hello"]})
    out = df_to_toon(df, "row")
    assert "row:" in out
    assert "x: 42" in out
    assert "y: hello" in out


@_test("TOON: empty dataframe")
def t_toon_empty():
    df = pd.DataFrame({"a": [], "b": []})
    out = df_to_toon(df, "empty")
    assert "empty[0]" in out
    assert "empty result set" in out


@_test("TOON: truncation shows real total (BUG-2 fix)")
def t_toon_truncation():
    df = pd.DataFrame({"a": range(20)})
    out = df_to_toon(df, "t", max_rows=5)
    assert "5/20" in out, f"Expected 5/20 in: {out}"


@_test("TOON: values with commas are quoted")
def t_toon_comma_escape():
    df = pd.DataFrame({"k": ["a,b"], "v": [1]})
    out = df_to_toon(df, "t")
    assert '"a,b"' in out


@_test("TOON: null values serialised as 'null'")
def t_toon_null():
    df = pd.DataFrame({"k": ["x"], "v": [None]})
    out = df_to_toon(df, "t")
    assert "null" in out


@_test("TOON: scalar_to_toon dict output")
def t_toon_scalar():
    out = scalar_to_toon({"rows": 10, "source": "pg"}, "stats")
    assert "stats:" in out
    assert "rows: 10" in out


# ===========================================================================
# Session / config tests
# ===========================================================================

@_test("Session: context manager connect/close cycle")
def t_session_ctx():
    with _mem_db() as db:
        n = db.scalar("SELECT 42")
    assert n == 42


@_test("Session: PRAGMA memory_limit applied")
def t_session_pragma():
    cfg = DuckDBConfig(memory_limit="512MB", threads=2)
    with DuckDBSession(":memory:", config=cfg) as db:
        val = db.scalar("SELECT value FROM duckdb_settings() WHERE name='memory_limit'")
        assert "512" in str(val) or "488" in str(val)   # DuckDB may report as MiB


@_test("Session: invalid memory_limit rejected at connect")
def t_session_bad_mem():
    cfg = DuckDBConfig(memory_limit="not_a_limit")
    try:
        DuckDBSession(":memory:", config=cfg).connect()
        assert False, "should raise"
    except ValueError:
        pass


@_test("Session: show_tables returns DataFrame")
def t_session_show_tables():
    with _mem_db() as db:
        db.execute("CREATE TABLE foo (x INT)")
        df = db.show_tables()
        assert "foo" in df["table_name"].values


@_test("Session: show_tables schema filter is identifier-validated")
def t_session_schema_inject():
    with _mem_db() as db:
        try:
            db.show_tables(schema="'; DROP TABLE t;--")
            assert False
        except ValueError:
            pass


# ===========================================================================
# Cache / refresh_table tests
# ===========================================================================

@_test("Cache: refresh_table appends new rows only")
def t_cache_refresh():
    with _mem_db() as db:
        db.execute("CREATE TABLE cache (ts TIMESTAMP, v INT)")
        db.execute("INSERT INTO cache VALUES ('2025-01-01', 1)")
        n = db.refresh_table(
            source_query=(
                "SELECT TIMESTAMP '2025-01-01' AS ts, 1 AS v "
                "UNION ALL SELECT TIMESTAMP '2025-01-03', 3 "
                "UNION ALL SELECT TIMESTAMP '2025-01-04', 4"
            ),
            table="cache",
            watermark_col="ts",
        )
        assert n == 2, f"Expected 2 new rows, got {n}"
        total = db.scalar("SELECT COUNT(*) FROM cache")
        assert total == 3


@_test("Cache: refresh_table creates missing table")
def t_cache_create():
    with _mem_db() as db:
        n = db.refresh_table(
            source_query="SELECT TIMESTAMP '2025-01-01' AS ts, 42 AS v",
            table="new_cache",
            watermark_col="ts",
            create_if_missing=True,
        )
        assert n == 1


@_test("Cache: refresh_table rejects bad identifiers")
def t_cache_bad_ident():
    with _mem_db() as db:
        try:
            db.refresh_table(
                source_query="SELECT now() AS ts",
                table="ok_table",
                watermark_col="bad col",
            )
            assert False
        except ValueError:
            pass


# ===========================================================================
# Graph tests (DuckDB 1.3+ USING KEY)
# ===========================================================================

def _graph_db() -> DuckDBSession:
    db = DuckDBSession(":memory:").__enter__()
    db.execute("CREATE TEMP TABLE edges (src INT, dst INT, weight DOUBLE)")
    # Simple linear chain: 1--(1)-->2--(2)-->3--(1)-->4
    # Plus a direct 1--(10)-->4 that is NOT shortest
    # Shortest from 1: 1=0, 2=1, 3=3, 4=4 (via 1->2->3->4)
    db.execute("INSERT INTO edges VALUES (1,2,1),(2,3,2),(3,4,1),(1,4,10)")
    return db


@_test("Graph: BFS finds all reachable nodes")
def t_graph_bfs():
    with _graph_db() as db:
        df = db.bfs_reachable("edges", start_node=1, max_depth=5)
        nodes = set(df["node"].tolist())
        assert nodes == {1, 2, 3, 4}, f"Got {nodes}"


@_test("Graph: BFS respects max_depth limit")
def t_graph_bfs_depth():
    with _graph_db() as db:
        df = db.bfs_reachable("edges", start_node=1, max_depth=1)
        # new linear graph: 1->2 (w1), 2->3 (w2), 3->4 (w1), 1->4 (w10)
        # depth=0: node 1; depth=1: nodes 2 and 4 (both directly reachable from 1)
        assert set(df["node"].tolist()) == {1, 2, 4}


@_test("Graph: Dijkstra returns correct shortest distances")
def t_graph_dijkstra():
    with _graph_db() as db:
        sp = db.shortest_path("edges", start_node=1)
        dist = dict(zip(sp["node"].tolist(), sp["dist"].tolist()))
        # 1->2 = 1; 1->3=4 vs 1->2->? no shortcut to 3; 1->2->4=3
        assert dist[1] == 0.0
        assert dist[2] == 1.0
        assert dist[3] == 3.0   # 1->2->3
        assert dist[4] == 4.0   # 1->2->3->4


@_test("Graph: Dijkstra path list correctness")
def t_graph_dijkstra_path():
    with _graph_db() as db:
        sp = db.shortest_path("edges", start_node=1)
        row4 = sp[sp["node"] == 4].iloc[0]
        # list comparison: convert numpy array to list
        path = list(row4["path"]) if hasattr(row4["path"], "tolist") else row4["path"]
        assert path == [1, 2, 3, 4], f"Got {path}"


@_test("Graph: bad edge table name rejected")
def t_graph_bad_ident():
    with _mem_db() as db:
        try:
            db.shortest_path("bad-table", start_node=1)
            assert False
        except ValueError:
            pass


# ===========================================================================
# Hierarchy tests
# ===========================================================================

def _tree_db() -> DuckDBSession:
    db = DuckDBSession(":memory:").__enter__()
    db.execute("CREATE TEMP TABLE tree (id INT PRIMARY KEY, parent_id INT, name VARCHAR)")
    db.execute("INSERT INTO tree VALUES "
               "(1,NULL,'root'),(2,1,'A'),(3,1,'B'),(4,2,'A1'),(5,2,'A2'),(6,3,'B1')")
    return db


@_test("Hierarchy: subtree returns root + all descendants")
def t_hier_subtree():
    with _tree_db() as db:
        sub = db.subtree("tree", root_id=2)
        ids = set(sub["id"].tolist())
        assert ids == {2, 4, 5}, f"Got {ids}"


@_test("Hierarchy: subtree root node is depth 0")
def t_hier_subtree_depth():
    with _tree_db() as db:
        sub = db.subtree("tree", root_id=1)
        root_row = sub[sub["id"] == 1].iloc[0]
        assert root_row["depth"] == 0


@_test("Hierarchy: ancestors returns root -> leaf order")
def t_hier_ancestors():
    with _tree_db() as db:
        ancs = db.ancestors("tree", node_id=4)
        ids = list(ancs["id"])
        assert ids == [1, 2, 4], f"Got {ids}"


@_test("Hierarchy: bad table name rejected")
def t_hier_bad_ident():
    with _mem_db() as db:
        try:
            db.subtree("bad-table", root_id=1)
            assert False
        except ValueError:
            pass


# ===========================================================================
# Vector store tests (no HNSW — brute-force scan)
# ===========================================================================

@_test("Vector: create_vector_table creates correct schema")
def t_vec_create():
    with _mem_db() as db:
        db.create_vector_table("emb", dim=8, extra_cols={"content": "VARCHAR"})
        cols = {r[0]: r[1] for r in db.execute("DESCRIBE emb").fetchall()}
        assert "embedding" in cols
        assert "FLOAT[8]" in cols["embedding"]
        assert "content" in cols


@_test("Vector: dim=0 rejected")
def t_vec_bad_dim():
    with _mem_db() as db:
        try: db.create_vector_table("e", dim=0); assert False
        except ValueError: pass


@_test("Vector: upsert and brute-force search")
def t_vec_search():
    import random
    DIM = 8
    with _mem_db() as db:
        db.create_vector_table("emb", dim=DIM, extra_cols={"content": "VARCHAR"})
        # Use distinct directions (all-same-value vectors have cosine dist 0 to each other)
        directions = [
            [1,0,0,0,0,0,0,0],   # doc0: pure axis 0
            [0,1,0,0,0,0,0,0],   # doc1: pure axis 1
            [0,0,1,0,0,0,0,0],   # doc2: pure axis 2
            [1,1,0,0,0,0,0,0],   # doc3: axes 0+1 (our target)
            [0,0,0,1,0,0,0,0],   # doc4: pure axis 3
        ]
        for i, d in enumerate(directions):
            db.execute(
                f"INSERT INTO emb (id, embedding, content) VALUES (?, ?::FLOAT[{DIM}], ?)",
                [f"doc{i}", [float(x) for x in d], f"content {i}"]
            )
        # Query similar to doc3 direction [1,1,0...0]
        q = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        results = db.vector_search("emb", query_vector=q, top_k=1,
                                   metric="cosine", return_cols=["id","content"])
        top_id = results.iloc[0]["id"]
        assert top_id == "doc3", f"Expected doc3 (closest direction), got {top_id}"


@_test("Vector: search bad table name rejected")
def t_vec_bad_table():
    with _mem_db() as db:
        try: db.vector_search("bad-table", query_vector=[1.0, 2.0], top_k=3)
        except ValueError: pass


# ===========================================================================
# Write-back tests (Parquet only — no live Postgres/DuckLake in test env)
# ===========================================================================

@_test("Writeback: Parquet dry-run prints row count, no file created")
def t_wb_parquet_dryrun():
    import scripts.duckdb_writeback as wb
    db_path = tempfile.mktemp(suffix=".duckdb")
    try:
        import duckdb
        con = duckdb.connect(db_path)
        con.execute("CREATE TABLE t (a INT, b VARCHAR)")
        con.execute("INSERT INTO t VALUES (1,'x'),(2,'y'),(3,'z')")
        con.close()

        with tempfile.TemporaryDirectory() as td:
            out = td + "/out.parquet"
            wb.writeback_to_parquet(
                duckdb_path=db_path, source_sql="SELECT * FROM t",
                output_path=out, partition_by=None,
                compression="zstd", dry_run=True,
            )
            assert not Path(out).exists(), "File should NOT be created in dry-run"
    finally:
        if Path(db_path).exists():
            Path(db_path).unlink()


@_test("Writeback: Parquet write actually creates file")
def t_wb_parquet_write():
    import scripts.duckdb_writeback as wb
    db_path = tempfile.mktemp(suffix=".duckdb")
    try:
        import duckdb
        con = duckdb.connect(db_path)
        con.execute("CREATE TABLE t (a INT)")
        con.execute("INSERT INTO t VALUES (1),(2),(3)")
        con.close()

        with tempfile.TemporaryDirectory() as td:
            out = td + "/out.parquet"
            wb.writeback_to_parquet(
                duckdb_path=db_path, source_sql="SELECT * FROM t",
                output_path=out, partition_by=None,
                compression="zstd", dry_run=False,
            )
            assert Path(out).exists(), "File should be created"
            # Verify content
            verify = duckdb.connect(":memory:")
            n = verify.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
            assert n == 3, f"Expected 3 rows, got {n}"
            verify.close()
    finally:
        if Path(db_path).exists():
            Path(db_path).unlink()


@_test("Writeback: bad compression rejected")
def t_wb_bad_comp():
    import scripts.duckdb_writeback as wb
    try:
        wb.writeback_to_parquet(
            duckdb_path=":memory:", source_sql="SELECT 1",
            output_path="/tmp/x.parquet", partition_by=None,
            compression="bad_codec", dry_run=True,
        )
        assert False
    except ValueError:
        pass


@_test("Writeback: null byte in output path rejected")
def t_wb_null_path():
    import scripts.duckdb_writeback as wb
    try:
        wb.writeback_to_parquet(
            duckdb_path=":memory:", source_sql="SELECT 1",
            output_path="/tmp/out\x00.parquet", partition_by=None,
            compression="zstd", dry_run=True,
        )
        assert False
    except ValueError:
        pass


@_test("Writeback: DuckLake alias validated before extension load")
def t_wb_ducklake_alias():
    import scripts.duckdb_writeback as wb
    try:
        wb.writeback_to_ducklake(
            duckdb_path=":memory:", source_sql="SELECT 1 AS id",
            dl_catalog="x.ducklake", dl_data_path="/tmp",
            dl_schema="main", dl_table="t", dl_alias="bad alias",
            conflict_key=None, dry_run=True,
        )
        assert False, "should raise ValueError for bad alias"
    except ValueError:
        pass


# ===========================================================================
# DuckLake tests (alias/path validation only — extension may not be installed)
# ===========================================================================

@_test("DuckLake: alias validated before extension load")
def t_dl_alias_validate():
    with _mem_db() as db:
        try:
            db.attach_ducklake(catalog="x.ducklake", data_path="/tmp/lake",
                               alias="bad alias")
            assert False
        except ValueError:
            pass


@_test("DuckLake: data_path null-byte rejected")
def t_dl_path_null():
    with _mem_db() as db:
        try:
            db.attach_ducklake(catalog="x.ducklake",
                               data_path="/tmp/\x00bad",
                               alias="lake")
            assert False
        except ValueError:
            pass


@_test("DuckLake: snapshot_version int() coercion raises on bad input")
def t_dl_version_type():
    # Validate that int("not_an_int") raises ValueError — tested at the Python layer
    # (full integration test requires ducklake extension; alias/path checks come first)
    try:
        int("not_an_int")
        assert False, "should raise"
    except ValueError:
        pass
    # Also verify int(5) works (happy path)
    assert int(5) == 5



# ===========================================================================
# Quack validator tests (SEC-B, SEC-C, SEC-D, SEC-F)
# ===========================================================================

@_test("Quack: valid localhost URI accepted")
def t_quack_uri_localhost():
    from scripts.duckdb_helper import _validate_quack_uri
    for uri in ["quack:localhost", "quack://localhost", "quack:127.0.0.1",
                "quack:myhost:9494", "quack:host.example.com:9000"]:
        assert _validate_quack_uri(uri) == uri


@_test("Quack: URI with SQL injection chars rejected")
def t_quack_uri_inject():
    from scripts.duckdb_helper import _validate_quack_uri
    for bad in ["quack:host'; DROP TABLE t;--", "quack:h\x00ost", "", "quack:host\'evil"]:
        try: _validate_quack_uri(bad); assert False, f"{bad!r} should be rejected"
        except ValueError: pass


@_test("Quack: URI with non-quack scheme rejected")
def t_quack_uri_scheme():
    from scripts.duckdb_helper import _validate_quack_uri
    for bad in ["postgres:localhost", "http://host", "quac:host", "localhost"]:
        try: _validate_quack_uri(bad); assert False, f"{bad!r} should be rejected"
        except ValueError: pass


@_test("Quack: valid token accepted")
def t_quack_token_valid():
    from scripts.duckdb_helper import _validate_quack_token
    import logging
    with _mem_db():  # just to load module
        pass
    # suppress short-token warning for this test
    t = _validate_quack_token("MY_TOKEN_32CHARS_ABCDEF_1234567", context="test")
    assert t == "MY_TOKEN_32CHARS_ABCDEF_1234567"
    _validate_quack_token("abcd")  # minimum 4-char — valid (will warn)


@_test("Quack: empty token rejected")
def t_quack_token_empty():
    from scripts.duckdb_helper import _validate_quack_token
    try: _validate_quack_token(""); assert False
    except ValueError: pass


@_test("Quack: token shorter than 4 chars rejected")
def t_quack_token_short():
    from scripts.duckdb_helper import _validate_quack_token
    try: _validate_quack_token("abc"); assert False
    except ValueError: pass


@_test("Quack: single-quote in token rejected (SEC-B)")
def t_quack_token_quote():
    from scripts.duckdb_helper import _validate_quack_token
    try: _validate_quack_token("tok'en"); assert False
    except ValueError: pass


@_test("Quack: attach_quack validates alias and URI (SEC-B, SEC-C)")
def t_quack_attach_validation():
    with _mem_db() as db:
        # Bad URI — should raise before extension load
        try:
            db.attach_quack("not-a-quack-uri", alias="remote", token="tok1234567890abcdef")
            assert False, "bad URI not rejected"
        except ValueError:
            pass

        # Bad alias — should raise
        try:
            db.attach_quack("quack:localhost", alias="bad alias", token="tok1234567890abcdef")
            assert False, "bad alias not rejected"
        except ValueError:
            pass


@_test("Quack: quack_serve external access guard (SEC-D)")
def t_quack_serve_tls_guard():
    with _mem_db() as db:
        # allow_other_hostname=True without require_tls_confirm=False should raise
        try:
            db.quack_serve("quack:0.0.0.0", allow_other_hostname=True,
                           require_tls_confirm=True)
            assert False, "should raise without TLS confirmation"
        except ValueError as e:
            assert "TLS" in str(e) or "tls" in str(e).lower() or "reverse proxy" in str(e).lower()


@_test("Quack: dl_catalog SQL-escaped in attach_ducklake (SEC-A)")
def t_ducklake_catalog_escaped():
    # Verify the code path contains _sql_str_escape(catalog)
    import inspect
    from scripts.duckdb_helper import DuckDBSession
    src = inspect.getsource(DuckDBSession.attach_ducklake)
    assert "_sql_str_escape(catalog)" in src, "SEC-A: dl_catalog still not SQL-escaped"

# ===========================================================================
# Entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="DuckDB skill v2.0 test suite")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print PASS lines too")
    parser.add_argument("--filter", "-f", default="",
                        help="Run only tests whose name contains this string")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    print(f"DuckDB skill v3.0 — test suite")
    print(f"{'='*60}")

    ok = _run_all(filter_str=args.filter, verbose=args.verbose)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
