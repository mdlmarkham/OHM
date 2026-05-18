"""Tests for the OHM Python SDK (Graph class)."""

import pytest

from ohm.sdk import connect


@pytest.fixture
def graph():
    """Create an in-memory Graph for testing."""
    g = connect(":memory:", actor="test_agent")
    yield g
    g.close()


class TestGraphWrite:
    """Tests for SDK write operations."""

    def test_create_node(self, graph):
        node = graph.create_node(label="Test Node")
        assert node["id"].startswith("test_node_")
        assert node["label"] == "Test Node"

    def test_create_node_with_type(self, graph):
        node = graph.create_node(label="Source A", node_type="source")
        assert node["id"]
        assert node["type"] == "source"

    def test_create_edge(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        assert edge["id"]
        assert edge["edge_type"] == "CAUSES"

    def test_challenge(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        challenge = graph.challenge(edge["id"], reason="weak evidence", confidence=0.3)
        assert challenge["id"]
        assert challenge["edge_type"] == "CHALLENGED_BY"

    def test_support(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        support = graph.support(edge["id"], reason="additional evidence", confidence=0.8)
        assert support["id"]
        assert support["edge_type"] == "SUPPORTS"

    def test_update_edge(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.5)
        graph.update_edge(edge["id"], confidence=0.95)
        result = graph.confidence(edge["id"])
        assert result["original"]["confidence"] == pytest.approx(0.95)

    def test_update_edge_permission_denied(self, graph, tmp_path):
        """Non-owner cannot update another agent's edge."""
        # Use a shared file DB so both connections see the same data
        db_path = str(tmp_path / "shared.duckdb")
        g1 = connect(db_path, actor="owner")
        a2 = g1.create_node(label="A2")["id"]
        b2 = g1.create_node(label="B2")["id"]
        e2 = g1.create_edge(from_node=a2, to_node=b2, edge_type="CAUSES", layer="L3")["id"]
        g1.close()

        g2 = connect(db_path, actor="other_agent")
        from ohm.exceptions import PermissionDeniedError
        with pytest.raises(PermissionDeniedError):
            g2.update_edge(e2, confidence=0.5)
        g2.close()

    def test_observe(self, graph):
        a = graph.create_node(label="A")["id"]
        obs = graph.observe(a, obs_type="measurement", value=1.5, sigma=0.3)
        assert obs["id"]
        assert obs["type"] == "measurement"

    def test_set_focus(self, graph):
        graph.set_focus("researching patterns")
        state = graph.agent_state("test_agent")
        assert len(state) >= 1


class TestGraphRead:
    """Tests for SDK read operations."""

    def test_neighborhood(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.neighborhood(a, depth=2)
        assert len(results) >= 1

    def test_path(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.path(a, b)
        assert len(results) >= 1

    def test_impact(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.impact(a, depth=5)
        assert len(results) >= 1

    def test_confidence(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        result = graph.confidence(edge["id"])
        assert result["original"] is not None

    def test_listen(self, graph):
        results = graph.listen()
        assert isinstance(results, list)

    def test_listen_with_node_type(self, graph):
        """listen() accepts node_type filter."""
        graph.create_node(label="Concept Node", node_type="concept")
        graph.create_node(label="Pattern Node", node_type="pattern")
        results = graph.listen(node_type="concept")
        assert isinstance(results, list)

    def test_agent_state(self, graph):
        graph.set_focus("testing")
        results = graph.agent_state()
        assert isinstance(results, list)

    def test_stats(self, graph):
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        stats = graph.stats()
        assert stats["total_nodes"] >= 2
        assert stats["total_edges"] >= 1


class TestGraphContextManager:
    """Tests for context manager protocol."""

    def test_context_manager(self):
        with connect(":memory:", actor="ctx_test") as g:
            node = g.create_node(label="Context Test")
            assert node["id"]
        # Connection should be closed after exiting context


class TestConnect:
    """Tests for the connect() factory function."""

    def test_connect_defaults(self):
        g = connect()
        assert g.actor == "unknown"
        g.close()

    def test_connect_with_actor(self):
        g = connect(actor="metis")
        assert g.actor == "metis"
        g.close()


class TestSDKParity:
    """Tests for OHM-azn.4: CLI↔SDK parity gap methods."""

    def test_apply_decay_dry_run(self, graph):
        """apply_decay with dry_run=True reports but doesn't modify."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        result = graph.apply_decay(dry_run=True)
        assert "decayed_count" in result
        assert "affected_edges" in result
        assert "summary" in result

    def test_apply_decay_with_half_life(self, graph):
        """apply_decay respects half_life_days parameter."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        result = graph.apply_decay(half_life_days=90.0, dry_run=True)
        assert isinstance(result["decayed_count"], int)

    def test_query_text_search(self, graph):
        """query() with text searches nodes by label."""
        graph.create_node(label="climate change")
        graph.create_node(label="unrelated topic")
        results = graph.query(text="climate")
        assert len(results) >= 1
        assert any("climate" in r.get("label", "") for r in results)

    def test_query_edge_filter(self, graph):
        """query() with filter_type filters edges."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.query(filter_type="CAUSES")
        assert len(results) >= 1
        assert all(r["edge_type"] == "CAUSES" for r in results)

    def test_query_layer_filter(self, graph):
        """query() with layer filters edges."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.query(layer="L3")
        assert len(results) >= 1

    def test_query_confidence_min(self, graph):
        """query() with confidence_min filters edges."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.9)
        results = graph.query(confidence_min=0.8)
        assert len(results) >= 1

    def test_query_no_filters_returns_nodes(self, graph):
        """query() with no filters returns recent nodes."""
        graph.create_node(label="Test Node")
        results = graph.query()
        assert len(results) >= 1

    def test_status_includes_schema_version(self, graph):
        """status() returns stats plus schema_version."""
        result = graph.status()
        assert "schema_version" in result
        assert "total_nodes" in result
        assert "total_edges" in result

    def test_status_schema_version_is_string(self, graph):
        """status() schema_version is a version string."""
        result = graph.status()
        version = result["schema_version"]
        assert isinstance(version, str)
        # Should look like a version (e.g., "0.4.0" or similar)
        assert len(version) >= 1

    def test_upgrade_dry_run(self, graph):
        """upgrade() with dry_run reports pending migrations without applying."""
        result = graph.upgrade(dry_run=True)
        assert "current_version" in result
        assert "target_version" in result
        assert "pending" in result
        assert "applied" in result
        assert result["applied"] is False  # dry run doesn't apply

    def test_upgrade_applies_pending(self, graph):
        """upgrade() without dry_run applies pending migrations."""
        result = graph.upgrade()
        assert "current_version" in result
        assert "applied" in result
        # Fresh DB should have had migrations applied during init
        # so no additional pending ones
        assert result["applied"] is False


class TestDiscovery:
    """Tests for ADR-005 self-documenting discovery methods."""

    def test_schema(self, graph):
        """schema() returns node types, edge types by layer, and version."""
        result = graph.schema()
        assert "node_types" in result
        assert "edge_types_by_layer" in result
        assert "schema_version" in result
        assert isinstance(result["node_types"], list)
        assert "L1" in result["edge_types_by_layer"]
        assert "L4" in result["edge_types_by_layer"]

    def test_layers(self, graph):
        """layers() returns L1-L4 layer descriptors."""
        result = graph.layers()
        assert isinstance(result, list)
        assert len(result) == 4
        # Should have L1, L2, L3, L4
        names = {r["name"] for r in result}
        assert names == {"L1", "L2", "L3", "L4"}
        # Each layer should have sharing, ownership, edge_types, example
        for r in result:
            assert "sharing" in r
            assert "ownership" in r
            assert "edge_types" in r
            assert "example" in r


class TestExpiringSoon:
    """Tests for OHM-0e0.6: batch expiry detection."""

    def test_expiring_soon_empty(self, graph):
        """expiring_soon() returns empty list when no expiry edges exist."""
        results = graph.expiring_soon()
        assert isinstance(results, list)
        assert len(results) == 0

    def test_expiring_soon_with_expiry_edge(self, graph):
        """expiring_soon() finds batches with BATCH_EXPIRES_BEFORE edges."""
        from datetime import datetime, timezone, timedelta

        # Create product batch node and location node
        batch = graph.create_node(label="Milk Batch #42", node_type="equipment")
        location = graph.create_node(label="Store #7 Cooler", node_type="site")

        # Create expiry edge with metadata
        expires = datetime.now(timezone.utc) + timedelta(days=3)
        graph.create_edge(
            from_node=batch["id"], to_node=location["id"],
            edge_type="BATCH_EXPIRES_BEFORE", layer="L2",
            metadata={"expires_at": expires.isoformat()},
        )

        results = graph.expiring_soon(days=5)
        assert len(results) >= 1
        assert results[0]["from_node"] == batch["id"]
        assert results[0]["product_type"] == "equipment"
        assert results[0]["days_until_expiry"] is not None
        assert results[0]["days_until_expiry"] <= 5

    def test_expiring_soon_product_type_filter(self, graph):
        """expiring_soon() filters by product_type."""
        from datetime import datetime, timezone, timedelta

        dairy = graph.create_node(label="Yogurt Batch", node_type="equipment")
        produce = graph.create_node(label="Lettuce Batch", node_type="system")
        loc = graph.create_node(label="Store", node_type="site")

        expires = datetime.now(timezone.utc) + timedelta(days=2)
        graph.create_edge(
            from_node=dairy["id"], to_node=loc["id"],
            edge_type="BATCH_EXPIRES_BEFORE", layer="L2",
            metadata={"expires_at": expires.isoformat()},
        )
        graph.create_edge(
            from_node=produce["id"], to_node=loc["id"],
            edge_type="BATCH_EXPIRES_BEFORE", layer="L2",
            metadata={"expires_at": expires.isoformat()},
        )

        dairy_results = graph.expiring_soon(product_type="equipment", days=5)
        assert len(dairy_results) >= 1
        assert all(r["product_type"] == "equipment" for r in dairy_results)

    def test_expiring_soon_days_filter(self, graph):
        """expiring_soon() only returns batches within the days window."""
        from datetime import datetime, timezone, timedelta

        batch = graph.create_node(label="Cheese Batch", node_type="equipment")
        loc = graph.create_node(label="Store", node_type="site")

        # Expires in 10 days — should NOT appear with days=5
        expires = datetime.now(timezone.utc) + timedelta(days=10)
        graph.create_edge(
            from_node=batch["id"], to_node=loc["id"],
            edge_type="BATCH_EXPIRES_BEFORE", layer="L2",
            metadata={"expires_at": expires.isoformat()},
        )

        results = graph.expiring_soon(days=5)
        assert len(results) == 0

        # But should appear with days=15
        results_wide = graph.expiring_soon(days=15)
        assert len(results_wide) >= 1

    def test_expiring_soon_sorted_by_expiry(self, graph):
        """expiring_soon() returns soonest-expiring batches first."""
        from datetime import datetime, timezone, timedelta

        loc = graph.create_node(label="Store", node_type="site")

        # Batch expiring in 1 day
        urgent = graph.create_node(label="Urgent Batch", node_type="equipment")
        graph.create_edge(
            from_node=urgent["id"], to_node=loc["id"],
            edge_type="BATCH_EXPIRES_BEFORE", layer="L2",
            metadata={"expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
        )

        # Batch expiring in 4 days
        later = graph.create_node(label="Later Batch", node_type="equipment")
        graph.create_edge(
            from_node=later["id"], to_node=loc["id"],
            edge_type="BATCH_EXPIRES_BEFORE", layer="L2",
            metadata={"expires_at": (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()},
        )

        results = graph.expiring_soon(days=5)
        assert len(results) >= 2
        # First result should be the one expiring sooner
        assert results[0]["days_until_expiry"] <= results[1]["days_until_expiry"]
