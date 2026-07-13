"""Tests for OHM-846: Type-level promotion autoresearch loop.

Covers evaluate_type_proposal, promote_type, demote_type, and the
HTTP endpoints for the type promotion lifecycle.
"""

import json
import pytest

from tests.conftest import _request

pytestmark = pytest.mark.integration


@pytest.fixture
def seed_type_proposal_with_usage(test_server):
    """Create a type proposal with evidence nodes from multiple agents."""
    port, store = test_server
    conn = store.conn

    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, tags) VALUES
        ('n1', 'Signal A', 'concept', 'agent1', CURRENT_TIMESTAMP, '["proposed-type:signal"]'),
        ('n2', 'Signal B', 'concept', 'agent2', CURRENT_TIMESTAMP, '["proposed-type:signal"]'),
        ('n3', 'Signal C', 'concept', 'agent1', CURRENT_TIMESTAMP, '["proposed-type:signal"]')
        """
    )
    conn.execute(
        """INSERT INTO ohm_type_proposals (id, proposed_type, status, proposed_by, evidence_node_ids, tenant_id)
           VALUES ('prop1', 'signal', 'trial', 'agent1', '["n1", "n2", "n3"]', '')
        """
    )
    conn.commit()
    return port, store


class TestEvaluateTypeProposal:
    """evaluate_type_proposal query function."""

    def test_evaluate_ready(self, test_db):
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, tags) VALUES
            ('n1', 'A', 'concept', 'agent1', CURRENT_TIMESTAMP, '["proposed-type:signal"]'),
            ('n2', 'B', 'concept', 'agent2', CURRENT_TIMESTAMP, '["proposed-type:signal"]'),
            ('n3', 'C', 'concept', 'agent1', CURRENT_TIMESTAMP, '["proposed-type:signal"]')
            """
        )
        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop1', 'signal', 'trial', '[\"n1\", \"n2\", \"n3\"]')"
        )
        test_db.commit()

        result = evaluate_type_proposal(test_db, proposal_id="prop1")
        assert result["ready"] is True
        assert result["metrics"]["distinct_agents"] == 2
        assert result["metrics"]["evidence_count"] == 3

    def test_evaluate_not_enough_agents(self, test_db):
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, tags) VALUES
            ('n1', 'A', 'concept', 'agent1', CURRENT_TIMESTAMP, '["proposed-type:signal"]'),
            ('n2', 'B', 'concept', 'agent1', CURRENT_TIMESTAMP, '["proposed-type:signal"]')
            """
        )
        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop2', 'signal', 'trial', '[\"n1\", \"n2\"]')"
        )
        test_db.commit()

        result = evaluate_type_proposal(test_db, proposal_id="prop2")
        assert result["ready"] is False
        assert "distinct agent" in result["reason"].lower()

    def test_evaluate_not_enough_evidence(self, test_db):
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, tags) VALUES
            ('n1', 'A', 'concept', 'agent1', CURRENT_TIMESTAMP, '["proposed-type:signal"]'),
            ('n2', 'B', 'concept', 'agent2', CURRENT_TIMESTAMP, '["proposed-type:signal"]')
            """
        )
        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop3', 'signal', 'trial', '[\"n1\", \"n2\"]')"
        )
        test_db.commit()

        result = evaluate_type_proposal(test_db, proposal_id="prop3", min_evidence_nodes=3)
        assert result["ready"] is False
        assert "evidence" in result["reason"].lower()

    def test_evaluate_not_found(self, test_db):
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        with pytest.raises(ValueError, match="not found"):
            evaluate_type_proposal(test_db, proposal_id="nonexistent")

    def test_evaluate_wrong_status(self, test_db):
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop4', 'signal', 'promoted', '[]')"
        )
        test_db.commit()

        result = evaluate_type_proposal(test_db, proposal_id="prop4")
        assert result["ready"] is False
        assert "promoted" in result["reason"]

    def test_evaluate_no_evidence(self, test_db):
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop5', 'signal', 'trial', '[]')"
        )
        test_db.commit()

        result = evaluate_type_proposal(test_db, proposal_id="prop5")
        assert result["ready"] is False
        assert "no evidence" in result["reason"].lower()


