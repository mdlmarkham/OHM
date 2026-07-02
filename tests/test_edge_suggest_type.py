"""Tests for OHM-ezt5: /edge/suggest-type endpoint + participates_in_inference flag."""

from __future__ import annotations

import pytest

from tests.conftest import _request
from ohm.graph.queries import suggest_edge_type
from ohm.graph.constraints import EDGE_CONSTRAINTS


@pytest.fixture
def test_db():
    import duckdb

    from ohm.graph.schema import initialize_schema

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


class TestParticipatesInInference:
    """EDGE_CONSTRAINTS has participates_in_inference flag."""

    def test_causes_participates(self):
        assert EDGE_CONSTRAINTS["CAUSES"]["participates_in_inference"] is True

    def test_predicts_participates(self):
        assert EDGE_CONSTRAINTS["PREDICTS"]["participates_in_inference"] is True

    def test_correlates_with_participates(self):
        assert EDGE_CONSTRAINTS["CORRELATES_WITH"]["participates_in_inference"] is True

    def test_supports_does_not_participate(self):
        assert EDGE_CONSTRAINTS["SUPPORTS"]["participates_in_inference"] is False

    def test_challenged_by_does_not_participate(self):
        assert EDGE_CONSTRAINTS["CHALLENGED_BY"]["participates_in_inference"] is False

    def test_refines_does_not_participate(self):
        assert EDGE_CONSTRAINTS["REFINES"]["participates_in_inference"] is False

    def test_explains_does_not_participate(self):
        assert EDGE_CONSTRAINTS["EXPLAINS"]["participates_in_inference"] is False

    def test_references_does_not_participate(self):
        assert EDGE_CONSTRAINTS["REFERENCES"]["participates_in_inference"] is False


class TestSuggestEdgeType:
    """suggest_edge_type() recommends the correct edge type."""

    def test_pattern_to_decision_suggests_refines(self, test_db):
        from ohm.graph.queries import create_node

        pattern = create_node(test_db, label="AND→OR pattern", node_type="pattern", created_by="test")
        decision = create_node(test_db, label="Switch to OR", node_type="decision", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=pattern["id"], to_node_id=decision["id"])
        assert result["suggested_edge_type"] == "REFINES"
        assert result["participates_in_inference"] is False

    def test_pattern_to_case_suggests_refines(self, test_db):
        from ohm.graph.queries import create_node

        pattern = create_node(test_db, label="Pattern", node_type="pattern", created_by="test")
        case = create_node(test_db, label="Case", node_type="task", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=pattern["id"], to_node_id=case["id"])
        assert result["suggested_edge_type"] == "REFINES"

    def test_pattern_to_concept_suggests_explains(self, test_db):
        from ohm.graph.queries import create_node

        pattern = create_node(test_db, label="Pattern", node_type="pattern", created_by="test")
        concept = create_node(test_db, label="Concept", node_type="concept", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=pattern["id"], to_node_id=concept["id"])
        assert result["suggested_edge_type"] == "EXPLAINS"

    def test_source_to_concept_suggests_references(self, test_db):
        from ohm.graph.queries import create_node

        source = create_node(test_db, label="Source", node_type="source", created_by="test")
        concept = create_node(test_db, label="Concept", node_type="concept", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=source["id"], to_node_id=concept["id"])
        assert result["suggested_edge_type"] == "REFERENCES"
        assert result["suggested_layer"] == "L2"

    def test_experiment_to_hypothesis_suggests_tests(self, test_db):
        from ohm.graph.queries import create_node

        exp = create_node(test_db, label="Experiment", node_type="experiment", created_by="test")
        hyp = create_node(test_db, label="Hypothesis", node_type="hypothesis", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=exp["id"], to_node_id=hyp["id"])
        assert result["suggested_edge_type"] == "TESTS"

    def test_decision_to_hypothesis_suggests_depends_on(self, test_db):
        from ohm.graph.queries import create_node

        dec = create_node(test_db, label="Decision", node_type="decision", created_by="test")
        hyp = create_node(test_db, label="Hypothesis", node_type="hypothesis", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=dec["id"], to_node_id=hyp["id"])
        assert result["suggested_edge_type"] == "DECISION_DEPENDS_ON"

    def test_default_suggests_related_to(self, test_db):
        from ohm.graph.queries import create_node

        n1 = create_node(test_db, label="Concept A", node_type="concept", created_by="test")
        n2 = create_node(test_db, label="Concept B", node_type="concept", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=n1["id"], to_node_id=n2["id"])
        assert result["suggested_edge_type"] == "RELATED_TO"

    def test_returns_from_and_to_types(self, test_db):
        from ohm.graph.queries import create_node

        pattern = create_node(test_db, label="Pattern", node_type="pattern", created_by="test")
        concept = create_node(test_db, label="Concept", node_type="concept", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=pattern["id"], to_node_id=concept["id"])
        assert result["from_type"] == "pattern"
        assert result["to_type"] == "concept"

    def test_returns_reasoning(self, test_db):
        from ohm.graph.queries import create_node

        pattern = create_node(test_db, label="Pattern", node_type="pattern", created_by="test")
        decision = create_node(test_db, label="Decision", node_type="decision", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=pattern["id"], to_node_id=decision["id"])
        assert "REFINES" in result["reasoning"]
        assert "CAUSES" in result["reasoning"]

    def test_returns_alternatives(self, test_db):
        from ohm.graph.queries import create_node

        pattern = create_node(test_db, label="Pattern", node_type="pattern", created_by="test")
        decision = create_node(test_db, label="Decision", node_type="decision", created_by="test")
        result = suggest_edge_type(test_db, from_node_id=pattern["id"], to_node_id=decision["id"])
        assert isinstance(result["alternatives"], list)
        assert len(result["alternatives"]) >= 1

    def test_nonexistent_from_node_raises(self, test_db):
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            suggest_edge_type(test_db, from_node_id="nonexistent", to_node_id="also_nonexistent")

    def test_nonexistent_to_node_raises(self, test_db):
        from ohm.graph.queries import create_node
        from ohm.exceptions import NodeNotFoundError

        n1 = create_node(test_db, label="N1", node_type="concept", created_by="test")
        with pytest.raises(NodeNotFoundError):
            suggest_edge_type(test_db, from_node_id=n1["id"], to_node_id="nonexistent")


