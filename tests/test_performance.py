"""Performance tests for semantic search and HD fingerprints (OHM-c1id).

These tests are marked ``@pytest.mark.performance`` and excluded from the
default run.  Execute them explicitly with::

    pytest -m performance -v

Each test enforces a p95 latency threshold and prints a one-line summary
that can be tracked over time.
"""

from __future__ import annotations

import time

import duckdb
import pytest

from ohm.graph.schema import DEFAULT_SCHEMA, initialize_schema
from ohm.graph.queries import create_node, update_node_hd_fingerprint
from ohm.inference.hd import fingerprint_text, hamming_similarity


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_db(n_nodes: int) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    initialize_schema(conn, DEFAULT_SCHEMA)
    for i in range(n_nodes):
        create_node(conn, label=f"node_{i:04d}", node_type="concept", created_by="perf")
    return conn


def _seed_embeddings(conn: duckdb.DuckDBPyConnection, dim: int = 768) -> None:
    rows = conn.execute("SELECT id FROM ohm_nodes WHERE deleted_at IS NULL").fetchall()
    for idx, (nid,) in enumerate(rows):
        emb = [float(idx % 10) / 10.0] * dim
        conn.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [emb, nid],
        )


def _measure(fn, iterations: int = 5) -> list[float]:
    times: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return times


def _p95(times: list[float]) -> float:
    sorted_times = sorted(times)
    idx = int(len(sorted_times) * 0.95)
    return sorted_times[min(idx, len(sorted_times) - 1)]


SINGLE_QUERY_THRESHOLD_MS = 200
BATCH_QUERY_THRESHOLD_MS = 2000


# ── Semantic search ──────────────────────────────────────────────────────────


@pytest.mark.performance
class TestSemanticSearchLatency:
    """Latency-gated semantic search tests at 100/500/1000 nodes."""

    def test_semantic_search_100_nodes_p95_under_200ms(self):
        conn = _make_db(100)
        _seed_embeddings(conn)
        query_emb = [0.0] * 768
        query_emb[0] = 1.0

        def run():
            conn.execute(
                "SELECT id, label, array_cosine_distance(embedding, ?::FLOAT[768]) AS dist "
                "FROM ohm_nodes WHERE embedding IS NOT NULL ORDER BY dist LIMIT 10",
                [query_emb],
            ).fetchall()

        times = _measure(run, iterations=10)
        p95_ms = _p95(times) * 1000
        print(f"\n[perf] semantic_search 100 nodes p95={p95_ms:.1f}ms")
        assert p95_ms < SINGLE_QUERY_THRESHOLD_MS, f"p95 {p95_ms:.1f}ms > {SINGLE_QUERY_THRESHOLD_MS}ms"
        conn.close()

    def test_semantic_search_500_nodes_p95_under_200ms(self):
        conn = _make_db(500)
        _seed_embeddings(conn)
        query_emb = [0.0] * 768
        query_emb[0] = 1.0

        def run():
            conn.execute(
                "SELECT id, label, array_cosine_distance(embedding, ?::FLOAT[768]) AS dist "
                "FROM ohm_nodes WHERE embedding IS NOT NULL ORDER BY dist LIMIT 10",
                [query_emb],
            ).fetchall()

        times = _measure(run, iterations=10)
        p95_ms = _p95(times) * 1000
        print(f"\n[perf] semantic_search 500 nodes p95={p95_ms:.1f}ms")
        assert p95_ms < SINGLE_QUERY_THRESHOLD_MS, f"p95 {p95_ms:.1f}ms > {SINGLE_QUERY_THRESHOLD_MS}ms"
        conn.close()

    def test_semantic_search_batch_100_queries_p95_under_2s(self):
        conn = _make_db(100)
        _seed_embeddings(conn)

        def run():
            for i in range(100):
                q = [float(i % 10) / 10.0] * 768
                conn.execute(
                    "SELECT id FROM ohm_nodes WHERE embedding IS NOT NULL "
                    "ORDER BY array_cosine_distance(embedding, ?::FLOAT[768]) LIMIT 5",
                    [q],
                ).fetchall()

        times = _measure(run, iterations=3)
        p95_ms = _p95(times) * 1000
        print(f"\n[perf] semantic_search batch=100 p95={p95_ms:.1f}ms")
        assert p95_ms < BATCH_QUERY_THRESHOLD_MS, f"batch p95 {p95_ms:.1f}ms > {BATCH_QUERY_THRESHOLD_MS}ms"
        conn.close()


