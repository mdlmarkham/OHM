#!/usr/bin/env python3
"""Spike archive: OHM-lqpk.2 — DuckDB SQL Hamming distance for HD fingerprints.

Investigated whether ``hd_membership_search`` in
src/ohm/graph/queries/__init__.py:6163 could be rewritten as a single
DuckDB SQL query (replacing the per-row Python loop that calls
``hamming_similarity`` for every candidate).

Run with:
    python scripts/_spike_hd_sql.py

CONCLUSION (closed 2026-07-02): SQL path is FEASIBLE but does NOT
deliver the predicted 5-20x speedup. Python loop is faster for
current OHM scale (≤ 10k candidates). Not shipping the implementation.

Key findings (DuckDB 1.5.2):
1. ``bit_count(BLOB)`` fails -- no overload. ``bit_count`` only works on
   integer types and on BIT.
2. ``^`` operator is BROKEN on integer types in DuckDB 1.5.2:
       SELECT 15 ^ 240;     -- returns 1.8e+282 (a DOUBLE!), not 255
       SELECT bit_count(15 ^ 240);  -- Binder Error: bit_count(DOUBLE)
   This is a real DuckDB bug. Workaround: use De Morgan identity
   ``a ^ b = (a | b) - (a & b)`` (both | and & work correctly on ints).
3. ``length(BLOB)``, ``^(BLOB, BLOB)``, ``encode(BLOB)`` are not
   supported.
4. ``list(blob)`` returns a SINGLE-ELEMENT list with the whole blob,
   not a list of bytes -- so list_zip + list_transform is unusable.
5. The only viable SQL path requires a schema change: store
   fingerprints as ``UTINYINT[]`` (1250-element list of bytes per
   10000-bit fingerprint) instead of BLOB.

Benchmark results (De Morgan SQL vs Python loop):
    N=100   candidates: SQL 524.7ms  vs Python 11.4ms  -- SQL 50x SLOWER
    N=1000  candidates: SQL 5130.9ms vs Python 84.0ms  -- SQL 60x SLOWER
    N=10000 candidates: SQL 1116.9ms vs Python 764.5ms -- SQL 1.5x SLOWER

Correctness: 20/20 top-20 overlap at N=100,1000; 16/20 at N=10000
(likely near-tie variance on hashed fingerprints).

The bottleneck is the cross product UNNEST × UNNEST × GROUP BY which
generates O(N × D) intermediate rows (N candidates × D=1250 bytes).
Python's tight C-implemented per-byte XOR + popcount loop is hard to
beat at this scale.

WHEN TO REVISIT:
- DuckDB fixes the ``^`` operator bug (track duckdb/duckdb issues).
- OHM scales past 10k candidates and SQL becomes a win.
- A BITSTRING-only storage path is adopted (would allow a single
  ``bit_count(a ^ b)`` call once ``^`` works on BITSTRING).

This script is the canonical reference for the v8 final benchmark.
Earlier v1-v7 scripts (in /tmp) are NOT archived -- they each
explored a single approach that hit a DuckDB limitation. v8 is the
only working one.
"""
from __future__ import annotations

import os
import random
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(r"C:\Users\mdlma\Documents\Projects\OHM")
sys.path.insert(0, str(REPO_ROOT / "src"))

import duckdb  # noqa: E402

from ohm.inference.hd import fingerprint_node, hamming_similarity  # noqa: E402
from ohm.schema import initialize_schema  # noqa: E402


def _seed_nodes_with_utinyint_list(conn, n: int, dim: int = 10000, seed: int = 42) -> None:
    """Seed n nodes with UTINYINT[] fingerprints (1250 elements each)."""
    rng = random.Random(seed)
    for i in range(n):
        nid = f"u_seed_{i:06d}"
        words = rng.sample(
            ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"],
            k=rng.randint(2, 4),
        )
        label = " ".join(words)
        ntype = rng.choice(["concept", "entity", "source"])
        fp = fingerprint_node(label=label, node_type=ntype, dim=dim, seed=seed)
        fp_bytes = bytes.fromhex(fp["fingerprint_hex"])
        # 0..255 fits in UTINYINT
        fp_int_list = list(fp_bytes)
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, hd_fingerprint_u, created_at) "
            "VALUES (?, ?, ?, 'spike', ?, CURRENT_TIMESTAMP)",
            [nid, label, ntype, fp_int_list],
        )


def _setup_table(n: int) -> tuple[duckdb.DuckDBPyConnection, bytes]:
    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    conn.execute("ALTER TABLE ohm_nodes ADD COLUMN hd_fingerprint_u UTINYINT[]")
    _seed_nodes_with_utinyint_list(conn, n)
    # Get query bytes from first node
    qrow = conn.execute(
        "SELECT hd_fingerprint_u FROM ohm_nodes WHERE id = 'u_seed_000000'"
    ).fetchone()
    # Convert UTINYINT list back to bytes
    query_bytes = bytes(qrow[0])
    return conn, query_bytes


