"""Tests for the OHM CTE graph traversal queries."""

import pytest
import uuid
from datetime import datetime, timezone


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

        # All results should have from_node as a visited node
        visited = {node_a}
        for r in results:
            visited.add(r["from_node"])
            visited.add(r["to_node"])
        # The start node should be the source of at least one edge
        start_edges = [r for r in results if r["from_node"] == node_a]
        assert len(start_edges) >= 1

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

    def test_change_feed_filter_by_node_type(self, test_db):
        """Change feed can be filtered by node_type."""
        from ohm.queries import query_change_feed, create_node

        # Create nodes of different types
        create_node(test_db, label="Big Idea", node_type="idea", created_by="agent_a")
        create_node(test_db, label="Causation", node_type="concept", created_by="agent_a")
        create_node(test_db, label="Recurring", node_type="pattern", created_by="agent_b")

        # Query without filter — should get all changes
        all_changes = query_change_feed(test_db, limit=100)
        assert len(all_changes) >= 3

        # Query with node_type filter — should only get concept changes
        concept_changes = query_change_feed(test_db, node_type="concept", limit=100)
        # Concept filter should return fewer results than unfiltered
        assert len(concept_changes) <= len(all_changes)

    def test_change_feed_filter_by_node_type_with_edges(self, test_db):
        """Change feed node_type filter matches edges touching nodes of that type."""
        from ohm.queries import query_change_feed, create_node, create_edge

        # Create nodes of different types
        src = create_node(test_db, label="Source Concept", node_type="concept", created_by="agent_a")
        tgt = create_node(test_db, label="Target Idea", node_type="idea", created_by="agent_a")

        # Create an edge between them
        create_edge(test_db, from_node=src["id"], to_node=tgt["id"],
                     edge_type="CAUSES", layer="L3", created_by="agent_a")

        # Filter by concept — should include the concept node
        concept_changes = query_change_feed(test_db, node_type="concept", limit=100)
        row_ids = {c["row_id"] for c in concept_changes}
        assert src["id"] in row_ids

    def test_change_feed_node_type_no_match(self, test_db):
        """Change feed with node_type that doesn't match returns empty."""
        from ohm.queries import query_change_feed, create_node

        create_node(test_db, label="Node", node_type="concept", created_by="agent_a")

        # Filter by a type that doesn't exist
        results = query_change_feed(test_db, node_type="equipment", limit=100)
        assert len(results) == 0

    def test_change_feed_filter_by_node_id(self, test_db):
        """Change feed can be filtered by specific node_id."""
        from ohm.queries import query_change_feed, create_node

        # Create nodes
        target = create_node(test_db, label="Target Node", node_type="concept", created_by="agent_a")
        other = create_node(test_db, label="Other Node", node_type="concept", created_by="agent_a")

        # Filter by target node_id — should only get that node's changes
        target_changes = query_change_feed(test_db, node_id=target["id"], limit=100)
        row_ids = {c["row_id"] for c in target_changes}
        assert target["id"] in row_ids
        # Other node should not be in results for this specific node_id
        assert other["id"] not in row_ids

    def test_change_feed_filter_by_node_id_with_edges(self, test_db):
        """Change feed node_id filter matches edges touching that node."""
        from ohm.queries import query_change_feed, create_node, create_edge

        # Create two nodes and connect them
        node_a = create_node(test_db, label="Node A", node_type="concept", created_by="agent_a")
        node_b = create_node(test_db, label="Node B", node_type="concept", created_by="agent_a")
        edge = create_edge(test_db, from_node=node_a["id"], to_node=node_b["id"],
                           edge_type="CAUSES", layer="L3", created_by="agent_a")

        # Filter by node_a's id — should include the edge too (touches node_a)
        node_a_changes = query_change_feed(test_db, node_id=node_a["id"], limit=100)
        row_ids = {c["row_id"] for c in node_a_changes}
        assert node_a["id"] in row_ids
        assert edge["id"] in row_ids  # Edge touches node_a