# ── HD fingerprints ──────────────────────────────────────────────────────────


@pytest.mark.performance
class TestHDFingerprintLatency:
    """Latency-gated HD fingerprint operations."""

    def test_fingerprint_text_p95_under_200ms(self):
        text = "AND→OR conversion enables cheaper retries in distributed systems."

        def run():
            fingerprint_text(text)

        times = _measure(run, iterations=20)
        p95_ms = _p95(times) * 1000
        print(f"\n[perf] fingerprint_text p95={p95_ms:.1f}ms")
        assert p95_ms < SINGLE_QUERY_THRESHOLD_MS, f"p95 {p95_ms:.1f}ms > {SINGLE_QUERY_THRESHOLD_MS}ms"

    def test_hd_similarity_p95_under_200ms(self):
        hv_a = fingerprint_text("causal claim about supply chain disruption")
        hv_b = fingerprint_text("Bayesian inference over causal graph edges")

        def run():
            hamming_similarity(hv_a, hv_b)

        times = _measure(run, iterations=20)
        p95_ms = _p95(times) * 1000
        print(f"\n[perf] hd_similarity p95={p95_ms:.1f}ms")
        assert p95_ms < SINGLE_QUERY_THRESHOLD_MS, f"p95 {p95_ms:.1f}ms > {SINGLE_QUERY_THRESHOLD_MS}ms"

    def test_update_node_hd_fingerprint_100_nodes_p95_under_2s(self):
        conn = _make_db(100)
        node_ids = [r[0] for r in conn.execute("SELECT id FROM ohm_nodes WHERE deleted_at IS NULL").fetchall()]

        def run():
            for nid in node_ids:
                update_node_hd_fingerprint(conn, nid)

        times = _measure(run, iterations=3)
        p95_ms = _p95(times) * 1000
        print(f"\n[perf] update_hd_fingerprint 100 nodes p95={p95_ms:.1f}ms")
        assert p95_ms < BATCH_QUERY_THRESHOLD_MS, f"batch p95 {p95_ms:.1f}ms > {BATCH_QUERY_THRESHOLD_MS}ms"
        conn.close()


# ── Source diversity score ───────────────────────────────────────────────────


@pytest.mark.performance
class TestSourceDiversityLatency:
    """Latency-gated source_diversity_score at depth=3 on a dense node."""

    def test_source_diversity_score_p95_under_200ms(self):
        from ohm.graph.methods import source_diversity_score

        conn = _make_db(50)
        # Build a dense cluster: node_0 is the target, 10 supporters each
        # with 3 supporting edges from distinct nodes.
        target = [r[0] for r in conn.execute("SELECT id FROM ohm_nodes LIMIT 1").fetchall()][0]
        supporters = [r[0] for r in conn.execute("SELECT id FROM ohm_nodes OFFSET 1 LIMIT 10").fetchall()]
        leaves = [r[0] for r in conn.execute("SELECT id FROM ohm_nodes OFFSET 11 LIMIT 30").fetchall()]

        for sup in supporters:
            conn.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by) "
                "VALUES (?, ?, ?, 'SUPPORTS', 'L3', 0.8, 'perf')",
                [f"e_{sup}_{target}", sup, target],
            )
            for j, leaf in enumerate(leaves[:3]):
                conn.execute(
                    "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by) "
                    "VALUES (?, ?, ?, 'SUPPORTS', 'L3', 0.7, 'perf')",
                    [f"e_{leaf}_{sup}_{j}", leaf, sup],
                )

        def run():
            source_diversity_score(conn, target, max_depth=3)

        times = _measure(run, iterations=10)
        p95_ms = _p95(times) * 1000
        print(f"\n[perf] source_diversity_score depth=3 p95={p95_ms:.1f}ms")
        assert p95_ms < SINGLE_QUERY_THRESHOLD_MS, f"p95 {p95_ms:.1f}ms > {SINGLE_QUERY_THRESHOLD_MS}ms"
        conn.close()