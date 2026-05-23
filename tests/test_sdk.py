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

    def test_observe_with_notes(self, graph):
        """OHM-of8: observe() should persist and return notes."""
        a = graph.create_node(label="NotesTest")["id"]
        obs = graph.observe(a, obs_type="measurement", value=2.0, notes="Anomalous spike")
        assert obs["id"]
        assert obs["notes"] == "Anomalous spike"

    def test_observe_with_source_attribution(self, graph):
        """OHM-lmr: observe() should persist and return source_name and source_url."""
        a = graph.create_node(label="SourceTest")["id"]
        obs = graph.observe(
            a,
            obs_type="measurement",
            value=3.0,
            source_name="Reuters",
            source_url="https://reuters.com/article/456",
        )
        assert obs["id"]
        assert obs["source_name"] == "Reuters"
        assert obs["source_url"] == "https://reuters.com/article/456"

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

    def test_connect_remote_strict_raises(self):
        """connect_remote with strict=True raises when Quack unavailable."""
        from pytest import raises
        from ohm.sdk import connect_remote

        # Quack not available in test env — should raise (message varies by failure mode)
        with raises(ConnectionError, match="Quack is not available|Failed to attach"):
            connect_remote(uri="quack:localhost", actor="test", strict=True)

    def test_connect_remote_non_strict_succeeds(self, monkeypatch):
        """connect_remote with strict=False falls back to in-memory DB."""
        from ohm.sdk import connect_remote

        # Force in-memory fallback to avoid file path issues
        monkeypatch.setenv("OHM_DB", ":memory:")
        g = connect_remote(uri="quack:localhost", actor="test", strict=False)
        assert g.actor == "test"
        g.close()


class TestSDKParity:
    """Tests for OHM-azn.4: CLI↔SDK parity gap methods."""

    def test_unicode_roundtrip_café(self, graph):
        """Node with Unicode label 'café' round-trips correctly."""
        node = graph.create_node(label="café", node_type="concept")
        assert node["label"] == "café"
        # Read back
        found = graph.get_node(node["id"])
        assert found["label"] == "café"

    def test_unicode_roundtrip_emoji(self, graph):
        """Node with emoji label round-trips correctly."""
        node = graph.create_node(label="⚠️ High Risk", node_type="concept")
        assert node["label"] == "⚠️ High Risk"
        found = graph.get_node(node["id"])
        assert found["label"] == "⚠️ High Risk"

    def test_unicode_roundtrip_mixed(self, graph):
        """Node with mixed Unicode content works."""
        node = graph.create_node(label="naïve approach: ½ + 日本語", node_type="concept")
        assert node["label"] == "naïve approach: ½ + 日本語"
        found = graph.get_node(node["id"])
        assert found["label"] == "naïve approach: ½ + 日本語"

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


# ===== Customer Support SDK Tests (OHM-af8.5) =====


class TestHandoff:
    """Tests for SDK handoff() method."""

    def test_handoff_creates_transferred_to_edge(self, graph):
        """handoff() creates TRANSFERRED_TO edge between agents."""
        agent_a = graph.create_node(label="Agent A", node_type="agent")
        agent_b = graph.create_node(label="Agent B", node_type="agent")
        ticket = graph.create_node(label="Ticket #1", node_type="event")

        result = graph.handoff(
            from_agent=agent_a["id"],
            to_agent=agent_b["id"],
            ticket_node=ticket["id"],
            reason="Customer needs specialist",
        )

        assert result["edge"]["edge_type"] == "TRANSFERRED_TO"
        assert result["edge"]["from_node"] == agent_a["id"]
        assert result["edge"]["to_node"] == agent_b["id"]
        assert result["edge"]["condition"] == "Customer needs specialist"
        assert "handoff_chain" in result

    def test_handoff_with_delegation(self, graph):
        """handoff() with DELEGATED_TO creates L3 edge."""
        manager = graph.create_node(label="Manager", node_type="agent")
        specialist = graph.create_node(label="Specialist", node_type="agent")
        ticket = graph.create_node(label="Ticket #2", node_type="event")

        result = graph.handoff(
            from_agent=manager["id"],
            to_agent=specialist["id"],
            ticket_node=ticket["id"],
            reason="Delegating",
            edge_type="DELEGATED_TO",
        )

        assert result["edge"]["edge_type"] == "DELEGATED_TO"
        assert result["edge"]["layer"] == "L3"

    def test_handoff_invalid_edge_type_raises(self, graph):
        """handoff() with invalid edge_type raises ValueError."""
        agent_a = graph.create_node(label="Agent A", node_type="agent")
        agent_b = graph.create_node(label="Agent B", node_type="agent")
        ticket = graph.create_node(label="Ticket", node_type="event")

        with pytest.raises(ValueError, match="Invalid handoff edge_type"):
            graph.handoff(
                from_agent=agent_a["id"],
                to_agent=agent_b["id"],
                ticket_node=ticket["id"],
                reason="test",
                edge_type="CAUSES",
            )