class TestDiffQuery:
    """Tests for the ohm diff query (OHM-xgm.3)."""

    def test_diff_empty_range(self, test_db):
        """Diff on empty database returns zero changes."""
        from ohm.queries import query_diff

        result = query_diff(test_db, "2020-01-01T00:00:00", "2020-01-02T00:00:00")
        assert result["summary"]["total_changes"] == 0

    def test_diff_finds_new_nodes(self, test_db, sample_graph_small):
        """Diff finds nodes created within the time range."""
        from ohm.queries import query_diff

        # Use a wide range that covers the sample graph creation
        result = query_diff(test_db, "2020-01-01T00:00:00", "2030-01-01T00:00:00")
        assert result["summary"]["nodes_added"] >= 3  # a, b, c
        assert result["summary"]["edges_added"] >= 2  # a->b, b->c

    def test_diff_layer_filter(self, test_db, sample_graph_medium):
        """Diff with layer filter only returns edges in that layer."""
        from ohm.queries import query_diff

        result = query_diff(
            test_db, "2020-01-01T00:00:00", "2030-01-01T00:00:00", layer="L3",
        )
        for edge in result["edges_added"]:
            assert edge["layer"] == "L3"

    def test_diff_agent_filter(self, test_db, sample_graph_small):
        """Diff with agent filter only returns changes by that agent."""
        from ohm.queries import query_diff

        result = query_diff(
            test_db, "2020-01-01T00:00:00", "2030-01-01T00:00:00",
            agent_name="test_agent",
        )
        for node in result["nodes_added"]:
            assert node["created_by"] == "test_agent"

    def test_diff_narrow_range_empty(self, test_db, sample_graph_small):
        """Diff with a narrow range in the past returns no changes."""
        from ohm.queries import query_diff

        result = query_diff(test_db, "2020-01-01T00:00:00", "2020-01-01T00:00:01")
        assert result["summary"]["total_changes"] == 0

    def test_diff_has_summary(self, test_db, sample_graph_small):
        """Diff result includes a summary with counts."""
        from ohm.queries import query_diff

        result = query_diff(test_db, "2020-01-01T00:00:00", "2030-01-01T00:00:00")
        assert "summary" in result
        assert "nodes_added" in result["summary"]
        assert "edges_added" in result["summary"]
        assert "total_changes" in result["summary"]


class TestSnapshotQuery:
    """Tests for the ohm snapshot query (OHM-xgm.2)."""

    def test_snapshot_empty_db(self, test_db):
        """Snapshot on empty database returns zero items."""
        from ohm.queries import query_snapshot

        result = query_snapshot(test_db, "2030-01-01T00:00:00")
        assert result["summary"]["nodes"] == 0
        assert result["summary"]["edges"] == 0

    def test_snapshot_finds_existing_nodes(self, test_db, sample_graph_small):
        """Snapshot finds nodes that existed at the timestamp."""
        from ohm.queries import query_snapshot

        result = query_snapshot(test_db, "2030-01-01T00:00:00")
        assert result["summary"]["nodes"] >= 3  # a, b, c
        assert result["summary"]["edges"] >= 2  # a->b, b->c

    def test_snapshot_before_creation_empty(self, test_db, sample_graph_small):
        """Snapshot before any nodes were created returns empty."""
        from ohm.queries import query_snapshot

        result = query_snapshot(test_db, "2020-01-01T00:00:00")
        assert result["summary"]["nodes"] == 0
        assert result["summary"]["edges"] == 0

    def test_snapshot_single_node(self, test_db, sample_graph_small):
        """Snapshot with --node filter returns only that node."""
        from ohm.queries import query_snapshot

        node_a = sample_graph_small["nodes"]["a"]
        result = query_snapshot(test_db, "2030-01-01T00:00:00", node_id=node_a)
        assert result["summary"]["nodes"] == 1
        assert result["nodes"][0]["id"] == node_a

    def test_snapshot_single_edge(self, test_db, sample_graph_small):
        """Snapshot with --edge filter returns only that edge."""
        from ohm.queries import query_snapshot

        edge_ab = sample_graph_small["edges"]["ab"]
        result = query_snapshot(test_db, "2030-01-01T00:00:00", edge_id=edge_ab)
        assert result["summary"]["edges"] == 1
        assert result["edges"][0]["id"] == edge_ab

    def test_snapshot_has_timestamp(self, test_db, sample_graph_small):
        """Snapshot result includes the requested timestamp."""
        from ohm.queries import query_snapshot

        ts = "2025-06-15T12:00:00"
        result = query_snapshot(test_db, ts)
        assert result["timestamp"] == ts


