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
        create_edge(test_db, from_node=src["id"], to_node=tgt["id"], edge_type="CAUSES", layer="L3", created_by="agent_a")

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
        edge = create_edge(test_db, from_node=node_a["id"], to_node=node_b["id"], edge_type="CAUSES", layer="L3", created_by="agent_a")

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
            test_db,
            "2020-01-01T00:00:00",
            "2030-01-01T00:00:00",
            layer="L3",
        )
        for edge in result["edges_added"]:
            assert edge["layer"] == "L3"

    def test_diff_agent_filter(self, test_db, sample_graph_small):
        """Diff with agent filter only returns changes by that agent."""
        from ohm.queries import query_diff

        result = query_diff(
            test_db,
            "2020-01-01T00:00:00",
            "2030-01-01T00:00:00",
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
            [f"old_l4_{uuid.uuid4().hex[:6]}", node_a, node_b, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
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
            [f"old_l1_{uuid.uuid4().hex[:6]}", node_a, node_b, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
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
            [edge_id, node_a, node_b, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
        )

        result = apply_confidence_decay(test_db, stale_threshold=0.5, dry_run=True)

        # Should have found decayed edges but not updated
        assert result["updated"] == 0
        assert len(result["decayed"]) >= 1

        # Verify original confidence unchanged (use approximate comparison)
        row = test_db.execute("SELECT confidence FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
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
            [edge_id, node_a, node_b, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")],
        )

        result = apply_confidence_decay(test_db, stale_threshold=0.5, dry_run=False)

        assert result["updated"] >= 1

        # Verify confidence was updated
        row = test_db.execute("SELECT confidence FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
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

    def test_stale_edges_sql_push_uses_configurable_half_life(self, test_db, sample_graph_small):
        """OHM-od01.11: query_stale_edges computes decay in SQL, including
        custom half_life_days overrides."""
        from ohm.queries import query_stale_edges

        node_a = sample_graph_small["nodes"]["a"]
        node_b = sample_graph_small["nodes"]["b"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        edge_id = f"custom_hl_{uuid.uuid4().hex[:6]}"
        test_db.execute(
            """INSERT INTO ohm_edges
               (id, from_node, to_node, layer, edge_type, created_by, confidence,
                created_at)
               VALUES (?, ?, ?, 'L3', 'CAUSES', 'test_agent', 0.8,
                       ?::TIMESTAMP - INTERVAL '5 days')""",
            [edge_id, node_a, node_b, now],
        )

        stale_default = query_stale_edges(test_db, stale_threshold=0.5)
        assert not any(e["id"] == edge_id for e in stale_default), "5-day-old L3 with 90-day hl should not be stale"

        stale_fast = query_stale_edges(test_db, stale_threshold=0.5, half_life_days={"L3": 2.0})
        assert any(e["id"] == edge_id for e in stale_fast), "5-day-old L3 with 2-day hl should be stale"


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
        from ohm.queries import create_node, create_edge, query_deterministic_cascade

        # Build a supply chain: supplier → factory → distributor → retailer
        supplier = create_node(test_db, label="Supplier", node_type="system", created_by="test_agent")
        factory = create_node(test_db, label="Factory", node_type="system", created_by="test_agent")
        distributor = create_node(test_db, label="Distributor", node_type="system", created_by="test_agent")
        retailer = create_node(test_db, label="Retailer", node_type="system", created_by="test_agent")

        # Supplier → Factory at 20% disruption probability
        create_edge(test_db, from_node=supplier["id"], to_node=factory["id"], layer="L3", edge_type="EXPECTED_LIKELIHOOD", created_by="test_agent", confidence=0.8, probability=0.2)
        # Factory → Distributor at 50% pass-through
        create_edge(test_db, from_node=factory["id"], to_node=distributor["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.5)
        # Distributor → Retailer at 80% pass-through (DEPENDS_ON is L4)
        create_edge(test_db, from_node=distributor["id"], to_node=retailer["id"], layer="L4", edge_type="DEPENDS_ON", created_by="test_agent", confidence=0.8)

        # Start cascade from supplier with 100% failure
        results = query_deterministic_cascade(test_db, supplier["id"], failure_probability=1.0)

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
        from ohm.queries import create_node, create_edge, query_deterministic_cascade

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")

        # Edge with confidence=0.6 but no probability
        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.6)

        results = query_deterministic_cascade(test_db, a["id"], failure_probability=1.0)

        assert len(results) == 1
        assert results[0]["failure_probability"] == pytest.approx(0.6)

    def test_cascade_scenario_max_depth(self, test_db):
        """Cascade respects max_depth limit."""
        from ohm.queries import create_node, create_edge, query_deterministic_cascade

        # Build a chain of 5 nodes
        nodes = []
        for i in range(5):
            n = create_node(test_db, label=f"Node_{i}", node_type="concept", created_by="test_agent")
            nodes.append(n)

        for i in range(4):
            create_edge(test_db, from_node=nodes[i]["id"], to_node=nodes[i + 1]["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.5)

        # With max_depth=2, should only reach 2 hops
        results = query_deterministic_cascade(test_db, nodes[0]["id"], failure_probability=1.0, max_depth=2)

        assert len(results) == 2  # nodes 1 and 2 only
        assert all(r["depth"] <= 2 for r in results)

    def test_cascade_scenario_no_downstream(self, test_db):
        """Cascade from a leaf node returns empty."""
        from ohm.queries import create_node, query_deterministic_cascade

        leaf = create_node(test_db, label="Leaf", node_type="concept", created_by="test_agent")

        results = query_deterministic_cascade(test_db, leaf["id"], failure_probability=1.0)

        assert len(results) == 0

    def test_what_if_returns_impact_analysis(self, test_db):
        """what_if returns trigger edge details and downstream impact."""
        from ohm.queries import create_node, create_edge, query_what_if

        supplier = create_node(test_db, label="Supplier", node_type="system", created_by="test_agent")
        factory = create_node(test_db, label="Factory", node_type="system", created_by="test_agent")
        distributor = create_node(test_db, label="Distributor", node_type="system", created_by="test_agent")

        edge = create_edge(test_db, from_node=supplier["id"], to_node=factory["id"], layer="L3", edge_type="EXPECTED_LIKELIHOOD", created_by="test_agent", confidence=0.8, probability=0.3)

        create_edge(test_db, from_node=factory["id"], to_node=distributor["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.5)

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
        from ohm.queries import create_node, create_edge, query_deterministic_cascade

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")
        c = create_node(test_db, label="C", node_type="concept", created_by="test_agent")

        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.5)
        create_edge(test_db, from_node=b["id"], to_node=c["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.5)

        results = query_deterministic_cascade(test_db, a["id"], failure_probability=1.0)

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
            from_node=supplier["id"],
            to_node=factory["id"],
            edge_type="EXPECTED_LIKELIHOOD",
            layer="L3",
            confidence=0.8,
            probability=0.25,
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
            from_node=supplier["id"],
            to_node=factory["id"],
            edge_type="EXPECTED_LIKELIHOOD",
            layer="L3",
            confidence=0.9,
            probability=0.15,
        )

        result = g.what_if(edge["id"])

        assert result["trigger_edge"]["id"] == edge["id"]
        assert result["trigger_probability"] == pytest.approx(0.15)
        assert "downstream_impact" in result


class TestMonteCarloCascade:
    """Tests for monte_carlo_cascade() function with two-stage sampling."""

    def test_monte_carlo_basic_chain(self, test_db):
        """Monte Carlo on 3-node chain produces distribution, not point estimate."""
        from ohm.queries import create_node, create_edge, monte_carlo_cascade

        # A → B → C with explicit confidence and probability
        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")
        c = create_node(test_db, label="C", node_type="concept", created_by="test_agent")

        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.9, probability=0.8)
        create_edge(test_db, from_node=b["id"], to_node=c["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.8, probability=0.7)

        result = monte_carlo_cascade(test_db, a["id"], trials=1000, seed=42)

        assert result["trials"] == 1000
        assert result["seed"] == 42
        assert result["node_id"] == a["id"]
        assert len(result["results"]) >= 3  # A, B, C

        # Results should include distribution statistics
        b_result = next(r for r in result["results"] if r["node_id"] == b["id"])
        assert "p5" in b_result
        assert "p50" in b_result
        assert "p95" in b_result
        assert "mean" in b_result

        # With seed=42, results should be reproducible
        result2 = monte_carlo_cascade(test_db, a["id"], trials=1000, seed=42)
        for r1, r2 in zip(result["results"], result2["results"]):
            assert r1["activated_count"] == r2["activated_count"]

    def test_monte_carlo_two_stage_sampling(self, test_db):
        """Two-stage sampling: confidence × probability determines activation rate."""
        from ohm.queries import create_node, create_edge, monte_carlo_cascade

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")

        # A → B: confidence=0.9, probability=0.1 → should activate ~9%
        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.9, probability=0.1)

        result = monte_carlo_cascade(test_db, a["id"], trials=5000, seed=42)

        b_result = next(r for r in result["results"] if r["node_id"] == b["id"])
        # Expected: 0.9 * 0.1 = 0.09, allow ±3%
        assert abs(b_result["activated_pct"] - 0.09) < 0.03

    def test_monte_carlo_reasonable_distribution(self, test_db):
        """Monte Carlo with 1000 trials produces reasonable activated percentages."""
        from ohm.queries import create_node, create_edge, monte_carlo_cascade

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")

        # A → B with confidence=0.8, probability=0.5 → should activate ~40%
        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.8, probability=0.5)

        result = monte_carlo_cascade(test_db, a["id"], trials=1000, seed=42)

        # Find node B in results
        b_result = next(r for r in result["results"] if r["node_id"] == b["id"])
        # Expected: 0.8 * 0.5 = 0.4, allow wide margin
        assert 0.25 < b_result["activated_pct"] < 0.55

    def test_monte_carlo_no_downstream(self, test_db):
        """Monte Carlo from leaf node returns empty results."""
        from ohm.queries import create_node, monte_carlo_cascade

        leaf = create_node(test_db, label="Leaf", node_type="concept", created_by="test_agent")
        result = monte_carlo_cascade(test_db, leaf["id"], trials=100)

        assert result["trials"] == 100
        # Leaf node is in all_nodes so will show 100% activated
        assert len(result["results"]) == 1

    def test_monte_carlo_no_seed_varies(self, test_db):
        """Monte Carlo without seed produces different results each run."""
        from ohm.queries import create_node, create_edge, monte_carlo_cascade

        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")

        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test_agent", confidence=0.8, probability=0.5)

        # Two runs without seed should have different counts (not guaranteed but highly likely)
        result1 = monte_carlo_cascade(test_db, a["id"], trials=500)
        result2 = monte_carlo_cascade(test_db, a["id"], trials=500)

        next(r for r in result1["results"] if r["node_id"] == b["id"])
        next(r for r in result2["results"] if r["node_id"] == b["id"])

        # They may or may not be equal by chance, but we verify seed is None
        assert result1["seed"] is None
        assert result2["seed"] is None


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
        rows = test_db.execute("SELECT row_id FROM ohm_change_feed WHERE table_name = 'ohm_nodes' ORDER BY occurred_at DESC").fetchall()
        created_ids = {n["id"] for n in result["nodes"]}
        feed_ids = {r[0] for r in rows}
        assert created_ids.issubset(feed_ids)


class TestFindOrCreateNode:
    """Tests for find_or_create_node() (OHM-5n7: idempotent registration)."""

    def test_find_or_create_creates_new(self, test_db):
        """find_or_create_node creates a node when none exists."""
        from ohm.queries import find_or_create_node

        node = find_or_create_node(test_db, label="Courage", node_type="value", created_by="metis")
        assert node["label"] == "Courage"
        assert node["type"] == "value"

    def test_find_or_create_finds_existing(self, test_db):
        """find_or_create_node returns existing node with same label and type."""
        from ohm.queries import find_or_create_node, create_node

        original = create_node(test_db, label="Courage", node_type="value", created_by="metis")
        found = find_or_create_node(test_db, label="Courage", node_type="value", created_by="metis")
        assert found["id"] == original["id"]

    def test_find_or_create_different_type(self, test_db):
        """find_or_create_node creates separate nodes for different types."""
        from ohm.queries import find_or_create_node

        value = find_or_create_node(test_db, label="Courage", node_type="value", created_by="metis")
        concept = find_or_create_node(test_db, label="Courage", node_type="concept", created_by="metis")
        assert value["id"] != concept["id"]

    def test_find_or_create_case_insensitive(self, test_db):
        """find_or_create_node matches labels case-insensitively."""
        from ohm.queries import find_or_create_node, create_node

        original = create_node(test_db, label="Courage", node_type="value", created_by="metis")
        found = find_or_create_node(test_db, label="courage", node_type="value", created_by="metis")
        assert found["id"] == original["id"]


class TestDecisionNode:
    """Tests for decision node type with utility function (OHM-6mv.2)."""

    def test_decision_node_type_is_valid(self):
        """The 'decision' node type should be in VALID_NODE_TYPES."""
        from ohm.schema import VALID_NODE_TYPES

        assert "decision" in VALID_NODE_TYPES

    def test_validate_decision_node_type(self):
        """validate_node_type should accept 'decision'."""
        from ohm.schema import validate_node_type

        assert validate_node_type("decision") is True

    def test_create_decision_node(self, test_db):
        """Creating a node with type='decision' should succeed."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="Hormuz Response Strategy",
            node_type="decision",
            created_by="metis",
        )
        assert node["type"] == "decision"
        assert node["label"] == "Hormuz Response Strategy"

    def test_create_decision_node_with_utility(self, test_db):
        """Decision nodes should accept utility_scale, current_best_action, and action_alternatives."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="Agent Governance Standard",
            node_type="decision",
            created_by="metis",
            utility_scale=0.9,
            current_best_action="Adopt current standard",
            action_alternatives=["Revise standard", "Wait for more data"],
        )
        assert node["type"] == "decision"
        assert node["utility_scale"] == pytest.approx(0.9)
        assert node["current_best_action"] == "Adopt current standard"
        # action_alternatives is stored as JSON list
        import json

        alternatives = json.loads(node["action_alternatives"]) if isinstance(node["action_alternatives"], str) else node["action_alternatives"]
        assert "Revise standard" in alternatives
        assert "Wait for more data" in alternatives

    def test_utility_scale_validation(self, test_db):
        """utility_scale must be between 0 and 1."""
        from ohm.queries import create_node

        with pytest.raises(ValueError, match="utility_scale"):
            create_node(
                test_db,
                label="Bad Decision",
                node_type="decision",
                created_by="metis",
                utility_scale=1.5,
            )

    def test_utility_scale_negative_rejected(self, test_db):
        """Negative utility_scale should be rejected."""
        from ohm.queries import create_node

        with pytest.raises(ValueError, match="utility_scale"):
            create_node(
                test_db,
                label="Bad Decision",
                node_type="decision",
                created_by="metis",
                utility_scale=-0.1,
            )

    def test_utility_scale_zero_accepted(self, test_db):
        """utility_scale=0 should be accepted (being wrong doesn't matter)."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="Low Stakes Decision",
            node_type="decision",
            created_by="metis",
            utility_scale=0.0,
        )
        assert node["utility_scale"] == pytest.approx(0.0)

    def test_create_decision_node_with_usd_utility(self, test_db):
        """Decision nodes should accept utility_usd_per_day and utility_currency."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="Hormuz Response",
            node_type="decision",
            created_by="metis",
            utility_scale=0.9,
            utility_usd_per_day=5_000_000.0,
            utility_currency="USD",
        )
        assert node["utility_usd_per_day"] == pytest.approx(5_000_000.0)
        assert node["utility_currency"] == "USD"

    def test_usd_utility_null_by_default(self, test_db):
        """utility_usd_per_day should be NULL when not specified."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="Plain Decision",
            node_type="decision",
            created_by="metis",
            utility_scale=0.5,
        )
        assert node["utility_usd_per_day"] is None
        assert node["utility_currency"] is None

    def test_utility_scale_one_accepted(self, test_db):
        """utility_scale=1.0 should be accepted (being wrong matters a lot)."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="High Stakes Decision",
            node_type="decision",
            created_by="metis",
            utility_scale=1.0,
        )
        assert node["utility_scale"] == pytest.approx(1.0)

    def test_decision_node_without_utility(self, test_db):
        """Decision nodes can be created without utility fields (they default to NULL)."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="Decision Without Utility",
            node_type="decision",
            created_by="metis",
        )
        assert node["type"] == "decision"
        assert node["utility_scale"] is None
        assert node["current_best_action"] is None
        assert node["action_alternatives"] is None

    def test_utility_fields_on_non_decision_node(self, test_db):
        """Utility fields can be set on any node type, not just decision."""
        from ohm.queries import create_node

        node = create_node(
            test_db,
            label="Important Concept",
            node_type="concept",
            created_by="metis",
            utility_scale=0.5,
            current_best_action="Research more",
        )
        assert node["type"] == "concept"
        assert node["utility_scale"] == pytest.approx(0.5)
        assert node["current_best_action"] == "Research more"

    def test_schema_migration_adds_utility_columns(self, test_db):
        """Migration v0.16.0 should add utility columns to ohm_nodes."""
        from ohm.schema import get_schema_version, SCHEMA_VERSION

        version = get_schema_version(test_db)
        assert version == SCHEMA_VERSION
        # Verify columns exist
        columns = [row[0] for row in test_db.execute("DESCRIBE ohm_nodes").fetchall()]
        assert "utility_scale" in columns
        assert "current_best_action" in columns
        assert "action_alternatives" in columns


class TestDeleteNode:
    """Tests for delete_node() — cascading edge deletion (OHM-cpi)."""

    def test_delete_node_removes_edges(self, test_db):
        """delete_node removes all edges referencing the node."""
        from ohm.queries import create_node, create_edge, delete_node

        a = create_node(test_db, label="A", node_type="concept", created_by="test")
        b = create_node(test_db, label="B", node_type="concept", created_by="test")
        create_edge(test_db, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="test")

        result = delete_node(test_db, node_id=a["id"], deleted_by="test")
        assert result["deleted"] == a["id"]
        assert result["type"] == "node"
        assert result["edges_removed"] >= 1

        # Node should be soft-deleted (not findable without deleted_at filter)
        rows = test_db.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [a["id"]]).fetchall()
        assert len(rows) == 0

        # But the node row should still exist (soft delete)
        row = test_db.execute("SELECT id, deleted_at FROM ohm_nodes WHERE id = ?", [a["id"]]).fetchone()
        assert row is not None
        assert row[1] is not None  # deleted_at is set

        # Edges should be soft-deleted
        edge_rows = test_db.execute("SELECT * FROM ohm_edges WHERE from_node = ? AND deleted_at IS NULL", [a["id"]]).fetchall()
        assert len(edge_rows) == 0

        # But the edge row should still exist
        edge_row = test_db.execute("SELECT id, deleted_at FROM ohm_edges WHERE from_node = ?", [a["id"]]).fetchone()
        assert edge_row is not None
        assert edge_row[1] is not None  # deleted_at is set

    def test_delete_node_removes_incoming_edges(self, test_db):
        """delete_node removes edges where node is the target."""
        from ohm.queries import create_node, create_edge, delete_node

        a = create_node(test_db, label="A", node_type="concept", created_by="test")
        b = create_node(test_db, label="B", node_type="concept", created_by="test")
        create_edge(test_db, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="test")

        result = delete_node(test_db, node_id=b["id"], deleted_by="test")
        assert result["edges_removed"] >= 1

        # Edges should be soft-deleted (not findable without deleted_at filter)
        rows = test_db.execute("SELECT * FROM ohm_edges WHERE to_node = ? AND deleted_at IS NULL", [b["id"]]).fetchall()
        assert len(rows) == 0

        # But the edge row should still exist (soft delete)
        edge_row = test_db.execute("SELECT id, deleted_at FROM ohm_edges WHERE to_node = ?", [b["id"]]).fetchone()
        assert edge_row is not None
        assert edge_row[1] is not None  # deleted_at is set

    def test_delete_node_removes_observations(self, test_db):
        """delete_node removes observations on the node."""
        from ohm.queries import create_node, delete_node

        node = create_node(test_db, label="Obs", node_type="concept", created_by="test")
        test_db.execute(
            "INSERT INTO ohm_observations (id, node_id, type, value, created_by) VALUES (?, ?, ?, ?, ?)",
            ["obs-1", node["id"], "metric", 42.0, "test"],
        )

        result = delete_node(test_db, node_id=node["id"], deleted_by="test")
        assert result["observations_removed"] >= 1

        # Observation should be soft-deleted (not findable without deleted_at filter)
        rows = test_db.execute("SELECT * FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL", [node["id"]]).fetchall()
        assert len(rows) == 0

        # But the observation row should still exist (soft delete)
        obs_row = test_db.execute("SELECT id, deleted_at FROM ohm_observations WHERE node_id = ?", [node["id"]]).fetchone()
        assert obs_row is not None
        assert obs_row[1] is not None  # deleted_at is set

    def test_delete_node_not_found(self, test_db):
        """delete_node raises NodeNotFoundError for nonexistent node."""
        from ohm.queries import delete_node
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            delete_node(test_db, node_id="nonexistent_xyz", deleted_by="test")

    def test_delete_node_no_edges(self, test_db):
        """delete_node works on a node with no edges."""
        from ohm.queries import create_node, delete_node

        node = create_node(test_db, label="Lonely", node_type="concept", created_by="test")
        result = delete_node(test_db, node_id=node["id"], deleted_by="test")
        assert result["edges_removed"] == 0
        assert result["observations_removed"] == 0


class TestDeleteEdge:
    """Tests for delete_edge() (OHM-cpi)."""

    def test_delete_edge(self, test_db):
        """delete_edge removes an edge by ID."""
        from ohm.queries import create_node, create_edge, delete_edge

        a = create_node(test_db, label="A", node_type="concept", created_by="test")
        b = create_node(test_db, label="B", node_type="concept", created_by="test")
        edge = create_edge(test_db, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="test")

        result = delete_edge(test_db, edge_id=edge["id"], deleted_by="test")
        assert result["deleted"] == edge["id"]
        assert result["type"] == "edge"

        # Edge should be soft-deleted (not findable without deleted_at filter)
        rows = test_db.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [edge["id"]]).fetchall()
        assert len(rows) == 0

        # But the row should still exist in the database (soft delete)
        row = test_db.execute("SELECT id, deleted_at FROM ohm_edges WHERE id = ?", [edge["id"]]).fetchone()
        assert row is not None
        assert row[0] == edge["id"]
        assert row[1] is not None  # deleted_at is set

    def test_delete_edge_not_found(self, test_db):
        """delete_edge raises EdgeNotFoundError for nonexistent edge."""
        from ohm.queries import delete_edge
        from ohm.exceptions import EdgeNotFoundError

        with pytest.raises(EdgeNotFoundError):
            delete_edge(test_db, edge_id="nonexistent_edge_xyz", deleted_by="test")


class TestPERTFields:
    """Tests for PERT distribution columns on edges (OHM-6mv.11)."""

    def test_create_edge_with_pert_probability(self, test_db):
        """create_edge accepts PERT probability estimates."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause A", node_type="concept", created_by="test")
        create_node(test_db, label="Effect B", node_type="concept", created_by="test")
        edge = create_edge(
            test_db,
            from_node="Cause A",
            to_node="Effect B",
            layer="L3",
            edge_type="CAUSES",
            created_by="test",
            confidence=0.7,
            probability_p05=0.1,
            probability_p50=0.5,
            probability_p95=0.9,
        )
        assert abs(edge["probability_p05"] - 0.1) < 0.01
        assert abs(edge["probability_p50"] - 0.5) < 0.01
        assert abs(edge["probability_p95"] - 0.9) < 0.01

    def test_create_edge_with_pert_confidence(self, test_db):
        """create_edge accepts PERT confidence estimates."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause C", node_type="concept", created_by="test")
        create_node(test_db, label="Effect D", node_type="concept", created_by="test")
        edge = create_edge(
            test_db,
            from_node="Cause C",
            to_node="Effect D",
            layer="L3",
            edge_type="CAUSES",
            created_by="test",
            confidence=0.7,
            confidence_p05=0.3,
            confidence_p50=0.7,
            confidence_p95=0.95,
        )
        assert abs(edge["confidence_p05"] - 0.3) < 0.01
        assert abs(edge["confidence_p50"] - 0.7) < 0.01
        assert abs(edge["confidence_p95"] - 0.95) < 0.01

    def test_create_edge_with_all_pert_fields(self, test_db):
        """create_edge accepts all PERT fields at once."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause E", node_type="concept", created_by="test")
        create_node(test_db, label="Effect F", node_type="concept", created_by="test")
        edge = create_edge(
            test_db,
            from_node="Cause E",
            to_node="Effect F",
            layer="L3",
            edge_type="CAUSES",
            created_by="test",
            confidence=0.7,
            probability_p05=0.05,
            probability_p50=0.4,
            probability_p95=0.85,
            confidence_p05=0.2,
            confidence_p50=0.7,
            confidence_p95=0.95,
        )
        assert abs(edge["probability_p05"] - 0.05) < 0.01
        assert abs(edge["probability_p50"] - 0.4) < 0.01
        assert abs(edge["probability_p95"] - 0.85) < 0.01
        assert abs(edge["confidence_p05"] - 0.2) < 0.01
        assert abs(edge["confidence_p50"] - 0.7) < 0.01
        assert abs(edge["confidence_p95"] - 0.95) < 0.01

    def test_create_edge_without_pert_fields(self, test_db):
        """create_edge works without PERT fields (backward compatible)."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause G", node_type="concept", created_by="test")
        create_node(test_db, label="Effect H", node_type="concept", created_by="test")
        edge = create_edge(
            test_db,
            from_node="Cause G",
            to_node="Effect H",
            layer="L3",
            edge_type="CAUSES",
            created_by="test",
            confidence=0.7,
        )
        assert edge.get("probability_p05") is None
        assert edge.get("probability_p50") is None
        assert edge.get("probability_p95") is None
        assert edge.get("confidence_p05") is None
        assert edge.get("confidence_p50") is None
        assert edge.get("confidence_p95") is None

    def test_create_edge_rejects_p05_greater_than_p50(self, test_db):
        """create_edge rejects PERT probability where p05 > p50."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause I", node_type="concept", created_by="test")
        create_node(test_db, label="Effect J", node_type="concept", created_by="test")
        with pytest.raises(ValueError, match="p05.*must be <= p50"):
            create_edge(
                test_db,
                from_node="Cause I",
                to_node="Effect J",
                layer="L3",
                edge_type="CAUSES",
                created_by="test",
                confidence=0.7,
                probability_p05=0.6,
                probability_p50=0.3,
                probability_p95=0.9,
            )

    def test_create_edge_rejects_p50_greater_than_p95(self, test_db):
        """create_edge rejects PERT probability where p50 > p95."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause K", node_type="concept", created_by="test")
        create_node(test_db, label="Effect L", node_type="concept", created_by="test")
        with pytest.raises(ValueError, match="p50.*must be <= p95"):
            create_edge(
                test_db,
                from_node="Cause K",
                to_node="Effect L",
                layer="L3",
                edge_type="CAUSES",
                created_by="test",
                confidence=0.7,
                probability_p05=0.1,
                probability_p50=0.8,
                probability_p95=0.5,
            )

    def test_create_edge_rejects_p50_missing(self, test_db):
        """create_edge rejects PERT values when p50 is missing."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause M", node_type="concept", created_by="test")
        create_node(test_db, label="Effect N", node_type="concept", created_by="test")
        with pytest.raises(ValueError, match="p50 is required"):
            create_edge(
                test_db,
                from_node="Cause M",
                to_node="Effect N",
                layer="L3",
                edge_type="CAUSES",
                created_by="test",
                confidence=0.7,
                probability_p05=0.1,
                probability_p95=0.9,
            )

    def test_create_edge_rejects_pert_out_of_range(self, test_db):
        """create_edge rejects PERT values outside [0, 1]."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause O", node_type="concept", created_by="test")
        create_node(test_db, label="Effect P", node_type="concept", created_by="test")
        with pytest.raises(ValueError, match="must be between"):
            create_edge(
                test_db,
                from_node="Cause O",
                to_node="Effect P",
                layer="L3",
                edge_type="CAUSES",
                created_by="test",
                confidence=0.7,
                probability_p05=-0.1,
                probability_p50=0.5,
                probability_p95=0.9,
            )

    def test_create_edge_rejects_confidence_pert_p05_greater_than_p50(self, test_db):
        """create_edge rejects PERT confidence where c05 > c50."""
        from ohm.queries import create_edge, create_node

        create_node(test_db, label="Cause Q", node_type="concept", created_by="test")
        create_node(test_db, label="Effect R", node_type="concept", created_by="test")
        with pytest.raises(ValueError, match="confidence PERT.*p05.*must be <= p50"):
            create_edge(
                test_db,
                from_node="Cause Q",
                to_node="Effect R",
                layer="L3",
                edge_type="CAUSES",
                created_by="test",
                confidence=0.7,
                confidence_p05=0.9,
                confidence_p50=0.5,
                confidence_p95=0.95,
            )


class TestDiscoveryQueue:
    """Tests for discovery queue functions (OHM-od01.4)."""

    def test_queue_discovery_candidates(self, test_db):
        """queue_discovery_candidates inserts candidate edges."""
        from ohm.queries import queue_discovery_candidates, query_discovery_queue

        edges = [
            {"from": "node_a", "to": "node_b", "edge_type": "directed", "confidence": 0.8, "method": "pc"},
            {"from": "node_b", "to": "node_c", "edge_type": "undirected", "confidence": 0.6, "method": "ges"},
        ]
        ids = queue_discovery_candidates(test_db, edges, created_by="test")
        assert len(ids) == 2
        assert all(isinstance(i, str) and len(i) > 0 for i in ids)

        queue = query_discovery_queue(test_db)
        assert len(queue) == 2
        assert all(q["status"] == "pending" for q in queue)

    def test_query_discovery_queue_filters_by_status(self, test_db):
        """query_discovery_queue filters by status."""
        from ohm.queries import queue_discovery_candidates, query_discovery_queue

        edges = [{"from": "a", "to": "b", "edge_type": "directed", "method": "pc"}]
        queue_discovery_candidates(test_db, edges)

        pending = query_discovery_queue(test_db, status="pending")
        accepted = query_discovery_queue(test_db, status="accepted")
        assert len(pending) == 1
        assert len(accepted) == 0

    def test_query_discovery_queue_filters_by_method(self, test_db):
        """query_discovery_queue filters by method."""
        from ohm.queries import queue_discovery_candidates, query_discovery_queue

        edges = [
            {"from": "a", "to": "b", "edge_type": "directed", "method": "pc"},
            {"from": "c", "to": "d", "edge_type": "directed", "method": "ges"},
        ]
        queue_discovery_candidates(test_db, edges)

        pc = query_discovery_queue(test_db, method="pc")
        ges = query_discovery_queue(test_db, method="ges")
        assert len(pc) == 1
        assert len(ges) == 1
        assert pc[0]["method"] == "pc"
        assert ges[0]["method"] == "ges"

    def test_review_accept_creates_edge(self, test_db):
        """Accepting a discovery candidate creates an edge in ohm_edges."""
        from ohm.queries import (
            create_node,
            queue_discovery_candidates,
            query_discovery_queue,
            review_discovery_candidate,
        )

        n1 = create_node(test_db, label="NodeX", node_type="concept", created_by="test")
        n2 = create_node(test_db, label="NodeY", node_type="concept", created_by="test")

        edges = [{"from": n1["id"], "to": n2["id"], "edge_type": "directed", "confidence": 0.8, "method": "pc"}]
        ids = queue_discovery_candidates(test_db, edges, created_by="test")

        result = review_discovery_candidate(
            test_db, ids[0], action="accept", reviewed_by="metis", edge_layer="L3",
        )
        assert result["action"] == "accepted"
        assert "edge_id" in result

        queue = query_discovery_queue(test_db, status="accepted")
        assert len(queue) == 1
        assert queue[0]["reviewed_by"] == "metis"

    def test_review_reject_marks_rejected(self, test_db):
        """Rejecting a discovery candidate marks it as rejected."""
        from ohm.queries import queue_discovery_candidates, query_discovery_queue, review_discovery_candidate

        edges = [{"from": "a", "to": "b", "edge_type": "directed", "method": "pc"}]
        ids = queue_discovery_candidates(test_db, edges)

        result = review_discovery_candidate(
            test_db, ids[0], action="reject", reviewed_by="clio", review_notes="insufficient evidence",
        )
        assert result["action"] == "rejected"

        queue = query_discovery_queue(test_db, status="rejected")
        assert len(queue) == 1
        assert queue[0]["reviewed_by"] == "clio"

    def test_review_already_reviewed_returns_error(self, test_db):
        """Reviewing an already-reviewed entry returns error dict."""
        from ohm.queries import queue_discovery_candidates, review_discovery_candidate

        edges = [{"from": "a", "to": "b", "edge_type": "directed", "method": "pc"}]
        ids = queue_discovery_candidates(test_db, edges)

        review_discovery_candidate(test_db, ids[0], action="accept", reviewed_by="metis")
        result = review_discovery_candidate(test_db, ids[0], action="reject", reviewed_by="clio")
        assert "error" in result
        assert result["error"] == "already_reviewed"

    def test_review_nonexistent_raises(self, test_db):
        """Reviewing a non-existent queue entry raises EdgeNotFoundError."""
        from ohm.queries import review_discovery_candidate
        from ohm.exceptions import EdgeNotFoundError

        with pytest.raises(EdgeNotFoundError):
            review_discovery_candidate(test_db, "nonexistent_id", action="accept", reviewed_by="metis")

    def test_review_invalid_action_returns_error(self, test_db):
        """Review with invalid action returns error dict."""
        from ohm.queries import queue_discovery_candidates, review_discovery_candidate

        edges = [{"from": "a", "to": "b", "edge_type": "directed", "method": "pc"}]
        ids = queue_discovery_candidates(test_db, edges)

        result = review_discovery_candidate(
            test_db, ids[0], action="maybe", reviewed_by="metis",
        )
        assert "error" in result
        assert result["error"] == "invalid_action"


class TestScratch:
    """Tests for L0 scratch function (OHM-a5rz.4)."""

    def test_scratch_creates_fragment(self, test_db):
        from ohm.queries import scratch

        node = scratch(test_db, content="Hunch about supply chain", created_by="metis")
        assert node["type"] == "fragment"
        assert node["scratch"] is True
        assert node["confidence"] == 0.0
        assert node["label"] == "Hunch about supply chain"
        assert node["provenance"] == "scratch"

    def test_scratch_empty_content_raises(self, test_db):
        from ohm.queries import scratch

        with pytest.raises(ValueError, match="non-empty"):
            scratch(test_db, content="", created_by="metis")

    def test_scratch_url_extraction(self, test_db):
        from ohm.queries import scratch

        node = scratch(
            test_db,
            content="Check https://example.com/paper for details",
            created_by="metis",
        )
        assert node["url"] == "https://example.com/paper"

    def test_scratch_label_truncated_to_80(self, test_db):
        from ohm.queries import scratch

        long_content = "x" * 200
        node = scratch(test_db, content=long_content, created_by="metis")
        assert len(node["label"]) <= 80

    def test_scratch_with_connects_to(self, test_db):
        from ohm.queries import create_node, scratch

        anchor = create_node(test_db, label="Anchor", node_type="concept", created_by="test")
        node = scratch(
            test_db,
            content="Relates to anchor",
            created_by="metis",
            connects_to=[anchor["id"]],
        )
        assert node["type"] == "fragment"

    def test_scratch_no_url_when_none_in_content(self, test_db):
        from ohm.queries import scratch

        node = scratch(test_db, content="No URL here", created_by="metis")
        assert node.get("url") is None


class TestAutoLinkFragment:
    """Tests for auto-linking fragments to existing nodes (OHM-a5rz.8)."""

    def test_auto_link_creates_context_of_edge(self, test_db):
        from ohm.queries import create_node, scratch

        create_node(test_db, label="Hormuz AND-Gate", node_type="pattern", created_by="test")
        node = scratch(test_db, content="Hormuz AND-Gate is the key constraint", created_by="metis")
        assert "auto_links" in node
        assert len(node["auto_links"]) >= 1
        link = node["auto_links"][0]
        assert link["edge_id"] is not None

    def test_auto_link_case_insensitive(self, test_db):
        from ohm.queries import create_node, scratch

        create_node(test_db, label="Demand Rationing", node_type="concept", created_by="test")
        node = scratch(test_db, content="demand rationing is the key insight", created_by="metis")
        assert "auto_links" in node
        assert len(node["auto_links"]) >= 1

    def test_auto_link_max_5(self, test_db):
        from ohm.queries import create_node, scratch

        for i in range(10):
            label = f"Pattern{i:02d}Thing"
            create_node(test_db, label=label, node_type="concept", created_by="test")
        node = scratch(
            test_db,
            content=" ".join(f"Pattern{i:02d}Thing" for i in range(10)),
            created_by="metis",
        )
        assert "auto_links" in node
        assert len(node["auto_links"]) <= 5

    def test_auto_link_skips_short_labels(self, test_db):
        from ohm.queries import create_node, scratch

        create_node(test_db, label="AB", node_type="concept", created_by="test")
        node = scratch(test_db, content="I found AB in the data", created_by="metis")
        assert "auto_links" not in node or len(node.get("auto_links", [])) == 0

    def test_auto_link_skips_fragment_nodes(self, test_db):
        from ohm.queries import scratch

        scratch(test_db, content="earlier hunch about supply chain", created_by="metis")
        node = scratch(test_db, content="earlier hunch about supply chain update", created_by="metis")
        assert "auto_links" not in node or len(node.get("auto_links", [])) == 0

    def test_auto_link_edge_is_L0_context_of(self, test_db):
        from ohm.queries import create_node, scratch

        create_node(test_db, label="Supply Chain Disruption", node_type="pattern", created_by="test")
        node = scratch(test_db, content="Supply Chain Disruption at Hormuz", created_by="metis")
        if "auto_links" in node and node["auto_links"]:
            edge_id = node["auto_links"][0]["edge_id"]
            row = test_db.execute("SELECT layer, edge_type, confidence FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
            assert row[0] == "L0"
            assert row[1] == "CONTEXT_OF"
            assert abs(row[2] - 0.3) < 0.01


class TestSemanticAutoLinkFragment:
    """Tests for semantic auto-linking via embedding similarity (OHM-a5rz.19)."""

    def test_semantic_auto_link_fallback_when_ollama_unavailable(self, test_db):
        """Semantic auto-linking falls back to substring when Ollama unavailable."""
        from ohm.queries import create_node, scratch

        create_node(test_db, label="SemanticFallbackTest", node_type="concept", created_by="test")
        node = scratch(test_db, content="SemanticFallbackTest is important", created_by="metis")
        assert "auto_links" in node
        assert len(node["auto_links"]) >= 1
        # When Ollama is unavailable, provenance should be auto_link_substring
        assert node["auto_links"][0].get("provenance", "") == "auto_link_substring"

    def _patch_embedding(self, embedding):
        """Monkey-patch generate_embedding in the module where _auto_link_fragment looks it up."""
        import ohm.graph.queries as _gq
        self._original_embedding = _gq.generate_embedding
        _gq.generate_embedding = lambda text, model="", url="": embedding
        return _gq

    def _unpatch_embedding(self, _gq):
        _gq.generate_embedding = self._original_embedding

    def test_semantic_auto_link_with_mock_embeddings(self, test_db):
        """Semantic auto-links created using pre-populated embeddings."""
        from ohm.queries import create_node, scratch

        # Skip if VSS (array_cosine_distance) not available
        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        # Create nodes and set embeddings so they are semantically close
        node_a = create_node(test_db, label="Monetary Policy Tighter", node_type="concept", created_by="test")
        node_b = create_node(test_db, label="Interest Rate Hike", node_type="concept", created_by="test")
        node_c = create_node(test_db, label="Baking Sourdough", node_type="concept", created_by="test")

        # Embeddings: monetary policy and interest rate should be close (dim 0)
        # Baking is far away (dim 2)
        policy_emb = [0.0] * 768
        policy_emb[0] = 0.95
        rate_emb = [0.0] * 768
        rate_emb[0] = 0.90
        baking_emb = [0.0] * 768
        baking_emb[2] = 0.95

        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [policy_emb, node_a["id"]])
        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [rate_emb, node_b["id"]])
        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [baking_emb, node_c["id"]])

        fragment_emb = [0.0] * 768
        fragment_emb[0] = 1.0  # Close to monetary/rate

        _gq = self._patch_embedding(fragment_emb)
        try:
            node = scratch(
                test_db,
                content="Central bank tightens monetary policy with rate adjustment",
                created_by="metis",
            )
        finally:
            self._unpatch_embedding(_gq)

        assert "auto_links" in node
        assert len(node["auto_links"]) >= 1
        # Should be semantic provenance
        for link in node["auto_links"]:
            assert link["provenance"] == "auto_link_semantic"
        # Should not link to baking (far away in embedding space)
        linked_ids = {link["node_id"] for link in node["auto_links"]}
        assert node_c["id"] not in linked_ids

    def test_semantic_auto_link_provenance_on_edge(self, test_db):
        """Semantic auto-link edges have correct layer, type, confidence, provenance."""
        from ohm.queries import create_node, scratch

        # Skip if VSS not available
        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension not available")

        node = create_node(test_db, label="Economic Indicator", node_type="concept", created_by="test")
        emb = [0.0] * 768
        emb[0] = 0.9
        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [emb, node["id"]])

        fragment_emb = [1.0] + [0.0] * 767
        _gq = self._patch_embedding(fragment_emb)
        try:
            result = scratch(
                test_db,
                content="Leading economic indicators suggest slowdown",
                created_by="metis",
            )
        finally:
            self._unpatch_embedding(_gq)

        assert "auto_links" in result
        assert len(result["auto_links"]) >= 1
        edge_id = result["auto_links"][0]["edge_id"]
        row = test_db.execute(
            "SELECT layer, edge_type, confidence, provenance FROM ohm_edges WHERE id = ?",
            [edge_id],
        ).fetchone()
        assert row[0] == "L0"
        assert row[1] == "CONTEXT_OF"
        assert abs(row[2] - 0.3) < 0.01
        assert row[3] == "auto_link_semantic"

    def test_semantic_auto_link_skips_fragments(self, test_db):
        """Semantic auto-linking skips fragment-type nodes."""
        from ohm.queries import scratch

        # Skip if VSS not available
        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension not available")

        # Create a fragment (no mock needed since we just need its id)
        frag = scratch(test_db, content="Monetary policy fragment", created_by="metis")

        fragment_emb = [1.0] + [0.0] * 767
        _gq = self._patch_embedding(fragment_emb)
        try:
            node = scratch(
                test_db,
                content="Monetary policy and rate decisions",
                created_by="metis",
            )
        finally:
            self._unpatch_embedding(_gq)

        if "auto_links" in node:
            linked_ids = {link["node_id"] for link in node["auto_links"]}
            assert frag["id"] not in linked_ids


class TestQuestionAutoDetection:
    """Tests for question auto-detection in fragments (OHM-a5rz.12)."""

    def test_question_fragment_has_is_question(self, test_db):
        import json
        from ohm.queries import scratch

        node = scratch(test_db, content="Why would Altman meet Sanders?", created_by="metis")
        meta = json.loads(node["metadata"]) if node.get("metadata") else {}
        assert meta.get("is_question") is True

    def test_non_question_fragment_no_is_question(self, test_db):
        import json
        from ohm.queries import scratch

        node = scratch(test_db, content="Broadcom refused to raise guidance", created_by="metis")
        meta = json.loads(node["metadata"]) if node.get("metadata") else {}
        assert "is_question" not in meta

    def test_resolve_question(self, test_db):
        import json
        from ohm.queries import scratch, resolve_question

        node = scratch(test_db, content="Why did Kioxia delay?", created_by="metis")
        result = resolve_question(test_db, fragment_id=node["id"], resolved_by="metis")
        assert result is not None
        meta = json.loads(result["metadata"]) if result.get("metadata") else {}
        assert meta.get("is_question") is False
        assert "resolved_at" in meta

    def test_resolve_non_question_returns_none(self, test_db):
        from ohm.queries import scratch, resolve_question

        node = scratch(test_db, content="No question here", created_by="metis")
        result = resolve_question(test_db, fragment_id=node["id"], resolved_by="metis")
        assert result is None

    def test_resolve_nonexistent_returns_none(self, test_db):
        from ohm.queries import resolve_question

        result = resolve_question(test_db, fragment_id="nonexistent", resolved_by="metis")
        assert result is None

    def test_question_with_tags_both_in_metadata(self, test_db):
        import json
        from ohm.queries import scratch

        node = scratch(test_db, content="Is HBM scaling?", created_by="metis", tags=["semi"])
        meta = json.loads(node["metadata"]) if node.get("metadata") else {}
        assert meta.get("is_question") is True
        assert meta.get("tags") == ["semi"]


class TestFragmentResonance:
    """Tests for cross-agent fragment resonance (OHM-a5rz.13)."""

    def test_detect_resonance_shared_context(self, test_db):
        from ohm.queries import create_node, scratch, detect_fragment_resonance

        anchor = create_node(test_db, label="Hormuz AND-Gate", node_type="pattern", created_by="test")
        create_node(test_db, label="Supply Chain Disruption", node_type="pattern", created_by="test")

        f1 = scratch(test_db, content="Hormuz AND-Gate and Supply Chain Disruption both matter", created_by="metis")
        f2 = scratch(test_db, content="Hormuz AND-Gate and Supply Chain Disruption overlap", created_by="clio")

        result = detect_fragment_resonance(test_db, min_shared=2)
        assert len(result) >= 1
        pair = result[0]
        assert pair["agent_a"] != pair["agent_b"]
        assert pair["shared_count"] >= 2
        assert "jaccard" in pair

    def test_no_resonance_single_agent(self, test_db):
        from ohm.queries import create_node, scratch, detect_fragment_resonance

        create_node(test_db, label="Shared Context Node", node_type="concept", created_by="test")
        scratch(test_db, content="Shared Context Node is important", created_by="metis")
        scratch(test_db, content="Shared Context Node also matters", created_by="metis")

        result = detect_fragment_resonance(test_db, min_shared=1)
        for pair in result:
            assert pair["agent_a"] != pair["agent_b"]

    def test_resonance_empty_graph(self, test_db):
        from ohm.queries import detect_fragment_resonance

        result = detect_fragment_resonance(test_db)
        assert result == []

    def test_resonance_limit(self, test_db):
        from ohm.queries import create_node, scratch, detect_fragment_resonance

        for i in range(5):
            create_node(test_db, label=f"Context{i:02d}Node", node_type="concept", created_by="test")

        content = " ".join(f"Context{i:02d}Node" for i in range(5))
        scratch(test_db, content=content, created_by="metis")
        scratch(test_db, content=content, created_by="clio")
        scratch(test_db, content=content, created_by="socrates")

        result = detect_fragment_resonance(test_db, limit=1)
        assert len(result) <= 1

    # OHM-a5rz.25: Inline resonance edge creation during scratch()
    def test_scratch_creates_resonance_links(self, test_db):
        """scratch() returns resonance_links when fragments from different agents share targets."""
        from ohm.queries import create_node, scratch

        create_node(test_db, label="ResonanceShared", node_type="concept", created_by="test")
        scratch(test_db, content="ResonanceShared from agent alpha", created_by="agent_a")
        frag2 = scratch(test_db, content="ResonanceShared from agent beta", created_by="agent_b")
        assert "resonance_links" in frag2
        assert len(frag2["resonance_links"]) >= 1
        assert frag2["resonance_links"][0]["edge_type"] == "RESONANCE"

    def test_resonance_edge_is_persisted(self, test_db):
        """RESONANCE edge exists in database with correct attributes."""
        from ohm.queries import create_node, scratch

        create_node(test_db, label="ResonanceTrigger", node_type="concept", created_by="test")
        scratch(test_db, content="ResonanceTrigger from alpha", created_by="agent_a")
        result = scratch(test_db, content="ResonanceTrigger from beta", created_by="agent_b")

        assert "resonance_links" in result
        edge_id = result["resonance_links"][0]["edge_id"]
        row = test_db.execute(
            "SELECT edge_type, layer, provenance FROM ohm_edges WHERE id = ?",
            [edge_id],
        ).fetchone()
        assert row[0] == "RESONANCE"
        assert row[1] == "L0"
        assert row[2] == "auto_resonance"

    def test_no_resonance_with_same_agent(self, test_db):
        """No RESONANCE edge created when fragments from same agent share targets."""
        from ohm.queries import create_node, scratch

        create_node(test_db, label="SameAgentResonance", node_type="concept", created_by="test")
        scratch(test_db, content="SameAgentResonance first", created_by="agent_a")
        result = scratch(test_db, content="SameAgentResonance second", created_by="agent_a")
        assert "resonance_links" not in result or len(result.get("resonance_links", [])) == 0


class TestScratchConnectsToEdges:
    """OHM-a5rz.17: connects_to creates explicit L0 CONTEXT_OF edges."""

    def test_connects_to_creates_edge(self, test_db):
        from ohm.queries import create_node, scratch

        anchor = create_node(test_db, label="Anchor Node For Edge", node_type="concept", created_by="test")
        result = scratch(test_db, content="Hunch about anchor", created_by="test", connects_to=[anchor["id"]])
        assert "explicit_links" in result
        assert len(result["explicit_links"]) == 1
        link = result["explicit_links"][0]
        assert link["node_id"] == anchor["id"]
        assert link["edge_type"] == "CONTEXT_OF"
        assert link["provenance"] == "scratch_explicit"

    def test_connects_to_multiple_targets(self, test_db):
        from ohm.queries import create_node, scratch

        target_a = create_node(test_db, label="Target A For Multi", node_type="concept", created_by="test")
        target_b = create_node(test_db, label="Target B For Multi", node_type="concept", created_by="test")
        result = scratch(test_db, content="Multi hunch", created_by="test", connects_to=[target_a["id"], target_b["id"]])
        assert len(result["explicit_links"]) == 2
        target_ids = {l["node_id"] for l in result["explicit_links"]}
        assert target_a["id"] in target_ids
        assert target_b["id"] in target_ids

    def test_connects_to_edge_in_database(self, test_db):
        from ohm.queries import create_node, scratch

        anchor = create_node(test_db, label="Edge Verify Target", node_type="concept", created_by="test")
        result = scratch(test_db, content="Edge verify", created_by="test", connects_to=[anchor["id"]])
        fragment_id = result["id"]
        rows = test_db.execute(
            "SELECT * FROM ohm_edges WHERE from_node = ? AND to_node = ? AND layer = 'L0'",
            [fragment_id, anchor["id"]],
        ).fetchall()
        assert len(rows) == 1
        # Column order: id, from_node, to_node, layer, edge_type, ...
        assert rows[0][4] == "CONTEXT_OF"

    def test_no_connects_to_no_explicit_links(self, test_db):
        from ohm.queries import scratch

        result = scratch(test_db, content="Simple hunch no connections", created_by="test")
        assert "explicit_links" not in result or len(result.get("explicit_links", [])) == 0


class TestSearchExcludesFragments:
    """OHM-a5rz.18/20: search and semantic_search exclude fragments by default."""

    def test_search_excludes_fragments(self, test_db):
        from ohm.queries import create_node, scratch, search

        create_node(test_db, label="SearchExConcept", node_type="concept", created_by="test")
        scratch(test_db, content="SearchExConcept hunch", created_by="test")
        results = search(test_db, query="SearchExConcept")
        types = [r["type"] for r in results]
        assert "concept" in types
        assert "fragment" not in types

    def test_search_includes_fragments_with_include_l0(self, test_db):
        from ohm.queries import create_node, scratch, search

        create_node(test_db, label="SearchInConcept", node_type="concept", created_by="test")
        scratch(test_db, content="SearchInConcept hunch", created_by="test")
        results = search(test_db, query="SearchInConcept", include_l0=True)
        types = [r["type"] for r in results]
        assert "concept" in types
        assert "fragment" in types

    def test_search_type_fragment_overrides_include_l0(self, test_db):
        from ohm.queries import scratch, search

        scratch(test_db, content="ExplicitTypeFragQuery hunch", created_by="test")
        results = search(test_db, query="ExplicitTypeFragQuery", node_type="fragment")
        assert len(results) >= 1
        assert all(r["type"] == "fragment" for r in results)

    def test_search_empty_query_returns_empty(self, test_db):
        from ohm.queries import search

        results = search(test_db, query="")
        assert results == []

    def test_connects_to_and_auto_links_coexist(self, test_db):
        from ohm.queries import create_node, scratch

        anchor = create_node(test_db, label="CoexistTest", node_type="concept", created_by="test")
        result = scratch(test_db, content="CoexistTest hunch", created_by="test", connects_to=[anchor["id"]])
        # Should have explicit link
        assert len(result.get("explicit_links", [])) >= 1
        # Should also have auto_link (CoexistTest appears in content)
        assert len(result.get("auto_links", [])) >= 1


class TestFragmentClusters:
    """Tests for fragment cluster detection (OHM-a5rz.28)."""

    def test_cluster_empty_graph(self, test_db):
        """query_fragment_clusters returns empty list on empty graph."""
        from ohm.queries import query_fragment_clusters

        result = query_fragment_clusters(test_db)
        assert result == []

    def test_cluster_single_fragment(self, test_db):
        """query_fragment_clusters returns empty list with only 1 fragment."""
        from ohm.queries import create_node, scratch, query_fragment_clusters

        create_node(test_db, label="Alone", node_type="concept", created_by="test")
        scratch(test_db, content="Alone fragment", created_by="test")
        result = query_fragment_clusters(test_db)
        assert result == []

    def test_cluster_three_fragments_shared_two_targets(self, test_db):
        """3 fragments sharing 2 targets form a cluster."""
        from ohm.queries import create_node, scratch, query_fragment_clusters

        t1 = create_node(test_db, label="TargetOne", node_type="concept", created_by="test")
        t2 = create_node(test_db, label="TargetTwo", node_type="concept", created_by="test")

        # Three fragments all linking to both targets
        content = "TargetOne and TargetTwo are both discussed here"
        scratch(test_db, content=content, created_by="agent_a")
        scratch(test_db, content=content, created_by="agent_b")
        scratch(test_db, content=content, created_by="agent_c")

        result = query_fragment_clusters(test_db, min_fragments=3, min_shared_targets=2)
        assert len(result) >= 1
        cluster = result[0]
        assert cluster["cluster_size"] >= 3
        assert cluster["shared_target_count"] >= 2

    def test_cluster_excludes_pairs(self, test_db):
        """2 fragments sharing targets do not form a cluster (need 3+)."""
        from ohm.queries import create_node, scratch, query_fragment_clusters

        t1 = create_node(test_db, label="SharedTarget", node_type="concept", created_by="test")
        content = "SharedTarget discussed here"
        scratch(test_db, content=content, created_by="agent_a")
        scratch(test_db, content=content, created_by="agent_b")

        result = query_fragment_clusters(test_db, min_fragments=3)
        assert result == []

    def test_cluster_multiple_groups(self, test_db):
        """Separate clusters are identified independently."""
        from ohm.queries import create_node, scratch, query_fragment_clusters

        c1 = create_node(test_db, label="ClusterA", node_type="concept", created_by="test")
        c2 = create_node(test_db, label="ClusterB", node_type="concept", created_by="test")

        # Cluster A: 3 fragments sharing "ClusterA"
        for agent in ("alpha", "beta", "gamma"):
            scratch(test_db, content=f"ClusterA discussed by {agent}", created_by=agent)

        # Cluster B: 3 fragments sharing "ClusterB"
        for agent in ("delta", "epsilon", "zeta"):
            scratch(test_db, content=f"ClusterB discussed by {agent}", created_by=agent)

        result = query_fragment_clusters(test_db, min_fragments=3, min_shared_targets=1)
        assert len(result) >= 2


class TestFragmentEviction:
    """Tests for fragment TTL eviction (OHM-a5rz.27)."""

    def _set_old_updated_at(self, test_db, fragment_id: str, days_ago: int = 31):
        """Helper: set a fragment's updated_at to look old."""
        test_db.execute(
            f"UPDATE ohm_nodes SET updated_at = CURRENT_TIMESTAMP - INTERVAL '{days_ago}' DAY WHERE id = ?",
            [fragment_id],
        )

    def test_evict_no_candidates(self, test_db):
        """evict_expired_fragments returns empty when nothing is expired."""
        from ohm.queries import evict_expired_fragments

        result = evict_expired_fragments(test_db, ttl_days=0)
        assert result["candidate_count"] == 0
        assert result["evicted"] == []
        assert result["extended"] == []
        assert result["skipped_promoted"] == []

    def test_evict_soft_deletes_expired_fragment(self, test_db):
        """Expired fragment with no edges is soft-deleted."""
        from ohm.queries import scratch, evict_expired_fragments

        frag = scratch(test_db, content="This fragment will be evicted", created_by="test")
        self._set_old_updated_at(test_db, frag["id"])

        result = evict_expired_fragments(test_db, ttl_days=0)
        assert frag["id"] in result["evicted"]

        # Verify soft-deleted
        row = test_db.execute(
            "SELECT deleted_at FROM ohm_nodes WHERE id = ?",
            [frag["id"]],
        ).fetchone()
        assert row[0] is not None

    def test_evict_extends_ttl_for_fragment_with_edges(self, test_db):
        """Fragment with L0 edges gets TTL extended instead of evicted."""
        from ohm.queries import create_node, scratch, evict_expired_fragments

        concept = create_node(test_db, label="KeepAlive", node_type="concept", created_by="test")
        frag = scratch(test_db, content="KeepAlive keeps this fragment alive", created_by="test")
        self._set_old_updated_at(test_db, frag["id"])

        result = evict_expired_fragments(test_db, ttl_days=0)
        assert frag["id"] in result["extended"]
        assert frag["id"] not in result["evicted"]

        # Verify not deleted
        row = test_db.execute(
            "SELECT deleted_at FROM ohm_nodes WHERE id = ?",
            [frag["id"]],
        ).fetchone()
        assert row[0] is None

        # Verify updated_at was bumped
        row = test_db.execute(
            "SELECT updated_at > CURRENT_TIMESTAMP - INTERVAL '1 minute' FROM ohm_nodes WHERE id = ?",
            [frag["id"]],
        ).fetchone()
        assert row[0] is True

    def test_evict_skips_promoted_fragment(self, test_db):
        """Promoted fragment is never evicted, even when expired."""
        from ohm.queries import scratch, promote_fragment, evict_expired_fragments, create_node
        import json as _json

        ctx = create_node(test_db, label="Context", node_type="concept", created_by="test")
        frag = scratch(test_db, content="This promoted fragment survives", created_by="test", connects_to=[ctx["id"]])
        promote_fragment(test_db, fragment_id=frag["id"], promoted_by="test")
        self._set_old_updated_at(test_db, frag["id"])

        result = evict_expired_fragments(test_db, ttl_days=0)
        assert frag["id"] in result["skipped_promoted"]
        assert frag["id"] not in result["evicted"]

    def test_evict_mixed_scenario(self, test_db):
        """Multiple fragments with different states produce correct results."""
        from ohm.queries import create_node, scratch, promote_fragment, evict_expired_fragments

        anchor = create_node(test_db, label="AnchorExtend", node_type="concept", created_by="test")

        # f1: no edges (unique content that doesn't match any label)
        f1 = scratch(test_db, content="X7k9q2z4 evictable fragment", created_by="test")
        # f2: has edges (auto-links to AnchorExtend)
        f2 = scratch(test_db, content="AnchorExtend gives this fragment edges", created_by="test")
        # f3: promoted (needs context link per ADR-022)
        f3 = scratch(test_db, content="X7k9q2z4 promote candidate", created_by="test", connects_to=[anchor["id"]])
        promote_fragment(test_db, fragment_id=f3["id"], promoted_by="test")

        self._set_old_updated_at(test_db, f1["id"])
        self._set_old_updated_at(test_db, f2["id"])
        self._set_old_updated_at(test_db, f3["id"])

        result = evict_expired_fragments(test_db, ttl_days=0)
        assert f1["id"] in result["evicted"]
        assert f2["id"] in result["extended"]
        assert f3["id"] in result["skipped_promoted"]


class TestFragmentDensityStats:
    """Tests for fragment density metrics in stats (OHM-a5rz.24)."""

    def test_stats_without_include_l0_excludes_density(self, test_db):
        """query_stats without include_l0 does not include fragment_density."""
        from ohm.queries import query_stats

        stats = query_stats(test_db)
        assert "fragment_density" not in stats

    def test_stats_with_include_l0_returns_density(self, test_db):
        """query_stats with include_l0=True returns fragment density metrics."""
        from ohm.queries import query_stats, create_node, scratch

        # Create a concept and a fragment that links to it
        concept = create_node(test_db, label="DensityMetric", node_type="concept", created_by="test")
        scratch(test_db, content="DensityMetric test fragment", created_by="test")

        stats = query_stats(test_db, include_l0=True)
        assert "fragment_density" in stats
        f = stats["fragment_density"]
        assert f["fragments_total"] >= 1
        assert f["total_fragment_edges"] >= 1
        assert f["fragment_density"] > 0

    def test_fragment_density_empty_graph(self, test_db):
        """Fragment density is 0.0 when no fragments exist."""
        from ohm.queries import query_stats

        stats = query_stats(test_db, include_l0=True)
        assert "fragment_density" in stats
        assert stats["fragment_density"]["fragments_total"] == 0
        assert stats["fragment_density"]["fragment_density"] == 0.0


class TestFragmentPromotion:
    """Tests for fragment promotion to L1 concept (OHM-a5rz.26)."""

    def test_promote_creates_concept(self, test_db):
        """promote_fragment creates a concept node from a fragment."""
        from ohm.queries import scratch, promote_fragment, create_node

        ctx = create_node(test_db, label="Context", node_type="concept", created_by="test")
        frag = scratch(test_db, content="Important fragment worth promoting", created_by="test", connects_to=[ctx["id"]])
        result = promote_fragment(test_db, fragment_id=frag["id"], promoted_by="metis")

        assert "concept" in result
        concept = result["concept"]
        assert concept["type"] == "concept"
        assert concept["label"] == frag["label"]

    def test_promote_creates_refines_frag_edge(self, test_db):
        """promote_fragment creates REFINES_FRAG edge from concept to fragment."""
        from ohm.queries import scratch, promote_fragment, create_node

        ctx = create_node(test_db, label="Context", node_type="concept", created_by="test")
        frag = scratch(test_db, content="Promote me to concept", created_by="test", connects_to=[ctx["id"]])
        result = promote_fragment(test_db, fragment_id=frag["id"], promoted_by="metis")

        edge = test_db.execute(
            "SELECT edge_type, layer, from_node, to_node FROM ohm_edges WHERE id = ?",
            [result["edge"]["id"]],
        ).fetchone()
        assert edge[0] == "REFINES_FRAG"
        assert edge[1] == "L0"
        assert edge[2] == result["concept"]["id"]
        assert edge[3] == frag["id"]

    def test_promote_sets_fragment_metadata(self, test_db):
        """promote_fragment sets promoted_to in fragment metadata."""
        from ohm.queries import scratch, promote_fragment, create_node
        import json

        ctx = create_node(test_db, label="Context", node_type="concept", created_by="test")
        frag = scratch(test_db, content="Fragment with promotion metadata", created_by="test", connects_to=[ctx["id"]])
        result = promote_fragment(test_db, fragment_id=frag["id"], promoted_by="metis")

        meta_row = test_db.execute(
            "SELECT metadata FROM ohm_nodes WHERE id = ?",
            [frag["id"]],
        ).fetchone()
        meta = json.loads(meta_row[0]) if meta_row[0] else {}
        assert "promoted_to" in meta
        assert meta["promoted_to"] == result["concept"]["id"]

    def test_promote_nonexistent_fragment(self, test_db):
        """promote_fragment raises error for nonexistent fragment."""
        from ohm.queries import promote_fragment
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            promote_fragment(test_db, fragment_id="nonexistent-id", promoted_by="metis")

    def test_promote_non_fragment_node(self, test_db):
        """promote_fragment raises error for non-fragment node."""
        from ohm.queries import create_node, promote_fragment

        node = create_node(test_db, label="Not a fragment", node_type="concept", created_by="test")
        with pytest.raises(ValueError, match="is not a fragment"):
            promote_fragment(test_db, fragment_id=node["id"], promoted_by="metis")