class TestEscalate:
    """Tests for SDK escalate() method."""

    def test_escalate_creates_edge_and_sets_urgency(self, graph):
        """escalate() creates ESCALATED_TO edge and sets urgency='high'."""
        tier1 = graph.create_node(label="Tier 1", node_type="agent")
        tier2 = graph.create_node(label="Tier 2", node_type="agent")
        ticket = graph.create_node(label="Ticket #3", node_type="event")

        result = graph.escalate(
            ticket_node=ticket["id"],
            to_tier=tier2["id"],
            reason="SLA breach imminent",
            from_agent=tier1["id"],
        )

        assert result["edge"]["edge_type"] == "ESCALATED_TO"
        assert result["edge"]["from_node"] == tier1["id"]
        assert result["edge"]["to_node"] == tier2["id"]
        assert result["ticket"]["urgency"] == "high"

    def test_escalate_without_from_agent(self, graph):
        """escalate() without from_agent uses ticket_node as source."""
        tier2 = graph.create_node(label="Tier 2", node_type="agent")
        ticket = graph.create_node(label="Auto Ticket", node_type="event")

        result = graph.escalate(
            ticket_node=ticket["id"],
            to_tier=tier2["id"],
            reason="Auto-escalation",
        )

        assert result["edge"]["from_node"] == ticket["id"]


class TestTicketProvenance:
    """Tests for SDK ticket_provenance() method."""

    def test_ticket_provenance_shows_handoff_chain(self, graph):
        """ticket_provenance() returns handoff and state history."""
        agent_a = graph.create_node(label="Agent A", node_type="agent")
        agent_b = graph.create_node(label="Agent B", node_type="agent")
        ticket = graph.create_node(label="Ticket #4", node_type="event")

        graph.handoff(
            from_agent=agent_a["id"],
            to_agent=agent_b["id"],
            ticket_node=ticket["id"],
            reason="Needs specialist",
        )

        chain = graph.ticket_provenance(ticket["id"])
        assert len(chain) >= 1
        types = [step["edge_type"] for step in chain]
        assert "TRANSFERRED_TO" in types

    def test_ticket_provenance_with_state_machine(self, graph):
        """ticket_provenance() includes state machine edges."""
        agent = graph.create_node(label="Agent", node_type="agent")
        ticket = graph.create_node(label="Ticket #5", node_type="event")

        graph.create_edge(
            from_node=agent["id"],
            to_node=ticket["id"],
            edge_type="OPENED_BY",
            layer="L2",
            confidence=1.0,
        )

        chain = graph.ticket_provenance(ticket["id"])
        types = [step["edge_type"] for step in chain]
        assert "OPENED_BY" in types

    def test_full_customer_support_workflow(self, graph):
        """End-to-end customer support: open → handoff → escalate → resolve."""
        agent_a = graph.create_node(label="Agent A", node_type="agent")
        agent_b = graph.create_node(label="Agent B", node_type="agent")
        tier2 = graph.create_node(label="Tier 2", node_type="agent")
        ticket = graph.create_node(label="Support Ticket", node_type="event")

        graph.create_edge(
            from_node=agent_a["id"],
            to_node=ticket["id"],
            edge_type="OPENED_BY",
            layer="L2",
            confidence=1.0,
        )

        graph.handoff(
            ticket_node=ticket["id"],
            from_agent=agent_a["id"],
            to_agent=agent_b["id"],
            reason="Skill mismatch",
        )

        graph.escalate(
            ticket_node=ticket["id"],
            from_agent=agent_b["id"],
            to_tier=tier2["id"],
            reason="Exceeded authority",
        )

        graph.create_edge(
            from_node=tier2["id"],
            to_node=ticket["id"],
            edge_type="RESOLVED_BY",
            layer="L2",
            confidence=1.0,
        )

        chain = graph.ticket_provenance(ticket["id"])
        types = [step["edge_type"] for step in chain]
        assert "OPENED_BY" in types
        assert "TRANSFERRED_TO" in types
        assert "ESCALATED_TO" in types
        assert "RESOLVED_BY" in types


# ===== Cybersecurity SDK Tests (OHM-af8.4) =====


class TestRecordOutcome:
    """Tests for SDK record_outcome() method."""

    def test_record_outcome_false(self, graph):
        """record_outcome with outcome=False records incorrect claim."""
        edr = graph.create_node(label="EDR Sensor", node_type="system")
        alert = graph.create_node(label="Suspicious Login", node_type="event")

        result = graph.record_outcome(
            source_agent=edr["id"],
            claim_node=alert["id"],
            outcome=False,
        )

        assert result["source_agent"] == edr["id"]
        assert result["claim_node"] == alert["id"]
        assert result["outcome"] is False
        assert result["recorded_by"] == "test_agent"

    def test_record_outcome_true(self, graph):
        """record_outcome with outcome=True records correct claim."""
        siem = graph.create_node(label="SIEM", node_type="system")
        alert = graph.create_node(label="Brute Force", node_type="event")

        result = graph.record_outcome(
            source_agent=siem["id"],
            claim_node=alert["id"],
            outcome=True,
        )

        assert result["outcome"] is True


class TestSourceReliability:
    """Tests for SDK source_reliability() method."""

    def test_source_reliability_computes_metrics(self, graph):
        """source_reliability computes P(accurate) from outcomes."""
        edr = graph.create_node(label="EDR", node_type="system")
        alert1 = graph.create_node(label="Alert 1", node_type="event")
        alert2 = graph.create_node(label="Alert 2", node_type="event")
        alert3 = graph.create_node(label="Alert 3", node_type="event")

        # EDR: 2 correct, 1 incorrect → P(accurate) ≈ 0.67
        graph.record_outcome(source_agent=edr["id"], claim_node=alert1["id"], outcome=True)
        graph.record_outcome(source_agent=edr["id"], claim_node=alert2["id"], outcome=True)
        graph.record_outcome(source_agent=edr["id"], claim_node=alert3["id"], outcome=False)

        result = graph.source_reliability(edr["id"])
        assert result["total_outcomes"] == 3
        assert result["accurate_count"] == 2
        assert result["false_positive_count"] == 1
        assert result["p_accurate"] == pytest.approx(2 / 3, abs=0.01)
        assert result["false_positive_rate"] == pytest.approx(1 / 3, abs=0.01)

    def test_source_reliability_no_data(self, graph):
        """source_reliability with no outcomes returns None metrics."""
        edr = graph.create_node(label="New EDR", node_type="system")

        result = graph.source_reliability(edr["id"])
        assert result["total_outcomes"] == 0
        assert result["p_accurate"] is None


