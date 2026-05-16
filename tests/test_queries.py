"""Tests for the OHM CTE graph traversal queries."""

import pytest


class TestNeighborhoodQuery:
    """Tests for bounded-depth graph traversal."""

    def test_neighborhood_depth_1(self, test_db, sample_graph_small):
        """Depth-1 neighborhood returns direct edges only."""
        from ohm.queries import query_neighborhood

        node_a = sample_graph_small["nodes"]["a"]
        results = query_neighborhood(test_db, node_a, depth=1)

        assert len(results) >= 1
        # All results should be at hop 0 or 1
        for r in results:
            assert r["hop"] <= 1

    def test_neighborhood_depth_2(self, test_db, sample_graph_small):
        """Depth-2 neighborhood reaches further."""
        from ohm.queries import query_neighborhood

        node_a = sample_graph_small["nodes"]["a"]
        depth1 = query_neighborhood(test_db, node_a, depth=1)
        depth2 = query_neighborhood(test_db, node_a, depth=2)

        assert len(depth2) >= len(depth1)

    def test_neighborhood_layer_filter(self, test_db, sample_graph_medium):
        """Layer filter restricts results to that layer."""
        from ohm.queries import query_neighborhood

        node_a = sample_graph_medium["nodes"]["A"]
        l3_only = query_neighborhood(test_db, node_a, depth=3, layer="L3")

        for r in l3_only:
            assert r["layer"] == "L3"

    def test_neighborhood_direction_outgoing(self, test_db, sample_graph_medium):
        """Outgoing direction only follows edges from the start node."""
        from ohm.queries import query_neighborhood

        node_a = sample_graph_medium["nodes"]["A"]
        results = query_neighborhood(test_db, node_a, depth=2, direction="outgoing")

        for r in results:
            assert r["from_node"] == node_a or r["from_node"] in {
                e["to_node"] for e in results if e["hop"] < r["hop"]
            }

    def test_neighborhood_nonexistent_node(self, test_db):
        """Querying a nonexistent node returns empty results."""
        from ohm.queries import query_neighborhood

        results = query_neighborhood(test_db, "nonexistent_node", depth=3)
        assert len(results) == 0


class TestPathQuery:
    """Tests for shortest path finding."""

    def test_path_direct_edge(self, test_db, sample_graph_small):
        """Path between directly connected nodes returns one edge."""
        from ohm.queries import query_path

        node_a = sample_graph_small["nodes"]["a"]
        node_b = sample_graph_small["nodes"]["b"]
        results = query_path(test_db, node_a, node_b, max_depth=5)

        assert len(results) >= 1

    def test_path_two_hops(self, test_db, sample_graph_small):
        """Path between indirectly connected nodes returns multiple edges."""
        from ohm.queries import query_path

        node_a = sample_graph_small["nodes"]["a"]
        node_c = sample_graph_small["nodes"]["c"]
        results = query_path(test_db, node_a, node_c, max_depth=5)

        assert len(results) >= 1

    def test_path_no_path(self, test_db, sample_graph_small):
        """Path between disconnected nodes returns empty."""
        from ohm.queries import query_path

        # Create an isolated node
        from tests.conftest import create_sample_node
        isolated = create_sample_node(test_db, label="Isolated")

        results = query_path(test_db, sample_graph_small["nodes"]["a"], isolated, max_depth=5)
        assert len(results) == 0

    def test_path_max_depth_limit(self, test_db, sample_graph_large):
        """Path finding respects max_depth limit."""
        from ohm.queries import query_path

        node_a = sample_graph_large["nodes"]["A"]
        node_j = sample_graph_large["nodes"]["J"]
        # With max_depth=2, should not find the path (chain is 9 hops)
        results = query_path(test_db, node_a, node_j, max_depth=2)
        assert len(results) == 0


class TestImpactQuery:
    """Tests for downstream impact analysis."""

    def test_impact_downstream(self, test_db, sample_graph_medium):
        """Impact analysis finds downstream nodes."""
        from ohm.queries import query_impact

        node_a = sample_graph_medium["nodes"]["A"]
        results = query_impact(test_db, node_a, depth=5)

        assert len(results) >= 1
        # All results should be L2 or L3
        for r in results:
            assert r["layer"] in ("L2", "L3")

    def test_impact_no_downstream(self, test_db, sample_graph_small):
        """Leaf node has no downstream impact."""
        from ohm.queries import query_impact

        node_c = sample_graph_small["nodes"]["c"]
        results = query_impact(test_db, node_c, depth=5)
        assert len(results) == 0


class TestConfidenceQuery:
    """Tests for confidence audit."""

    def test_confidence_original_edge(self, test_db, sample_graph_medium):
        """Confidence audit returns original edge details."""
        from ohm.queries import query_confidence

        edge_ab = sample_graph_medium["edges"]["ab"]
        result = query_confidence(test_db, edge_ab)

        assert result["original"] is not None
        assert result["original"]["edge_type"] == "CAUSES"

    def test_confidence_challenges(self, test_db, sample_graph_medium):
        """Confidence audit returns challenge edges."""
        from ohm.queries import query_confidence

        edge_ab = sample_graph_medium["edges"]["ab"]
        result = query_confidence(test_db, edge_ab)

        assert len(result["challenges"]) >= 1
        assert result["challenges"][0]["edge_type"] == "CHALLENGED_BY"

    def test_confidence_supports(self, test_db, sample_graph_medium):
        """Confidence audit returns support edges."""
        from ohm.queries import query_confidence

        edge_ab = sample_graph_medium["edges"]["ab"]
        result = query_confidence(test_db, edge_ab)

        assert len(result["supports"]) >= 1
        assert result["supports"][0]["edge_type"] == "SUPPORTS"

    def test_confidence_nonexistent_edge(self, test_db):
        """Confidence audit for nonexistent edge returns None original."""
        from ohm.queries import query_confidence

        result = query_confidence(test_db, "nonexistent_edge")
        assert result["original"] is None
        assert result["challenges"] == []
        assert result["supports"] == []


class TestStatsQuery:
    """Tests for graph statistics."""

    def test_stats_counts(self, test_db, sample_graph_medium):
        """Stats returns correct counts for sample graph."""
        from ohm.queries import query_stats

        stats = query_stats(test_db)

        assert stats["total_nodes"] >= 6
        assert stats["total_edges"] >= 8
        assert "L3" in stats["edges_by_layer"]
        assert "L2" in stats["edges_by_layer"]

    def test_stats_challenge_ratio(self, test_db, sample_graph_medium):
        """Challenge ratio is calculated correctly."""
        from ohm.queries import query_stats

        stats = query_stats(test_db)
        assert stats["challenge_ratio"] > 0


class TestChangeFeedQuery:
    """Tests for change feed queries."""

    def test_change_feed_empty_initially(self, test_db):
        """Change feed is empty on fresh database."""
        from ohm.queries import query_change_feed

        results = query_change_feed(test_db)
        assert len(results) == 0
