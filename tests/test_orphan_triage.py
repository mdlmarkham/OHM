from __future__ import annotations

import pytest

from ohm.queries import batch_orphan_triage, create_edge, create_node, query_graph_health


class TestBatchOrphanTriage:
    def test_no_orphans(self, test_db):
        result = batch_orphan_triage(test_db)
        assert result["triaged_count"] == 0
        assert result["total_orphans"] == 0
        assert result["suggestions"] == []
        assert result["with_suggestions"] == 0
        assert result["without_suggestions"] == 0
        assert result["method"] == "batch_orphan_triage"

    def test_single_orphan_no_matches(self, test_db):
        create_node(test_db, label="Lonely node", node_type="concept", created_by="test")
        result = batch_orphan_triage(test_db)
        assert result["triaged_count"] == 1
        assert result["total_orphans"] == 1
        assert result["with_suggestions"] == 0
        assert result["without_suggestions"] == 1
        assert result["types_seen"] == {"concept": 1}

    def test_orphan_with_same_type_match(self, test_db):
        orphan = create_node(test_db, label="Orphan concept", node_type="concept", created_by="test")
        connected = create_node(test_db, label="Connected concept", node_type="concept", created_by="test")
        other = create_node(test_db, label="Other", node_type="source", created_by="test")
        create_edge(test_db, from_node=connected["id"], to_node=other["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")
        result = batch_orphan_triage(test_db)
        assert result["triaged_count"] == 1
        assert result["with_suggestions"] == 1
        entry = result["suggestions"][0]
        assert entry["orphan_id"] == orphan["id"]
        assert any("Same type" in s["reason"] for s in entry["suggestions"])

    def test_orphan_with_label_overlap(self, test_db):
        orphan = create_node(test_db, label="oil supply disruption persian gulf", node_type="pattern", created_by="test")
        conn_node = create_node(test_db, label="oil supply disruption hormuz", node_type="source", created_by="test")
        other = create_node(test_db, label="Other", node_type="concept", created_by="test")
        create_edge(test_db, from_node=conn_node["id"], to_node=other["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")
        result = batch_orphan_triage(test_db)
        assert result["triaged_count"] == 1
        entry = result["suggestions"][0]
        label_suggestions = [s for s in entry["suggestions"] if "Label overlap" in s["reason"]]
        assert len(label_suggestions) >= 1

    def test_exclude_fragment_nodes(self, test_db):
        create_node(test_db, label="Fragment", node_type="fragment", created_by="test")
        result = batch_orphan_triage(test_db)
        assert result["triaged_count"] == 0

    def test_exclude_agent_nodes(self, test_db):
        create_node(test_db, label="Agent", node_type="agent", created_by="test")
        result = batch_orphan_triage(test_db)
        assert result["triaged_count"] == 0

    def test_min_confidence_filter(self, test_db):
        low = create_node(test_db, label="Low conf", node_type="concept", created_by="test", confidence=0.2)
        high = create_node(test_db, label="High conf", node_type="concept", created_by="test", confidence=0.8)
        result = batch_orphan_triage(test_db, min_confidence=0.5)
        assert result["triaged_count"] == 1
        assert result["suggestions"][0]["orphan_id"] == high["id"]

    def test_limit(self, test_db):
        for i in range(5):
            create_node(test_db, label=f"Orphan {i}", node_type="concept", created_by="test")
        result = batch_orphan_triage(test_db, limit=3)
        assert result["triaged_count"] == 3
        assert result["total_orphans"] == 5

    def test_suggestions_sorted_by_score(self, test_db):
        orphan = create_node(test_db, label="oil supply disruption persian gulf", node_type="concept", created_by="test")
        c1 = create_node(test_db, label="oil supply disruption hormuz strait", node_type="concept", created_by="test")
        src = create_node(test_db, label="Src", node_type="source", created_by="test")
        create_edge(test_db, from_node=c1["id"], to_node=src["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")
        c2 = create_node(test_db, label="oil demand analysis persian", node_type="concept", created_by="test")
        create_edge(test_db, from_node=c2["id"], to_node=src["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")
        result = batch_orphan_triage(test_db)
        if result["with_suggestions"] > 0:
            entry = result["suggestions"][0]
            scores = [s["score"] for s in entry["suggestions"]]
            assert scores == sorted(scores, reverse=True)

    def test_suggestions_capped_at_3(self, test_db):
        src = create_node(test_db, label="Src", node_type="source", created_by="test")
        create_node(test_db, label="Orphan", node_type="concept", created_by="test")
        for i in range(5):
            cn = create_node(test_db, label=f"Connected {i}", node_type="concept", created_by="test")
            create_edge(test_db, from_node=cn["id"], to_node=src["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")
        result = batch_orphan_triage(test_db)
        if result["with_suggestions"] > 0:
            assert len(result["suggestions"][0]["suggestions"]) <= 3


class TestOrphanTypeBreakdown:
    def test_health_includes_type_breakdown(self, test_db):
        create_node(test_db, label="Concept 1", node_type="concept", created_by="test")
        create_node(test_db, label="Pattern 1", node_type="pattern", created_by="test")
        result = query_graph_health(test_db)
        assert "orphan_type_breakdown" in result
        assert result["orphan_type_breakdown"].get("concept") == 1
        assert result["orphan_type_breakdown"].get("pattern") == 1

    def test_empty_graph_breakdown(self, test_db):
        result = query_graph_health(test_db)
        assert result["orphan_type_breakdown"] == {}

    def test_connected_nodes_not_in_breakdown(self, test_db):
        c1 = create_node(test_db, label="Connected", node_type="concept", created_by="test")
        src = create_node(test_db, label="Src", node_type="source", created_by="test")
        create_edge(test_db, from_node=c1["id"], to_node=src["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")
        result = query_graph_health(test_db)
        assert result["orphan_type_breakdown"] == {}


class TestSDKOrphanTriage:
    def test_sdk_orphan_triage(self, test_db):
        from ohm.sdk import connect

        with connect(":memory:", actor="test") as graph:
            result = graph.orphan_triage(limit=10)
            assert "triaged_count" in result
            assert "suggestions" in result
            assert result["method"] == "batch_orphan_triage"

    def test_sdk_orphan_triage_with_nodes(self, test_db):
        from ohm.sdk import connect

        with connect(":memory:", actor="test") as graph:
            graph.create_node("Orphan concept", node_type="concept")
            result = graph.orphan_triage(limit=10)
            assert result["triaged_count"] == 1
            assert result["types_seen"].get("concept") == 1