class TestThreatClusterSDK:
    """Tests for SDK threat_cluster() method."""

    def test_threat_cluster_finds_related_alerts(self, graph):
        """threat_cluster() finds alerts linked to an IOC."""
        ioc = graph.create_node(label="malicious-ip", node_type="concept")
        alert1 = graph.create_node(label="Port Scan", node_type="concept")
        alert2 = graph.create_node(label="Lateral Movement", node_type="concept")

        graph.create_edge(from_node=ioc["id"], to_node=alert1["id"], edge_type="THREAT_CLUSTER", layer="L3")
        graph.create_edge(from_node=ioc["id"], to_node=alert2["id"], edge_type="THREAT_CLUSTER", layer="L3")

        results = graph.threat_cluster(ioc["id"])
        assert len(results) == 2

    def test_threat_cluster_empty(self, graph):
        """threat_cluster() returns empty for unconnected IOC."""
        ioc = graph.create_node(label="unused-ioc", node_type="concept")
        results = graph.threat_cluster(ioc["id"])
        assert len(results) == 0


# ===== Node Priority/Urgency SDK Tests =====


class TestNodePriorityUrgency:
    """Tests for priority on nodes and urgency on edges."""

    def test_create_node_with_priority(self, graph):
        """create_node with priority sets the priority field."""
        node = graph.create_node(label="P1 Issue", node_type="event", priority="P1")
        assert node["priority"] == "P1"

    def test_create_node_default_priority(self, graph):
        """create_node without priority defaults to None."""
        node = graph.create_node(label="Normal Ticket", node_type="event")
        assert node["priority"] is None

    def test_create_node_invalid_priority_raises(self, graph):
        """create_node with invalid priority raises ValueError."""
        with pytest.raises(ValueError, match="Invalid priority"):
            graph.create_node(label="Bad Ticket", node_type="event", priority="P99")

    def test_edge_with_urgency(self, graph):
        """create_edge with urgency sets the urgency field on the edge."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3", urgency="high")
        assert edge["urgency"] == "high"

    def test_edge_invalid_urgency_raises(self, graph):
        """create_edge with invalid urgency raises ValueError."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        with pytest.raises(ValueError, match="Invalid urgency"):
            graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3", urgency="extreme")


# ===== Sentiment Observation SDK Tests =====


class TestSentimentObservation:
    """Tests for sentiment observation type."""

    def test_sentiment_observation(self, graph):
        """observe() with obs_type='sentiment' records sentiment."""
        ticket = graph.create_node(label="Ticket #100", node_type="event")
        obs = graph.observe(
            ticket["id"],
            obs_type="sentiment",
            value=-0.7,
            sigma=0.5,
            source="nlp_analysis",
        )
        assert obs["type"] == "sentiment"


# ===== Read Operations SDK Tests =====


class TestGetNode:
    """Tests for SDK get_node() method."""

    def test_get_node_returns_node(self, graph):
        """get_node() returns the full node record."""
        created = graph.create_node(label="Find Me", node_type="concept")
        found = graph.get_node(created["id"])
        assert found is not None
        assert found["label"] == "Find Me"
        assert found["type"] == "concept"

    def test_get_node_not_found(self, graph):
        """get_node() returns None for nonexistent node."""
        result = graph.get_node("nonexistent_id")
        assert result is None


class TestGetEdge:
    """Tests for SDK get_edge() method."""

    def test_get_edge_returns_edge(self, graph):
        """get_edge() returns the full edge record."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        created = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        found = graph.get_edge(created["id"])
        assert found is not None
        assert found["edge_type"] == "CAUSES"

    def test_get_edge_not_found(self, graph):
        """get_edge() returns None for nonexistent edge."""
        result = graph.get_edge("nonexistent_id")
        assert result is None


class TestFindOrCreateNode:
    """Tests for SDK find_or_create_node() method."""

    def test_find_or_create_node_creates_new(self, graph):
        """find_or_create_node() creates node when not found."""
        node = graph.find_or_create_node("New Concept")
        assert node["label"] == "New Concept"
        assert node["type"] == "concept"

    def test_find_or_create_node_finds_existing(self, graph):
        """find_or_create_node() returns existing node by label."""
        created = graph.create_node(label="Existing", node_type="source")
        found = graph.find_or_create_node("Existing")
        assert found["id"] == created["id"]

    def test_find_or_create_node_case_insensitive(self, graph):
        """find_or_create_node() is case-insensitive."""
        created = graph.create_node(label="CaseTest", node_type="concept")
        found = graph.find_or_create_node("casetest")
        assert found["id"] == created["id"]


class TestSearchNodes:
    """Tests for SDK search_nodes() method."""

    def test_search_nodes_by_label(self, graph):
        """search_nodes() finds nodes by label text."""
        graph.create_node(label="climate change impact")
        graph.create_node(label="unrelated topic")
        results = graph.search_nodes("climate")
        assert len(results) >= 1
        assert any("climate" in r["label"] for r in results)

    def test_search_nodes_by_type(self, graph):
        """search_nodes() filters by node_type."""
        graph.create_node(label="Source A", node_type="source")
        graph.create_node(label="Concept A", node_type="concept")
        results = graph.search_nodes("A", node_type="source")
        assert all(r["type"] == "source" for r in results)

    def test_search_nodes_limit(self, graph):
        """search_nodes() respects limit parameter."""
        for i in range(10):
            graph.create_node(label=f"SearchTest {i}")
        results = graph.search_nodes("SearchTest", limit=3)
        assert len(results) <= 3


class TestSearchEdges:
    """Tests for SDK search_edges() method."""

    def test_search_edges_by_layer(self, graph):
        """search_edges() filters by layer."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.search_edges(layer="L3")
        assert len(results) >= 1
        assert all(r["layer"] == "L3" for r in results)

    def test_search_edges_by_type(self, graph):
        """search_edges() filters by edge_type."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.search_edges(edge_type="CAUSES")
        assert len(results) >= 1
        assert all(r["edge_type"] == "CAUSES" for r in results)

    def test_search_edges_by_confidence(self, graph):
        """search_edges() filters by confidence range."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.9)
        results = graph.search_edges(confidence_min=0.8)
        assert len(results) >= 1