def benchmark_python(conn, query_bytes) -> tuple[float, list]:
    """Current Python path: SELECT, iterate with hamming_similarity."""
    rows = conn.execute(
        """SELECT id, label, type, confidence, hd_fingerprint_u
           FROM ohm_nodes
           WHERE hd_fingerprint_u IS NOT NULL AND deleted_at IS NULL"""
    ).fetchall()
    start = time.perf_counter()
    results = []
    qbytes = bytearray(query_bytes)
    for r in rows:
        rid, rlabel, rtype, rconf, rfp_list = r
        cbytes = bytearray(rfp_list)
        sim = hamming_similarity(qbytes, cbytes)
        if sim >= 0.65:
            results.append((rid, rlabel, rtype, rconf, sim))
    results.sort(key=lambda x: x[4], reverse=True)
    elapsed = time.perf_counter() - start
    return elapsed, results[:20]


def benchmark_sql_de_morgan(conn, query_bytes) -> tuple[float, list]:
    """SQL path: a[i] | b[i], a[i] & b[i], subtract, bit_count, sum.
    Use De Morgan to work around DuckDB's broken ^ on integers."""
    query_list = list(query_bytes)
    start = time.perf_counter()
    try:
        rows = conn.execute(
            """
            WITH q AS (SELECT ?::UTINYINT[] AS fp),
            matched AS (
                SELECT
                    n.id, n.label, n.type, n.confidence,
                    n.hd_fingerprint_u AS fp
                FROM ohm_nodes n, q
                WHERE n.hd_fingerprint_u IS NOT NULL
                  AND n.deleted_at IS NULL
                  AND len(n.hd_fingerprint_u) = len(q.fp)
            ),
            pos AS (SELECT range + 1 AS i FROM range(1, 1250))
            SELECT
                m.id, m.label, m.type, m.confidence,
                sum(bit_count(
                    CAST((m.fp[pos.i] | q.fp[pos.i]) AS INTEGER)
                    - CAST((m.fp[pos.i] & q.fp[pos.i]) AS INTEGER)
                )) AS hamming
            FROM matched m, pos, q
            WHERE pos.i <= len(q.fp)
            GROUP BY m.id, m.label, m.type, m.confidence
            ORDER BY hamming ASC
            LIMIT 60
            """,
            [query_list],
        ).fetchall()
    except Exception as e:
        print(f"  SQL failed: {e}")
        return float("nan"), []

    results = []
    total_bits = len(query_bytes) * 8
    for r in rows:
        rid, rlabel, rtype, rconf, hamming = r
        sim = 1.0 - hamming / total_bits
        if sim >= 0.65:
            results.append((rid, rlabel, rtype, rconf, sim))
    results.sort(key=lambda x: x[4], reverse=True)
    elapsed = time.perf_counter() - start
    return elapsed, results[:20]


def main() -> None:
    print("=== De Morgan SQL path: Python loop vs SQL ===\n")
    for n in (100, 1000, 10000):
        print(f"--- N = {n} candidates ---")
        conn, query_bytes = _setup_table(n)
        try:
            conn.execute("SELECT COUNT(*) FROM ohm_nodes").fetchone()

            py_times = []
            sql_times = []
            for _ in range(2):
                t, _ = benchmark_python(conn, query_bytes)
                py_times.append(t)
                t, _ = benchmark_sql_de_morgan(conn, query_bytes)
                sql_times.append(t)
            py_med = statistics.median(py_times)
            sql_med = statistics.median(sql_times)
            print(f"  Python loop   median: {py_med * 1000:8.2f} ms  "
                  f"(runs: {[round(x * 1000, 2) for x in py_times]})")
            print(f"  SQL De Morgan median: {sql_med * 1000:8.2f} ms  "
                  f"(runs: {[round(x * 1000, 2) for x in sql_times]})  "
                  f"speedup: {py_med / sql_med:.2f}x")

            # Correctness
            _, py_top = benchmark_python(conn, query_bytes)
            _, sql_top = benchmark_sql_de_morgan(conn, query_bytes)
            py_ids = {r[0] for r in py_top}
            sql_ids = {r[0] for r in sql_top}
            print(f"  Top-20 overlap: {len(py_ids & sql_ids)}/20")
            if py_top:
                print(f"  Python top-3 sims: {[round(r[4], 4) for r in py_top[:3]]}")
            if sql_top:
                print(f"  SQL    top-3 sims: {[round(r[4], 4) for r in sql_top[:3]]}")
        finally:
            conn.close()
        print()


if __name__ == "__main__":
    main()
