"""Performance benchmarks for OHM graph queries.

Measures latency of CTE traversal queries at increasing graph sizes.
Results are written to docs/benchmarks/ as JSON for trend analysis.

Usage:
    pytest tests/test_benchmarks.py -v --benchmark-only
    pytest tests/test_benchmarks.py -v --benchmark-only --benchmark-json=docs/benchmarks/results.json
"""

import pytest

from tests.conftest import create_sample_graph, create_test_db


class TestNeighborhoodBenchmarks:
    """Benchmark neighborhood query at various scales."""

    @pytest.mark.benchmark(group="neighborhood")
    def test_neighborhood_small(self, benchmark):
        """Neighborhood query on small graph (3 nodes, 2 edges)."""
        from ohm.queries import query_neighborhood

        conn = create_test_db()
        graph = create_sample_graph(conn, size="small")
        node_a = graph["nodes"]["a"]

        def run():
            return query_neighborhood(conn, node_a, depth=3)

        result = benchmark(run)
        assert len(result) >= 1
        conn.close()

    @pytest.mark.benchmark(group="neighborhood")
    def test_neighborhood_medium(self, benchmark):
        """Neighborhood query on medium graph (6 nodes, 8 edges)."""
        from ohm.queries import query_neighborhood

        conn = create_test_db()
        graph = create_sample_graph(conn, size="medium")
        node_a = graph["nodes"]["A"]

        def run():
            return query_neighborhood(conn, node_a, depth=3)

        result = benchmark(run)
        assert len(result) >= 1
        conn.close()

    @pytest.mark.benchmark(group="neighborhood")
    def test_neighborhood_large(self, benchmark):
        """Neighborhood query on large graph (10 nodes, 13 edges)."""
        from ohm.queries import query_neighborhood

        conn = create_test_db()
        graph = create_sample_graph(conn, size="large")
        node_a = graph["nodes"]["A"]

        def run():
            return query_neighborhood(conn, node_a, depth=5)

        result = benchmark(run)
        assert len(result) >= 1
        conn.close()


class TestPathBenchmarks:
    """Benchmark shortest path queries."""

    @pytest.mark.benchmark(group="path")
    def test_path_large(self, benchmark):
        """Path finding on large graph."""
        from ohm.queries import query_path

        conn = create_test_db()
        graph = create_sample_graph(conn, size="large")
        node_a = graph["nodes"]["A"]
        node_j = graph["nodes"]["J"]

        def run():
            return query_path(conn, node_a, node_j, max_depth=20)

        benchmark(run)
        conn.close()


class TestImpactBenchmarks:
    """Benchmark impact analysis queries."""

    @pytest.mark.benchmark(group="impact")
    def test_impact_large(self, benchmark):
        """Impact analysis on large graph."""
        from ohm.queries import query_impact

        conn = create_test_db()
        graph = create_sample_graph(conn, size="large")
        node_a = graph["nodes"]["A"]

        def run():
            return query_impact(conn, node_a, depth=10)

        benchmark(run)
        conn.close()


class TestStatsBenchmarks:
    """Benchmark statistics queries."""

    @pytest.mark.benchmark(group="stats")
    def test_stats_large(self, benchmark):
        """Stats query on large graph."""
        from ohm.queries import query_stats

        conn = create_test_db()
        create_sample_graph(conn, size="large")

        def run():
            return query_stats(conn)

        result = benchmark(run)
        assert result["total_nodes"] >= 10
        conn.close()


class TestWriteBenchmarks:
    """Benchmark write operations."""

    @pytest.mark.benchmark(group="write")
    @pytest.mark.benchmark(group="create")
    @pytest.mark.performance
    def test_create_node(self, benchmark):
        """Node creation latency."""
        from ohm.queries import create_node

        conn = create_test_db()

        def run():
            return create_node(conn, label="bench_node", created_by="bench")

        benchmark(run)
        conn.close()

    @pytest.mark.benchmark(group="write")
    @pytest.mark.performance
    def test_create_edge(self, benchmark):
        """Edge creation latency."""
        from ohm.queries import create_edge, create_node

        conn = create_test_db()
        a = create_node(conn, label="A", created_by="bench")
        b = create_node(conn, label="B", created_by="bench")

        def run():
            return create_edge(
                conn,
                from_node=a,
                to_node=b,
                layer="L3",
                edge_type="CAUSES",
                created_by="bench",
            )

        benchmark(run)
        conn.close()