# ===== Agent Registration SDK Tests =====


class TestRegisterAgent:
    """Tests for SDK register_agent() method."""

    def test_register_agent_basic(self, graph):
        """register_agent() creates an agent node with identity."""
        agent = graph.register_agent(
            description="Test agent for SDK",
            values=["accuracy", "transparency"],
            goals=["help users"],
            capabilities=["search", "analyze"],
        )
        assert agent["type"] == "agent"
        assert agent["label"] == "test_agent"

    def test_register_agent_with_interests(self, graph):
        """register_agent() creates LISTENS_TO edges for interests."""
        agent = graph.register_agent(
            interests=["climate", "energy"],
        )
        assert agent["type"] == "agent"


# ===== Batch Operations SDK Tests =====


class TestBatchCreateNodes:
    """Tests for SDK batch_create_nodes() method."""

    def test_batch_create_nodes(self, graph):
        """batch_create_nodes() creates multiple nodes at once."""
        nodes = graph.batch_create_nodes(
            nodes=[
                {"label": "Node A", "node_type": "concept"},
                {"label": "Node B", "node_type": "source"},
                {"label": "Node C", "node_type": "pattern"},
            ]
        )
        assert len(nodes) == 3
        labels = {n["label"] for n in nodes}
        assert labels == {"Node A", "Node B", "Node C"}


class TestBatchCreateEdges:
    """Tests for SDK batch_create_edges() method."""

    def test_batch_create_edges(self, graph):
        """batch_create_edges() creates multiple edges at once."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        c = graph.create_node(label="C")["id"]
        edges = graph.batch_create_edges(
            edges=[
                {"from_node": a, "to_node": b, "edge_type": "CAUSES", "layer": "L3"},
                {"from_node": b, "to_node": c, "edge_type": "INFLUENCES", "layer": "L2"},
            ]
        )
        assert len(edges) == 2
        types = {e["edge_type"] for e in edges}
        assert types == {"CAUSES", "INFLUENCES"}


class TestCreateBatch:
    """Tests for SDK create_batch() method (OHM-1m3)."""

    def test_create_batch_nodes_and_edges(self, graph):
        """create_batch() creates both nodes and edges in one call."""
        result = graph.create_batch(
            nodes=[
                {"label": "Event", "node_type": "event"},
                {"label": "Source", "node_type": "source"},
            ],
            edges=[],
        )
        assert result["nodes_created"] == 2
        assert result["edges_created"] == 0
        assert len(result["nodes"]) == 2

    def test_create_batch_with_edges(self, graph):
        """create_batch() creates nodes and edges together."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        result = graph.create_batch(
            nodes=[{"label": "C", "node_type": "concept"}],
            edges=[
                {"from_node": a, "to_node": b, "edge_type": "CAUSES", "layer": "L3"},
            ],
        )
        assert result["nodes_created"] == 1
        assert result["edges_created"] == 1
        assert len(result["nodes"]) == 1
        assert len(result["edges"]) == 1

    def test_create_batch_empty(self, graph):
        """create_batch() with no nodes or edges returns zeros."""
        result = graph.create_batch()
        assert result["nodes_created"] == 0
        assert result["edges_created"] == 0
        assert result["nodes"] == []
        assert result["edges"] == []

    def test_create_batch_populates_change_feed(self, graph):
        """create_batch() populates change feed for each item individually."""
        result = graph.create_batch(
            nodes=[
                {"label": "CF1", "node_type": "concept"},
                {"label": "CF2", "node_type": "concept"},
            ],
        )
        assert result["nodes_created"] == 2
        # Each node creation should have its own change feed entry
        rows = graph._conn.execute("SELECT row_id FROM ohm_change_feed WHERE table_name = 'ohm_nodes' ORDER BY occurred_at DESC").fetchall()
        node_ids = {n["id"] for n in result["nodes"]}
        changed_ids = {r[0] for r in rows}
        assert node_ids.issubset(changed_ids)


# ===== Medical Diagnosis SDK Tests =====


class TestRulesOut:
    """Tests for SDK rules_out() method."""

    def test_rules_out_creates_negates_edge(self, graph):
        """rules_out() creates a NEGATES edge."""
        finding = graph.create_node(label="fever_absent", node_type="concept")
        condition = graph.create_node(label="malaria", node_type="concept")
        edge = graph.rules_out(from_node=finding["id"], to_node=condition["id"])
        assert edge["edge_type"] == "NEGATES"
        assert edge["from_node"] == finding["id"]
        assert edge["to_node"] == condition["id"]

    def test_rules_out_with_confidence(self, graph):
        """rules_out() accepts confidence parameter."""
        finding = graph.create_node(label="normal_wbc", node_type="concept")
        condition = graph.create_node(label="bacterial_infection", node_type="concept")
        edge = graph.rules_out(from_node=finding["id"], to_node=condition["id"], confidence=0.85)
        assert edge["confidence"] == pytest.approx(0.85)