class TestDecayQuery:
    """Tests for confidence decay (OHM-9hx.5)."""

    def test_decay_finds_stale_l4_edges(self, test_db, sample_graph_small):
        """L4 edges with low effective confidence are flagged as stale."""
        from ohm.queries import query_stale_edges

        # Create an old L4 edge with confidence 0.8
        node_a = sample_graph_small["nodes"]["a"]
        node_b = sample_graph_small["nodes"]["b"]
        test_db.execute(
            """INSERT INTO ohm_edges
               (id, from_node, to_node, layer, edge_type, created_by, confidence,
                created_at)
               VALUES (?, ?, ?, 'L4', 'CAUSES', 'test_agent', 0.8,
                       ?::TIMESTAMP - INTERVAL '40 days')""",
            [f"old_l4_{uuid.uuid4().hex[:6]}", node_a, node_b,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
        )

        # With 30-day half-life, 0.8 confidence decays to 0.8 * 0.5^(40/30) ≈ 0.4
        stale = query_stale_edges(test_db, stale_threshold=0.5)
        assert any(e["layer"] == "L4" for e in stale)

    def test_decay_excludes_l1_l2(self, test_db, sample_graph_small):
        """L1/L2 edges are never stale (infinite half-life)."""
        from ohm.queries import query_stale_edges

        node_a = sample_graph_small["nodes"]["a"]
        node_b = sample_graph_small["nodes"]["b"]
        test_db.execute(
            """INSERT INTO ohm_edges
               (id, from_node, to_node, layer, edge_type, created_by, confidence,
                created_at)
               VALUES (?, ?, ?, 'L1', 'CITATION', 'test_agent', 0.8,
                       ?::TIMESTAMP - INTERVAL '1000 days')""",
            [f"old_l1_{uuid.uuid4().hex[:6]}", node_a, node_b,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
        )

        stale = query_stale_edges(test_db, stale_threshold=0.5)
        assert not any(e["layer"] == "L1" for e in stale)

    def test_apply_decay_dry_run(self, test_db, sample_graph_small):
        """apply_confidence_decay dry-run does not update database."""
        from ohm.queries import apply_confidence_decay

        node_a = sample_graph_small["nodes"]["a"]
        node_b = sample_graph_small["nodes"]["b"]
        edge_id = f"decay_test_{uuid.uuid4().hex[:6]}"
        test_db.execute(
            """INSERT INTO ohm_edges
               (id, from_node, to_node, layer, edge_type, created_by, confidence,
                created_at)
               VALUES (?, ?, ?, 'L4', 'CAUSES', 'test_agent', 0.8,
                       ?::TIMESTAMP - INTERVAL '40 days')""",
            [edge_id, node_a, node_b,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
        )

        result = apply_confidence_decay(test_db, stale_threshold=0.5, dry_run=True)

        # Should have found decayed edges but not updated
        assert result["updated"] == 0
        assert len(result["decayed"]) >= 1

        # Verify original confidence unchanged (use approximate comparison)
        row = test_db.execute(
            "SELECT confidence FROM ohm_edges WHERE id = ?", [edge_id]
        ).fetchone()
        assert abs(row[0] - 0.8) < 0.0001

    def test_apply_decay_updates_confidence(self, test_db, sample_graph_small):
        """apply_confidence_decay updates stale edge confidence."""
        from ohm.queries import apply_confidence_decay

        node_a = sample_graph_small["nodes"]["a"]
        node_b = sample_graph_small["nodes"]["b"]
        edge_id = f"decay_live_{uuid.uuid4().hex[:6]}"
        test_db.execute(
            """INSERT INTO ohm_edges
               (id, from_node, to_node, layer, edge_type, created_by, confidence,
                created_at)
               VALUES (?, ?, ?, 'L4', 'CAUSES', 'test_agent', 0.8,
                       ?::TIMESTAMP - INTERVAL '40 days')""",
            [edge_id, node_a, node_b,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
        )

        result = apply_confidence_decay(test_db, stale_threshold=0.5, dry_run=False)

        assert result["updated"] >= 1

        # Verify confidence was updated
        row = test_db.execute(
            "SELECT confidence FROM ohm_edges WHERE id = ?", [edge_id]
        ).fetchone()
        assert row[0] < 0.8  # Should be decayed

    def test_apply_decay_layer_filter(self, test_db, sample_graph_small):
        """apply_confidence_decay with layer filter only decays that layer."""
        from ohm.queries import apply_confidence_decay

        node_a = sample_graph_small["nodes"]["a"]
        node_b = sample_graph_small["nodes"]["b"]
        # Create stale L4 and L3 edges
        l4_id = f"l4_filter_{uuid.uuid4().hex[:6]}"
        l3_id = f"l3_filter_{uuid.uuid4().hex[:6]}"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        test_db.execute(
            """INSERT INTO ohm_edges
               (id, from_node, to_node, layer, edge_type, created_by, confidence,
                created_at)
               VALUES (?, ?, ?, 'L4', 'CAUSES', 'test_agent', 0.8, ?::TIMESTAMP - INTERVAL '40 days')""",
            [l4_id, node_a, node_b, now],
        )
        test_db.execute(
            """INSERT INTO ohm_edges
               (id, from_node, to_node, layer, edge_type, created_by, confidence,
                created_at)
               VALUES (?, ?, ?, 'L3', 'CAUSES', 'test_agent', 0.8, ?::TIMESTAMP - INTERVAL '100 days')""",
            [l3_id, node_a, node_b, now],
        )

        # Only decay L4
        result = apply_confidence_decay(test_db, stale_threshold=0.5, layer="L4", dry_run=False)

        assert result["updated"] >= 1
        # Verify L4 was updated but L3 was not touched
        l4_row = test_db.execute("SELECT confidence FROM ohm_edges WHERE id = ?", [l4_id]).fetchone()
        l3_row = test_db.execute("SELECT confidence FROM ohm_edges WHERE id = ?", [l3_id]).fetchone()
        assert l4_row[0] < 0.8
        assert abs(l3_row[0] - 0.8) < 0.0001  # L3 not decayed (100-day half-life still above threshold)


class TestProbabilityCascade:
    """Tests for probability-weighted edges and cascade scenarios (OHM-af8.1)."""

    def test_create_edge_with_probability(self, test_db):
        """Edge can be created with a probability field."""
        from ohm.queries import create_node, create_edge

        supplier = create_node(test_db, label="Supplier A", node_type="system", created_by="test_agent")
        factory = create_node(test_db, label="Factory B", node_type="system", created_by="test_agent")

        edge = create_edge(
            test_db,
            from_node=supplier["id"],
            to_node=factory["id"],
            layer="L3",
            edge_type="EXPECTED_LIKELIHOOD",
            created_by="test_agent",
            confidence=0.8,
            probability=0.2,
        )

        assert edge["probability"] == pytest.approx(0.2)
        assert edge["confidence"] == pytest.approx(0.8)
        # probability and confidence are distinct
        assert edge["probability"] != edge["confidence"]

    def test_create_edge_probability_none(self, test_db):
        """Edge without probability defaults to None."""
        from ohm.queries import create_node, create_edge

        a = create_node(test_db, label="Node A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="Node B", node_type="concept", created_by="test_agent")

        edge = create_edge(
            test_db,
            from_node=a["id"],
            to_node=b["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="test_agent",
            confidence=0.7,
        )

        assert edge["probability"] is None

    def test_cascade_scenario_downstream(self, test_db):
        """Cascade scenario computes downstream failure probabilities."""
        from ohm.queries import create_node, create_edge, query_cascade_scenario

        # Build a supply chain: supplier → factory → distributor → retailer
        supplier = create_node(test_db, label="Supplier", node_type="system", created_by="test_agent")
        factory = create_node(test_db, label="Factory", node_type="system", created_by="test_agent")
        distributor = create_node(test_db, label="Distributor", node_type="system", created_by="test_agent")
        retailer = create_node(test_db, label="Retailer", node_type="system", created_by="test_agent")

        # Supplier → Factory at 20% disruption probability
        create_edge(test_db, from_node=supplier["id"], to_node=factory["id"],
                     layer="L3", edge_type="EXPECTED_LIKELIHOOD",
                     created_by="test_agent", confidence=0.8, probability=0.2)
        # Factory → Distributor at 50% pass-through
        create_edge(test_db, from_node=factory["id"], to_node=distributor["id"],
                     layer="L3", edge_type="CAUSES",
                     created_by="test_agent", confidence=0.5)
        # Distributor → Retailer at 80% pass-through (DEPENDS_ON is L4)
        create_edge(test_db, from_node=distributor["id"], to_node=retailer["id"],
                     layer="L4", edge_type="DEPENDS_ON",
                     created_by="test_agent", confidence=0.8)

        # Start cascade from supplier with 100% failure
        results = query_cascade_scenario(test_db, supplier["id"], failure_probability=1.0)

        assert len(results) >= 3  # factory, distributor, retailer

        # Find each node in results
        by_node = {r["node_id"]: r for r in results}

        # Factory: 1.0 * 0.2 = 0.2
        assert by_node[factory["id"]]["failure_probability"] == pytest.approx(0.2)
        # Distributor: 0.2 * 0.5 = 0.1
        assert by_node[distributor["id"]]["failure_probability"] == pytest.approx(0.1)
        # Retailer: 0.1 * 0.8 = 0.08
        assert by_node[retailer["id"]]["failure_probability"] == pytest.approx(0.08)

    def test_cascade_scenario_uses_confidence_when_no_probability(self, test_db):
        """When probability is None, cascade uses confidence as fallback."""
        from ohm.queries import create_node, create_edge, query_cascade_scenario

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")

        # Edge with confidence=0.6 but no probability
        create_edge(test_db, from_node=a["id"], to_node=b["id"],
                     layer="L3", edge_type="CAUSES",
                     created_by="test_agent", confidence=0.6)

        results = query_cascade_scenario(test_db, a["id"], failure_probability=1.0)

        assert len(results) == 1
        assert results[0]["failure_probability"] == pytest.approx(0.6)

    def test_cascade_scenario_max_depth(self, test_db):
        """Cascade respects max_depth limit."""
        from ohm.queries import create_node, create_edge, query_cascade_scenario

        # Build a chain of 5 nodes
        nodes = []
        for i in range(5):
            n = create_node(test_db, label=f"Node_{i}", node_type="concept", created_by="test_agent")
            nodes.append(n)

        for i in range(4):
            create_edge(test_db, from_node=nodes[i]["id"], to_node=nodes[i + 1]["id"],
                         layer="L3", edge_type="CAUSES",
                         created_by="test_agent", confidence=0.5)

        # With max_depth=2, should only reach 2 hops
        results = query_cascade_scenario(test_db, nodes[0]["id"], failure_probability=1.0, max_depth=2)

        assert len(results) == 2  # nodes 1 and 2 only
        assert all(r["depth"] <= 2 for r in results)

    def test_cascade_scenario_no_downstream(self, test_db):
        """Cascade from a leaf node returns empty."""
        from ohm.queries import create_node, query_cascade_scenario

        leaf = create_node(test_db, label="Leaf", node_type="concept", created_by="test_agent")

        results = query_cascade_scenario(test_db, leaf["id"], failure_probability=1.0)

        assert len(results) == 0

    def test_what_if_returns_impact_analysis(self, test_db):
        """what_if returns trigger edge details and downstream impact."""
        from ohm.queries import create_node, create_edge, query_what_if

        supplier = create_node(test_db, label="Supplier", node_type="system", created_by="test_agent")
        factory = create_node(test_db, label="Factory", node_type="system", created_by="test_agent")
        distributor = create_node(test_db, label="Distributor", node_type="system", created_by="test_agent")

        edge = create_edge(test_db, from_node=supplier["id"], to_node=factory["id"],
                            layer="L3", edge_type="EXPECTED_LIKELIHOOD",
                            created_by="test_agent", confidence=0.8, probability=0.3)

        create_edge(test_db, from_node=factory["id"], to_node=distributor["id"],
                     layer="L3", edge_type="CAUSES",
                     created_by="test_agent", confidence=0.5)

        result = query_what_if(test_db, edge["id"])

        assert result["trigger_edge"]["id"] == edge["id"]
        assert result["trigger_probability"] == pytest.approx(0.3)
        assert result["affected_nodes"] >= 1
        assert len(result["downstream_impact"]) >= 1

    def test_what_if_nonexistent_edge(self, test_db):
        """what_if on nonexistent edge raises ValueError."""
        import pytest
        from ohm.queries import query_what_if

        with pytest.raises(ValueError, match="Edge not found"):
            query_what_if(test_db, "nonexistent_edge_id")

    def test_cascade_scenario_path_tracking(self, test_db):
        """Cascade results include the path chain to each node."""
        from ohm.queries import create_node, create_edge, query_cascade_scenario

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")
        c = create_node(test_db, label="C", node_type="concept", created_by="test_agent")

        create_edge(test_db, from_node=a["id"], to_node=b["id"],
                     layer="L3", edge_type="CAUSES",
                     created_by="test_agent", confidence=0.5)
        create_edge(test_db, from_node=b["id"], to_node=c["id"],
                     layer="L3", edge_type="CAUSES",
                     created_by="test_agent", confidence=0.5)

        results = query_cascade_scenario(test_db, a["id"], failure_probability=1.0)

        # Node C should have path [a, b, c]
        c_result = next(r for r in results if r["node_id"] == c["id"])
        assert a["id"] in c_result["path"]
        assert b["id"] in c_result["path"]
        assert c["id"] in c_result["path"]

    def test_sdk_cascade_scenario(self, test_db):
        """SDK cascade_scenario method works correctly."""
        from ohm.sdk import Graph

        g = Graph(test_db, actor="test_agent")
        supplier = g.create_node("Supplier X", node_type="system")
        factory = g.create_node("Factory Y", node_type="system")

        g.create_edge(
            from_node=supplier["id"], to_node=factory["id"],
            edge_type="EXPECTED_LIKELIHOOD", layer="L3",
            confidence=0.8, probability=0.25,
        )

        results = g.cascade_scenario(supplier["id"], failure_probability=1.0)

        assert len(results) == 1
        assert results[0]["failure_probability"] == pytest.approx(0.25)

    def test_sdk_what_if(self, test_db):
        """SDK what_if method works correctly."""
        from ohm.sdk import Graph

        g = Graph(test_db, actor="test_agent")
        supplier = g.create_node("Supplier Z", node_type="system")
        factory = g.create_node("Factory W", node_type="system")

        edge = g.create_edge(
            from_node=supplier["id"], to_node=factory["id"],
            edge_type="EXPECTED_LIKELIHOOD", layer="L3",
            confidence=0.9, probability=0.15,
        )

        result = g.what_if(edge["id"])

        assert result["trigger_edge"]["id"] == edge["id"]
        assert result["trigger_probability"] == pytest.approx(0.15)
        assert "downstream_impact" in result


class TestCreateBatch:
    """Tests for create_batch() combined node+edge creation (OHM-1m3)."""

    def test_create_batch_nodes_only(self, test_db):
        """create_batch() creates nodes without edges."""
        from ohm.queries import create_batch

        result = create_batch(
            test_db,
            nodes=[
                {"label": "Node A", "node_type": "concept"},
                {"label": "Node B", "node_type": "source"},
            ],
            created_by="test_agent",
        )
        assert result["nodes_created"] == 2
        assert result["edges_created"] == 0
        assert len(result["nodes"]) == 2
        assert result["nodes"][0]["label"] == "Node A"

    def test_create_batch_edges_only(self, test_db):
        """create_batch() creates edges without new nodes."""
        from ohm.queries import create_node, create_batch

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")

        result = create_batch(
            test_db,
            edges=[
                {"from_node": a["id"], "to_node": b["id"], "edge_type": "CAUSES", "layer": "L3"},
            ],
            created_by="test_agent",
        )
        assert result["nodes_created"] == 0
        assert result["edges_created"] == 1
        assert len(result["edges"]) == 1

    def test_create_batch_nodes_and_edges(self, test_db):
        """create_batch() creates both nodes and edges."""
        from ohm.queries import create_batch

        result = create_batch(
            test_db,
            nodes=[
                {"label": "Event", "node_type": "event"},
                {"label": "Source", "node_type": "source"},
            ],
            created_by="test_agent",
        )
        assert result["nodes_created"] == 2
        node_ids = [n["id"] for n in result["nodes"]]

        # Now create edges between the nodes
        result2 = create_batch(
            test_db,
            edges=[
                {"from_node": node_ids[1], "to_node": node_ids[0], "edge_type": "REFERENCES", "layer": "L2"},
            ],
            created_by="test_agent",
        )
        assert result2["edges_created"] == 1

    def test_create_batch_empty(self, test_db):
        """create_batch() with no nodes or edges returns zeros."""
        from ohm.queries import create_batch

        result = create_batch(test_db, created_by="test_agent")
        assert result["nodes_created"] == 0
        assert result["edges_created"] == 0
        assert result["nodes"] == []
        assert result["edges"] == []

    def test_create_batch_populates_change_feed(self, test_db):
        """create_batch() populates change feed for each item."""
        from ohm.queries import create_batch

        result = create_batch(
            test_db,
            nodes=[
                {"label": "CF1", "node_type": "concept"},
                {"label": "CF2", "node_type": "concept"},
            ],
            created_by="test_agent",
        )
        assert result["nodes_created"] == 2
        # Verify change feed entries exist for each node
        rows = test_db.execute(
            "SELECT row_id FROM ohm_change_feed WHERE table_name = 'ohm_nodes' ORDER BY occurred_at DESC"
        ).fetchall()
        created_ids = {n["id"] for n in result["nodes"]}
        feed_ids = {r[0] for r in rows}
        assert created_ids.issubset(feed_ids)