class TestEdgeSuggestTypeEndpoint:
    """GET /edge/suggest-type HTTP endpoint."""

    def test_endpoint_returns_suggestion(self, test_server):
        port, store = test_server
        store.write_node("pattern_1", "Pattern", "pattern", agent_name="test")
        store.write_node("decision_1", "Decision", "decision", agent_name="test")
        status, data = _request("GET", port, "/edge/suggest-type?from=pattern_1&to=decision_1")
        assert status == 200
        assert data["ok"] is True
        assert data["data"]["suggested_edge_type"] == "REFINES"

    def test_endpoint_missing_from(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/edge/suggest-type?to=some_node")
        assert status in (400, 422)

    def test_endpoint_missing_to(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/edge/suggest-type?from=some_node")
        assert status in (400, 422)

    def test_endpoint_nonexistent_node(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/edge/suggest-type?from=nope1&to=nope2")
        assert status == 404

    def test_endpoint_returns_participates_flag(self, test_server):
        port, store = test_server
        store.write_node("c1", "Concept A", "concept", agent_name="test")
        store.write_node("c2", "Concept B", "concept", agent_name="test")
        status, data = _request("GET", port, "/edge/suggest-type?from=c1&to=c2")
        assert status == 200
        assert "participates_in_inference" in data["data"]

    def test_endpoint_pattern_to_concept_explains(self, test_server):
        port, store = test_server
        store.write_node("p1", "Pattern", "pattern", agent_name="test")
        store.write_node("c1", "Concept", "concept", agent_name="test")
        status, data = _request("GET", port, "/edge/suggest-type?from=p1&to=c1")
        assert status == 200
        assert data["data"]["suggested_edge_type"] == "EXPLAINS"
        assert data["data"]["participates_in_inference"] is False