class TestDifferentialDiagnosis:
    """Tests for SDK differential_diagnosis() method."""

    def test_differential_diagnosis_returns_candidates(self, graph):
        """differential_diagnosis() returns ranked candidate conditions."""
        patient = graph.create_node(label="Patient", node_type="concept")
        condition = graph.create_node(label="Flu", node_type="concept")
        graph.create_edge(
            from_node=patient["id"],
            to_node=condition["id"],
            edge_type="SUPPORTS",
            layer="L3",
            confidence=0.8,
        )
        results = graph.differential_diagnosis(patient["id"])
        assert isinstance(results, list)

    def test_differential_diagnosis_empty(self, graph):
        """differential_diagnosis() returns empty for isolated node."""
        node = graph.create_node(label="Isolated", node_type="concept")
        results = graph.differential_diagnosis(node["id"])
        assert isinstance(results, list)
        assert len(results) == 0


class TestCompoundConfidence:
    """Tests for SDK compound_confidence() method."""

    def test_compound_confidence_independent(self, graph):
        """compound_confidence() with independent observations."""
        result = graph.compound_confidence(
            [{"confidence": 0.7}, {"confidence": 0.8}],
            correlation=0.0,
        )
        assert result["compound_confidence"] > 0.7
        assert "method" in result

    def test_compound_confidence_correlated(self, graph):
        """compound_confidence() with perfectly correlated observations."""
        result = graph.compound_confidence(
            [{"confidence": 0.7}, {"confidence": 0.8}],
            correlation=1.0,
        )
        assert result["compound_confidence"] == pytest.approx(0.8)

    def test_compound_confidence_single(self, graph):
        """compound_confidence() with single observation returns that confidence."""
        result = graph.compound_confidence(
            [{"confidence": 0.75}],
            correlation=0.0,
        )
        assert result["compound_confidence"] == pytest.approx(0.75)


# ===== Substrate Methods SDK Tests =====


class TestDecayObservations:
    """Tests for SDK decay_observations() method."""

    def test_decay_observations_dry_run(self, graph):
        """decay_observations() with dry_run returns decay info without modifying data."""
        node = graph.create_node(label="DecayTest", node_type="concept")
        graph.observe(node["id"], obs_type="measurement", value=1.0, sigma=0.1)
        result = graph.decay_observations(node["id"], dry_run=True)
        assert isinstance(result, list)

    def test_decay_observations_all_nodes(self, graph):
        """decay_observations() without node_id processes all observations."""
        node = graph.create_node(label="AllDecay", node_type="concept")
        graph.observe(node["id"], obs_type="measurement", value=1.0, sigma=0.1)
        result = graph.decay_observations(dry_run=True)
        assert isinstance(result, list)


class TestExpiringSoon:
    """Tests for SDK expiring_soon() method."""

    def test_expiring_soon_returns_list(self, graph):
        """expiring_soon() returns a list (empty if no batches)."""
        result = graph.expiring_soon()
        assert isinstance(result, list)


class TestCascadeScenario:
    """Tests for SDK cascade_scenario() method."""

    def test_cascade_scenario_returns_list(self, graph):
        """cascade_scenario() returns downstream impact analysis."""
        a = graph.create_node(label="Supplier A", node_type="concept")
        b = graph.create_node(label="Factory B", node_type="concept")
        graph.create_edge(
            from_node=a["id"],
            to_node=b["id"],
            edge_type="CAUSES",
            layer="L3",
            confidence=0.8,
        )
        result = graph.cascade_scenario(a["id"], failure_probability=0.5)
        assert isinstance(result, list)

    def test_cascade_scenario_empty(self, graph):
        """cascade_scenario() returns empty for isolated node."""
        node = graph.create_node(label="Isolated", node_type="concept")
        result = graph.cascade_scenario(node["id"])
        assert isinstance(result, list)
        assert len(result) == 0


class TestWhatIf:
    """Tests for SDK what_if() method."""

    def test_what_if_returns_dict(self, graph):
        """what_if() returns dry-run cascade analysis."""
        a = graph.create_node(label="A", node_type="concept")
        b = graph.create_node(label="B", node_type="concept")
        edge = graph.create_edge(
            from_node=a["id"],
            to_node=b["id"],
            edge_type="CAUSES",
            layer="L3",
            confidence=0.7,
        )
        result = graph.what_if(edge["id"])
        assert isinstance(result, dict)
        assert "trigger_edge" in result or "trigger_probability" in result or "downstream_impact" in result


class TestProvenance:
    """Tests for SDK provenance() method."""

    def test_provenance_returns_chain(self, graph):
        """provenance() traces source chain backward."""
        source = graph.create_node(label="Original Source", node_type="source")
        derived = graph.create_node(label="Derived Claim", node_type="concept")
        graph.create_edge(
            from_node=derived["id"],
            to_node=source["id"],
            edge_type="REFERENCES",
            layer="L2",
            confidence=0.9,
        )
        result = graph.provenance(derived["id"])
        assert isinstance(result, list)

    def test_provenance_empty(self, graph):
        """provenance() returns empty for node with no sources."""
        node = graph.create_node(label="Root Node", node_type="concept")
        result = graph.provenance(node["id"])
        assert isinstance(result, list)
        assert len(result) == 0