class TestSemanticSearchBenchmarks:
    """Benchmark semantic search with VSS/HNSW index (OHM-c1id)."""

    @pytest.mark.benchmark(group="semantic_search")
    def test_semantic_search_small(self, benchmark):
        """Semantic search on small graph (3 nodes with embeddings)."""
        from ohm.queries import create_node

        conn = create_test_db()

        for i in range(3):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            emb = [float(i) / 10.0] * 768
            conn.execute(
                "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
                [emb, n["id"]],
            )

        query_emb = [0.0] * 768
        query_emb[0] = 1.0

        def run():
            return conn.execute(
                "SELECT id, label, array_cosine_distance(embedding, ?::FLOAT[768]) AS distance FROM ohm_nodes WHERE embedding IS NOT NULL ORDER BY distance LIMIT 10",
                [query_emb],
            ).fetchall()

        result = benchmark(run)
        assert len(result) == 3
        conn.close()

    @pytest.mark.benchmark(group="semantic_search")
    def test_semantic_search_medium(self, benchmark):
        """Semantic search on medium graph (100 nodes with embeddings)."""
        from ohm.queries import create_node

        conn = create_test_db()

        for i in range(100):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            emb = [float(i % 10) / 10.0] * 768
            conn.execute(
                "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
                [emb, n["id"]],
            )

        query_emb = [0.0] * 768
        query_emb[0] = 1.0

        def run():
            return conn.execute(
                "SELECT id, label, array_cosine_distance(embedding, ?::FLOAT[768]) AS distance FROM ohm_nodes WHERE embedding IS NOT NULL ORDER BY distance LIMIT 10",
                [query_emb],
            ).fetchall()

        result = benchmark(run)
        assert len(result) == 10
        conn.close()

    @pytest.mark.benchmark(group="semantic_search")
    def test_semantic_search_large(self, benchmark):
        """Semantic search on large graph (500 nodes with embeddings)."""
        from ohm.queries import create_node

        conn = create_test_db()

        for i in range(500):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            emb = [float(i % 20) / 20.0] * 768
            conn.execute(
                "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
                [emb, n["id"]],
            )

        query_emb = [0.0] * 768
        query_emb[0] = 1.0

        def run():
            return conn.execute(
                "SELECT id, label, array_cosine_distance(embedding, ?::FLOAT[768]) AS distance FROM ohm_nodes WHERE embedding IS NOT NULL ORDER BY distance LIMIT 10",
                [query_emb],
            ).fetchall()

        result = benchmark(run)
        assert len(result) == 10
        conn.close()


class TestHDFingerprintBenchmarks:
    """Benchmark HD fingerprint operations (OHM-c1id)."""

    @pytest.mark.benchmark(group="hd_fingerprint")
    def test_hd_fingerprint_update_small(self, benchmark):
        """HD fingerprint update on small graph (3 nodes)."""
        from ohm.queries import create_node
        from ohm.graph.queries import update_node_hd_fingerprint

        conn = create_test_db()
        nodes = []
        for i in range(3):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            nodes.append(n["id"])

        def run():
            for nid in nodes:
                update_node_hd_fingerprint(conn, nid)

        benchmark(run)
        conn.close()

    @pytest.mark.benchmark(group="hd_fingerprint")
    def test_hd_fingerprint_update_medium(self, benchmark):
        """HD fingerprint update on medium graph (100 nodes)."""
        from ohm.queries import create_node
        from ohm.graph.queries import update_node_hd_fingerprint

        conn = create_test_db()
        nodes = []
        for i in range(100):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            nodes.append(n["id"])

        def run():
            for nid in nodes:
                update_node_hd_fingerprint(conn, nid)

        benchmark(run)
        conn.close()

    @pytest.mark.benchmark(group="hd_fingerprint")
    def test_hd_fingerprint_update_large(self, benchmark):
        """HD fingerprint update on large graph (500 nodes)."""
        from ohm.queries import create_node
        from ohm.graph.queries import update_node_hd_fingerprint

        conn = create_test_db()
        nodes = []
        for i in range(500):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            nodes.append(n["id"])

        def run():
            for nid in nodes:
                update_node_hd_fingerprint(conn, nid)

        benchmark(run)
        conn.close()

    @pytest.mark.benchmark(group="hd_fingerprint")
    def test_hd_fingerprint_query_small(self, benchmark):
        """HD fingerprint similarity query on small graph (3 nodes)."""
        from ohm.queries import create_node
        from ohm.graph.queries import update_node_hd_fingerprint

        conn = create_test_db()
        for i in range(3):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            update_node_hd_fingerprint(conn, n["id"])

        def run():
            return conn.execute("SELECT id, hd_fingerprint FROM ohm_nodes WHERE hd_fingerprint IS NOT NULL LIMIT 10").fetchall()

        result = benchmark(run)
        assert len(result) == 3
        conn.close()

    @pytest.mark.benchmark(group="hd_fingerprint")
    def test_hd_fingerprint_query_large(self, benchmark):
        """HD fingerprint similarity query on large graph (500 nodes)."""
        from ohm.queries import create_node
        from ohm.graph.queries import update_node_hd_fingerprint

        conn = create_test_db()
        for i in range(500):
            n = create_node(conn, label=f"node_{i}", node_type="concept", created_by="bench")
            update_node_hd_fingerprint(conn, n["id"])

        def run():
            return conn.execute("SELECT id, hd_fingerprint FROM ohm_nodes WHERE hd_fingerprint IS NOT NULL LIMIT 100").fetchall()

        result = benchmark(run)
        assert len(result) == 100
        conn.close()