class TestPromoteType:
    """promote_type query function."""

    def test_promote_adds_to_valid_node_types(self, test_db):
        from ohm.graph.queries.type_proposals import promote_type
        import ohm.graph.schema as schema_mod

        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop1', 'signal', 'trial', '[]')"
        )
        test_db.commit()

        assert "signal" not in schema_mod.VALID_NODE_TYPES
        result = promote_type(test_db, proposal_id="prop1")
        assert result["status"] == "promoted"
        assert "signal" in schema_mod.VALID_NODE_TYPES

        schema_mod.VALID_NODE_TYPES = schema_mod.VALID_NODE_TYPES - frozenset({"signal"})

    def test_promote_updates_status(self, test_db):
        from ohm.graph.queries.type_proposals import promote_type, get_type_proposal
        import ohm.graph.schema as schema_mod

        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop2', 'indicator', 'trial', '[]')"
        )
        test_db.commit()

        promote_type(test_db, proposal_id="prop2")
        proposal = get_type_proposal(test_db, proposal_id="prop2")
        assert proposal["status"] == "promoted"

        schema_mod.VALID_NODE_TYPES = schema_mod.VALID_NODE_TYPES - frozenset({"indicator"})

    def test_promote_wrong_status_raises(self, test_db):
        from ohm.graph.queries.type_proposals import promote_type

        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop3', 'signal', 'rejected', '[]')"
        )
        test_db.commit()

        with pytest.raises(ValueError, match="not 'trial'"):
            promote_type(test_db, proposal_id="prop3")

    def test_promote_already_valid_type(self, test_db):
        from ohm.graph.queries.type_proposals import promote_type

        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop4', 'concept', 'trial', '[]')"
        )
        test_db.commit()

        result = promote_type(test_db, proposal_id="prop4")
        assert result["status"] == "promoted"
        assert result["in_valid_node_types"] is True


class TestDemoteType:
    """demote_type query function."""

    def test_demote_sets_rejected(self, test_db):
        from ohm.graph.queries.type_proposals import demote_type, get_type_proposal

        test_db.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop1', 'signal', 'trial', '[]')"
        )
        test_db.commit()

        result = demote_type(test_db, proposal_id="prop1", reason="Not useful")
        assert result["status"] == "rejected"
        assert "Not useful" in result["reason"]

        proposal = get_type_proposal(test_db, proposal_id="prop1")
        assert proposal["status"] == "rejected"

    def test_demote_not_found(self, test_db):
        from ohm.graph.queries.type_proposals import demote_type

        with pytest.raises(ValueError, match="not found"):
            demote_type(test_db, proposal_id="nonexistent")


class TestHTTPIntegration:
    """HTTP endpoint tests."""

    def test_get_type_proposals(self, seed_type_proposal_with_usage):
        port, _ = seed_type_proposal_with_usage
        status, data = _request("GET", port, "/type-proposals", None)
        assert status == 200
        assert data["count"] >= 1

    def test_get_type_proposals_filter_status(self, seed_type_proposal_with_usage):
        port, _ = seed_type_proposal_with_usage
        status, data = _request("GET", port, "/type-proposals?status=trial", None)
        assert status == 200
        assert all(r["status"] == "trial" for r in data["results"])

    def test_evaluate_via_http(self, seed_type_proposal_with_usage):
        port, _ = seed_type_proposal_with_usage
        status, data = _request("POST", port, "/type-proposal/prop1/evaluate", {})
        assert status == 200
        assert data["ready"] is True
        assert data["proposed_type"] == "signal"

    def test_promote_via_http(self, seed_type_proposal_with_usage):
        import ohm.graph.schema as schema_mod
        port, _ = seed_type_proposal_with_usage
        status, data = _request("POST", port, "/type-proposal/prop1/promote", {})
        assert status == 200
        assert data["status"] == "promoted"
        assert "signal" in schema_mod.VALID_NODE_TYPES
        schema_mod.VALID_NODE_TYPES = schema_mod.VALID_NODE_TYPES - frozenset({"signal"})

    def test_demote_via_http(self, seed_type_proposal_with_usage):
        port, _ = seed_type_proposal_with_usage
        status, data = _request("POST", port, "/type-proposal/prop1/demote", {
            "reason": "Testing demotion",
        })
        assert status == 200
        assert data["status"] == "rejected"

    def test_evaluate_nonexistent_via_http(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/type-proposal/nonexistent/evaluate", {})
        assert status == 422

    def test_unknown_action_via_http(self, test_server):
        port, store = test_server
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status) VALUES "
            "('propx', 'test', 'trial')"
        )
        conn.commit()
        status, data = _request("POST", port, "/type-proposal/propx/unknown", {})
        assert status in (400, 422, 500)

    def test_promoted_type_can_create_nodes(self, test_server):
        import ohm.graph.schema as schema_mod
        port, store = test_server
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_type_proposals (id, proposed_type, status, evidence_node_ids) VALUES "
            "('prop_p', 'mytype', 'trial', '[]')"
        )
        conn.commit()

        _request("POST", port, "/type-proposal/prop_p/promote", {})

        from ohm.queries import create_node
        node = create_node(store.conn, label="Test MyType", node_type="mytype", created_by="test")
        assert node["type"] == "mytype"

        schema_mod.VALID_NODE_TYPES = schema_mod.VALID_NODE_TYPES - frozenset({"mytype"})

    def test_demoted_prevents_reuse(self, seed_type_proposal_with_usage):
        port, store = seed_type_proposal_with_usage
        _request("POST", port, "/type-proposal/prop1/demote", {"reason": "test"})

        proposals = store.read_conn.execute(
            "SELECT status FROM ohm_type_proposals WHERE id = 'prop1'"
        ).fetchone()
        assert proposals[0] == "rejected"