class TestEdgeHistory:
    """Tests for SDK edge_history() method."""

    def test_edge_history_returns_creation(self, graph):
        """edge_history() returns creation event."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        history = graph.edge_history(edge["id"])
        assert isinstance(history, list)
        assert len(history) >= 1
        assert history[0]["type"] == "created"

    def test_edge_history_not_found(self, graph):
        """edge_history() returns empty for nonexistent edge."""
        result = graph.edge_history("nonexistent_id")
        assert result == []


# ===== Monte Carlo SDK Tests =====


class TestMonteCarlo:
    """Tests for SDK monte_carlo() method."""

    def test_monte_carlo_returns_dict(self, graph):
        """monte_carlo() returns simulation results."""
        a = graph.create_node(label="Source", node_type="concept")
        b = graph.create_node(label="Target", node_type="concept")
        graph.create_edge(
            from_node=a["id"],
            to_node=b["id"],
            edge_type="CAUSES",
            layer="L3",
            confidence=0.8,
        )
        result = graph.monte_carlo(a["id"], simulations=100)
        assert isinstance(result, dict)
        assert "simulation_count" in result


# ===== Near Duplicates SDK Tests =====


class TestNearDuplicates:
    """Tests for SDK near_duplicates() method."""

    def test_near_duplicates_returns_list(self, graph):
        """near_duplicates() returns list of duplicate pairs."""
        result = graph.near_duplicates()
        assert isinstance(result, list)


# ===== Calibration SDK Tests =====


class TestCalibration:
    """Tests for SDK calibration() method."""

    def test_calibration_returns_dict(self, graph):
        """calibration() returns calibration metrics."""
        result = graph.calibration()
        assert isinstance(result, dict)
        assert "calibration_by_band" in result or "calibration_score" in result


# ===== Suggest Connections SDK Tests =====


class TestSuggestConnections:
    """Tests for SDK suggest_connections() method."""

    def test_suggest_connections_returns_list(self, graph):
        """suggest_connections() returns list of suggestions."""
        result = graph.suggest_connections()
        assert isinstance(result, list)


# ===== Export/Import SDK Tests =====


class TestExportImport:
    """Tests for SDK export_graph() and import_graph() methods."""

    def test_export_graph_returns_dict(self, graph):
        """export_graph() returns a dict with nodes, edges, and meta."""
        graph.create_node(label="Export Node", node_type="concept")
        result = graph.export_graph()
        assert isinstance(result, dict)
        assert "nodes" in result
        assert "edges" in result
        assert "meta" in result
        assert result["meta"]["node_count"] >= 1

    def test_import_graph_merge(self, graph):
        """import_graph() with merge=True adds nodes."""
        data = {
            "nodes": [
                {"id": "imported_1", "label": "Imported", "type": "concept", "content": None, "created_by": "test", "visibility": "team", "provenance": None, "confidence": 1.0, "priority": None},
            ],
            "edges": [],
            "observations": [],
        }
        result = graph.import_graph(data, merge=True)
        assert result["nodes"] >= 1

    def test_export_import_roundtrip(self, graph):
        """export then import preserves data."""
        graph.create_node(label="Roundtrip Node", node_type="concept")
        exported = graph.export_graph()
        # Import into fresh graph
        g2 = connect(":memory:", actor="importer")
        result = g2.import_graph(exported, merge=True)
        assert result["nodes"] >= 1
        g2.close()


# ===== Evolve Identity SDK Tests =====


class TestEvolveIdentity:
    """Tests for SDK evolve_identity() method."""

    def test_evolve_identity_creates_new_edge(self, graph):
        """evolve_identity() creates a new edge and marks old as superseded."""
        graph.register_agent(description="Test agent")
        # Find the VALUES edge created by register_agent
        edges = graph.search_edges(edge_type="VALUES")
        if edges:
            old_edge_id = edges[0]["id"]
            new_edge = graph.evolve_identity(old_edge_id, new_target="new_value", reason="values changed")
            assert new_edge["edge_type"] == "VALUES"
            assert new_edge["provenance"] is not None


# ===== Discover Peers SDK Tests =====


class TestDiscoverPeers:
    """Tests for SDK discover_peers() method."""

    def test_discover_peers_returns_list(self, graph):
        """discover_peers() returns list of peer agents."""
        graph.register_agent(description="Agent 1")
        result = graph.discover_peers()
        assert isinstance(result, list)

    def test_discover_peers_unregistered(self, graph):
        """discover_peers() returns empty for unregistered agent."""
        # Default actor "test_agent" is not registered
        result = graph.discover_peers()
        assert isinstance(result, list)


# ===== Health SDK Tests =====


class TestHealth:
    """Tests for SDK health() method."""

    def test_health_returns_dict(self, graph):
        """health() returns structural health metrics."""
        result = graph.health()
        assert isinstance(result, dict)


# ===== Agent Health SDK Tests =====


class TestAgentHealth:
    """Tests for SDK agent_health() method."""

    def test_agent_health_returns_list(self, graph):
        """agent_health() returns health status for agents."""
        graph.register_agent(description="Health test agent")
        result = graph.agent_health()
        assert isinstance(result, list)


# ===== Heartbeat SDK Tests =====


class TestHeartbeat:
    """Tests for SDK heartbeat() method."""

    def test_heartbeat_updates_agent(self, graph):
        """heartbeat() updates agent's last-seen timestamp."""
        graph.register_agent(description="Heartbeat test")
        result = graph.heartbeat()
        assert isinstance(result, dict)


# ===== Confidence Chain SDK Tests =====


class TestConfidenceChain:
    """Tests for SDK confidence_chain() method."""

    def test_confidence_chain_returns_dict(self, graph):
        """confidence_chain() traces evidence and computes aggregate."""
        a = graph.create_node(label="Evidence A", node_type="concept")
        b = graph.create_node(label="Claim B", node_type="concept")
        graph.create_edge(
            from_node=a["id"],
            to_node=b["id"],
            edge_type="SUPPORTS",
            layer="L3",
            confidence=0.8,
        )
        result = graph.confidence_chain(b["id"])
        assert isinstance(result, dict)
        assert "evidence_chain" in result or "aggregate_confidence" in result


class TestCompositeScore:
    """Tests for OHM-0e0.3: multiplicative composite score."""

    def test_composite_score_arithmetic_default(self, graph):
        """composite_score() defaults to arithmetic (backwards compatible)."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=0.8, sigma=0.1)
        result = graph.composite_score(a)
        assert result["method"] == "arithmetic"
        assert result["composite_score"] is not None
        assert result["observation_score"] is not None
        assert result["observation_count"] >= 1

    def test_composite_score_geometric(self, graph):
        """composite_score() with method='geometric' uses geometric mean."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=1.3, sigma=0.1)
        graph.observe(a, obs_type="measurement", value=1.5, sigma=0.1)
        result = graph.composite_score(a, method="geometric")
        assert result["method"] == "geometric"
        assert result["composite_score"] is not None

    def test_composite_score_geometric_with_baseline(self, graph):
        """composite_score() geometric with baseline scales result."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=2.0, sigma=0.1)
        result = graph.composite_score(a, method="geometric", baseline=2.0)
        assert result["method"] == "geometric"
        assert result["baseline"] == 2.0
        assert result["composite_score"] is not None

    def test_composite_score_arithmetic_explicit(self, graph):
        """composite_score() with method='arithmetic' explicitly."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=0.5, sigma=0.1)
        result = graph.composite_score(a, method="arithmetic")
        assert result["method"] == "arithmetic"
        assert result["composite_score"] is not None

    def test_composite_score_no_observations(self, graph):
        """composite_score() with no observations returns None composite."""
        a = graph.create_node(label="A")["id"]
        result = graph.composite_score(a)
        assert result["composite_score"] is None
        assert result["observation_count"] == 0

    def test_composite_score_weights_preserved(self, graph):
        """composite_score() preserves weight parameters in result."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=0.7, sigma=0.1)
        result = graph.composite_score(a, observation_weight=0.3, evidence_weight=0.7)
        assert result["weights"]["observation"] == 0.3
        assert result["weights"]["evidence"] == 0.7

    def test_composite_score_arithmetic_vs_geometric_differ(self, graph):
        """Arithmetic and geometric composite scores differ with evidence."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.observe(a, obs_type="measurement", value=0.8, sigma=0.1)
        graph.create_edge(from_node=b, to_node=a, edge_type="SUPPORTS", layer="L3", confidence=0.9)
        result_arith = graph.composite_score(a, method="arithmetic")
        result_geom = graph.composite_score(a, method="geometric")
        assert result_arith["composite_score"] != result_geom["composite_score"]

    def test_composite_score_geometric_with_both_scores(self, graph):
        """Geometric composite with both observation and evidence scores."""
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        graph.observe(a, obs_type="measurement", value=0.8, sigma=0.1)
        graph.create_edge(from_node=b, to_node=a, edge_type="SUPPORTS", layer="L3", confidence=0.9)
        result = graph.composite_score(a, method="geometric")
        assert result["method"] == "geometric"
        assert result["composite_score"] is not None
        assert result["observation_score"] is not None
        assert result["evidence_score"] is not None

    def test_composite_score_geometric_fallback_on_zero(self, graph):
        """Geometric mean falls back to arithmetic when values include zero."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=0.0, sigma=0.1)
        result = graph.composite_score(a, method="geometric")
        assert result["composite_score"] is not None


class TestContradictions:
    """Tests for contradictions() SDK method."""

    def test_contradictions_returns_dict(self, graph):
        """contradictions() returns a dict with contradiction categories."""
        results = graph.contradictions()
        assert isinstance(results, dict)
        assert "contradictory_interpretations" in results
        assert "high_confidence_challenges" in results
        assert "opposite_observations" in results

    def test_contradictions_empty_graph(self, graph):
        """contradictions() on empty graph returns empty categories."""
        results = graph.contradictions()
        assert results["contradictory_interpretations"] == []
        assert results["high_confidence_challenges"] == []
        assert results["opposite_observations"] == []


class TestDetectTrend:
    """Tests for detect_trend() SDK method."""

    def test_detect_trend_returns_dict(self, graph):
        """detect_trend() returns a dict with trend info."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=0.5, sigma=0.1)
        graph.observe(a, obs_type="measurement", value=0.6, sigma=0.1)
        graph.observe(a, obs_type="measurement", value=0.7, sigma=0.1)
        result = graph.detect_trend(a, window_days=60)
        assert isinstance(result, dict)
        assert "trend" in result

    def test_detect_trend_insufficient_observations(self, graph):
        """detect_trend() with too few observations returns stable."""
        a = graph.create_node(label="A")["id"]
        graph.observe(a, obs_type="measurement", value=0.5, sigma=0.1)
        result = graph.detect_trend(a, window_days=60, min_observations=5)
        assert isinstance(result, dict)


class TestUrgentChanges:
    """Tests for urgent_changes() SDK method."""

    def test_urgent_changes_returns_list(self, graph):
        """urgent_changes() returns a list."""
        results = graph.urgent_changes()
        assert isinstance(results, list)

    def test_urgent_changes_with_filter(self, graph):
        """urgent_changes() accepts urgency_filter parameter."""
        results = graph.urgent_changes(urgency_filter=["critical"])
        assert isinstance(results, list)


class TestHelp:
    """Tests for help() SDK method."""

    def test_help_returns_dict(self, graph):
        """help() returns a dict with usage info."""
        result = graph.help()
        assert isinstance(result, dict)
        assert "methods" in result or "commands" in result or "usage" in result


class TestOHMClient:
    """Tests for OHMClient (OHM-12w)."""

    def test_ohmclient_init_defaults(self):
        """OHMClient initializes with defaults."""
        from ohm.client import OHMClient

        client = OHMClient(actor="test")
        assert client.actor == "test"
        assert client.base_url == "http://127.0.0.1:8710"

    def test_ohmclient_init_with_base_url(self):
        """OHMClient accepts explicit base_url."""
        from ohm.client import OHMClient

        client = OHMClient(actor="test", base_url="http://localhost:9999")
        assert client.base_url == "http://localhost:9999"

    def test_ohmclient_init_with_token(self):
        """OHMClient accepts explicit token."""
        from ohm.client import OHMClient

        client = OHMClient(actor="test", token="test-token-123")
        assert client.token == "test-token-123"

    def test_ohmclient_repr(self):
        """OHMClient repr includes actor and base_url."""
        from ohm.client import OHMClient

        client = OHMClient(actor="metis")
        r = repr(client)
        assert "metis" in r
        assert "8710" in r

    def test_ohmclient_context_manager(self):
        """OHMClient supports context manager protocol."""
        from ohm.client import OHMClient

        with OHMClient(actor="test") as client:
            assert client.actor == "test"
        # Should close cleanly

    def test_resolve_token_from_env(self, monkeypatch):
        """_resolve_token reads from OHM_TOKEN env var."""
        monkeypatch.setenv("OHM_TOKEN", "env-token-456")
        from ohm.client import _resolve_token

        token = _resolve_token("metis", None)
        assert token == "env-token-456"

    def test_resolve_token_from_config(self):
        """_resolve_token reads from config tokens dict."""
        from ohm.client import _resolve_token

        config = {"tokens": {"metis": "config-token-789"}}
        token = _resolve_token("metis", config)
        assert token == "config-token-789"

    def test_resolve_token_wildcard_fallback(self):
        """_resolve_token falls back to wildcard token."""
        from ohm.client import _resolve_token

        config = {"tokens": {"*": "wildcard-token"}}
        token = _resolve_token("unknown_agent", config)
        assert token == "wildcard-token"

    def test_resolve_base_url_default(self):
        """_resolve_base_url returns default when no config."""
        from ohm.client import _resolve_base_url

        url = _resolve_base_url(None)
        assert url == "http://127.0.0.1:8710"

    def test_resolve_base_url_from_config(self):
        """_resolve_base_url reads from config."""
        from ohm.client import _resolve_base_url

        config = {"host": "10.0.0.1", "port": 9999}
        url = _resolve_base_url(config)
        assert url == "http://10.0.0.1:9999"


# ===== Delete Node/Edge SDK Tests =====


class TestDeleteNodeSDK:
    """Tests for SDK delete_node() method (OHM-cpi)."""

    def test_delete_node_removes_edges(self, graph):
        """delete_node() removes all edges referencing the node."""
        a = graph.create_node(label="DelA", node_type="concept")
        b = graph.create_node(label="DelB", node_type="concept")
        graph.create_edge(
            from_node=a["id"],
            to_node=b["id"],
            edge_type="CAUSES",
            layer="L3",
        )
        result = graph.delete_node(a["id"])
        assert result["deleted"] == a["id"]
        assert result["type"] == "node"
        assert result["edges_removed"] >= 1

    def test_delete_node_removes_incoming_edges(self, graph):
        """delete_node() removes edges where node is the target."""
        a = graph.create_node(label="SrcA", node_type="concept")
        b = graph.create_node(label="TgtB", node_type="concept")
        graph.create_edge(
            from_node=a["id"],
            to_node=b["id"],
            edge_type="CAUSES",
            layer="L3",
        )
        result = graph.delete_node(b["id"])
        assert result["edges_removed"] >= 1

    def test_delete_node_not_found(self, graph):
        """delete_node() raises NodeNotFoundError for nonexistent node."""
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            graph.delete_node("nonexistent_node_xyz")

    def test_delete_node_no_edges(self, graph):
        """delete_node() works on a node with no edges."""
        node = graph.create_node(label="Lonely", node_type="concept")
        result = graph.delete_node(node["id"])
        assert result["edges_removed"] == 0
        assert result["observations_removed"] == 0


class TestDeleteEdgeSDK:
    """Tests for SDK delete_edge() method (OHM-cpi)."""

    def test_delete_edge(self, graph):
        """delete_edge() removes an edge by ID."""
        a = graph.create_node(label="A", node_type="concept")
        b = graph.create_node(label="B", node_type="concept")
        edge = graph.create_edge(
            from_node=a["id"],
            to_node=b["id"],
            edge_type="CAUSES",
            layer="L3",
        )
        result = graph.delete_edge(edge["id"])
        assert result["deleted"] == edge["id"]
        assert result["type"] == "edge"

    def test_delete_edge_not_found(self, graph):
        """delete_edge() raises EdgeNotFoundError for nonexistent edge."""
        from ohm.exceptions import EdgeNotFoundError

        with pytest.raises(EdgeNotFoundError):
            graph.delete_edge("nonexistent_edge_xyz")
