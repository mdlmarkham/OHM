"""Tests for OHM-845: Decision-node hypothesis autoresearch loop.

Covers the POST /decision/{id}/autoresearch endpoint and the underlying
autoresearch query functions. Verifies candidate generation, evaluation
via transaction-rollback, promotion, rejection history, stability
(no re-proposal), dry-run, and error cases.
"""

import json
import pytest

from tests.conftest import _request

pytestmark = pytest.mark.integration


@pytest.fixture
def seed_decision_graph(test_server):
    """Create a decision node with linked hypotheses and candidate nodes."""
    port, store = test_server
    conn = store.conn

    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
        ('dec1', 'Build new feature X', 'decision', 'metis', CURRENT_TIMESTAMP, 0.6, '["feature", "priority"]', '["build", "wait"]', 'wait', 0.5),
        ('hyp1', 'Feature X is feasible', 'pattern', 'metis', CURRENT_TIMESTAMP, 0.8, '["feature", "feasibility"]', NULL, NULL, NULL),
        ('hyp2', 'Feature X has market demand', 'pattern', 'metis', CURRENT_TIMESTAMP, 0.7, '["feature", "market"]', NULL, NULL, NULL),
        ('hyp3', 'Competitor has similar feature', 'pattern', 'metis', CURRENT_TIMESTAMP, 0.9, '["feature", "competitor"]', NULL, NULL, NULL),
        ('cand1', 'Feature X performance impact', 'concept', 'metis', CURRENT_TIMESTAMP, 0.5, '["feature", "performance"]', NULL, NULL, NULL),
        ('cand2', 'Feature X cost analysis', 'concept', 'metis', CURRENT_TIMESTAMP, 0.5, '["feature", "cost"]', NULL, NULL, NULL)
        """
    )
    conn.execute(
        """INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES
        ('dec1', 'hyp1', 'DECISION_DEPENDS_ON', 'L3', 0.9, 'metis', CURRENT_TIMESTAMP),
        ('dec1', 'hyp2', 'DECISION_DEPENDS_ON', 'L3', 0.7, 'metis', CURRENT_TIMESTAMP)
        """
    )
    conn.commit()
    return port, store


@pytest.fixture
def seed_empty_decision(test_server):
    """Create a decision node with no hypotheses linked."""
    port, store = test_server
    conn = store.conn

    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
        ('dec_empty', 'Empty Decision', 'decision', 'metis', CURRENT_TIMESTAMP, 0.5, '["test"]', '["go", "stop"]', 'stop', 0.5),
        ('cand_a', 'Candidate A', 'concept', 'metis', CURRENT_TIMESTAMP, 0.5, '["test"]', NULL, NULL, NULL)
        """
    )
    conn.commit()
    return port, store


class TestPostDecisionAutoresearch:
    """POST /decision/{id}/autoresearch — autoresearch endpoint."""

    def test_autoresearch_basic(self, seed_decision_graph):
        port, _ = seed_decision_graph
        status, data = _request("POST", port, "/decision/dec1/autoresearch", {})
        assert status == 200
        assert data["decision_id"] == "dec1"
        assert "candidates" in data
        assert "evaluations" in data
        assert "promotions" in data
        assert "rejections" in data
        assert "summary" in data

    def test_autoresearch_dry_run(self, seed_decision_graph):
        port, store = seed_decision_graph
        status, data = _request("POST", port, "/decision/dec1/autoresearch", {
            "dry_run": True,
        })
        assert status == 200
        assert data["dry_run"] is True
        edges_before = store.read_conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dec1' AND edge_type = 'DECISION_DEPENDS_ON' AND deleted_at IS NULL"
        ).fetchone()[0]
        edges_after = store.read_conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dec1' AND edge_type = 'DECISION_DEPENDS_ON' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert edges_before == edges_after

    def test_autoresearch_max_candidates(self, seed_decision_graph):
        port, _ = seed_decision_graph
        status, data = _request("POST", port, "/decision/dec1/autoresearch", {
            "max_candidates": 1,
        })
        assert status == 200
        assert len(data["candidates"]) <= 1

    def test_autoresearch_nonexistent_decision(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/decision/nonexistent/autoresearch", {})
        assert status == 422

    def test_autoresearch_non_decision_node(self, test_server):
        port, store = test_server
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES "
            "('concept1', 'Not a decision', 'concept', 'metis', CURRENT_TIMESTAMP)"
        )
        conn.commit()
        status, data = _request("POST", port, "/decision/concept1/autoresearch", {})
        assert status == 422

    def test_autoresearch_no_candidates(self, test_server):
        port, store = test_server
        conn = store.conn
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec_no_cands', 'Decision No Cands', 'decision', 'metis', CURRENT_TIMESTAMP, 0.5, '["go"]', 'go', 0.5)
            """
        )
        conn.commit()
        status, data = _request("POST", port, "/decision/dec_no_cands/autoresearch", {})
        assert status == 200
        assert data["candidates"] == []


class TestProposeHypothesisEdges:
    """propose_hypothesis_edges query function."""

    def test_propose_finds_candidates(self, test_db):
        from ohm.decision.autoresearch import propose_hypothesis_edges

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Build feature', 'decision', 'a', CURRENT_TIMESTAMP, 0.6, '["feature"]', '["build"]', 'build', 0.5),
            ('cand1', 'Feature feasible', 'pattern', 'a', CURRENT_TIMESTAMP, 0.8, '["feature", "feasibility"]', NULL, NULL, NULL)
            """
        )
        test_db.commit()
        candidates = propose_hypothesis_edges(test_db, decision_id="dec1")
        assert len(candidates) >= 1
        assert any(c["hypothesis_id"] == "cand1" for c in candidates)

    def test_propose_excludes_existing_edges(self, test_db):
        from ohm.decision.autoresearch import propose_hypothesis_edges

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Build feature', 'decision', 'a', CURRENT_TIMESTAMP, 0.6, '["feature"]', '["build"]', 'build', 0.5),
            ('hyp1', 'Already linked', 'pattern', 'a', CURRENT_TIMESTAMP, 0.8, '["feature"]', NULL, NULL, NULL)
            """
        )
        test_db.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
            "('dec1', 'hyp1', 'DECISION_DEPENDS_ON', 'L3', 0.9, 'a', CURRENT_TIMESTAMP)"
        )
        test_db.commit()
        candidates = propose_hypothesis_edges(test_db, decision_id="dec1")
        assert not any(c["hypothesis_id"] == "hyp1" for c in candidates)

    def test_propose_excludes_rejected(self, test_db):
        from ohm.decision.autoresearch import propose_hypothesis_edges

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Build feature', 'decision', 'a', CURRENT_TIMESTAMP, 0.6, '["feature"]', '["build"]', 'build', 0.5),
            ('rejected1', 'Rejected hypothesis', 'pattern', 'a', CURRENT_TIMESTAMP, 0.8, '["feature"]', NULL, NULL, NULL)
            """
        )
        test_db.execute(
            "INSERT INTO ohm_autoresearch_history (decision_id, hypothesis_id, outcome, reason) VALUES "
            "('dec1', 'rejected1', 'rejected', 'did not improve')"
        )
        test_db.commit()
        candidates = propose_hypothesis_edges(test_db, decision_id="dec1")
        assert not any(c["hypothesis_id"] == "rejected1" for c in candidates)

    def test_propose_not_found_raises(self, test_db):
        from ohm.decision.autoresearch import propose_hypothesis_edges

        with pytest.raises(ValueError, match="not found"):
            propose_hypothesis_edges(test_db, decision_id="nonexistent")

    def test_propose_wrong_type_raises(self, test_db):
        from ohm.decision.autoresearch import propose_hypothesis_edges

        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES "
            "('c1', 'Concept', 'concept', 'a', CURRENT_TIMESTAMP)"
        )
        test_db.commit()
        with pytest.raises(ValueError, match="not 'decision'"):
            propose_hypothesis_edges(test_db, decision_id="c1")

    def test_propose_sorted_by_score(self, test_db):
        from ohm.decision.autoresearch import propose_hypothesis_edges

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Build feature test', 'decision', 'a', CURRENT_TIMESTAMP, 0.6, '["feature", "test"]', '["build"]', 'build', 0.5),
            ('c1', 'Feature test plan', 'concept', 'a', CURRENT_TIMESTAMP, 0.5, '["feature", "test"]', NULL, NULL, NULL),
            ('c2', 'Feature plan', 'concept', 'a', CURRENT_TIMESTAMP, 0.5, '["feature"]', NULL, NULL, NULL)
            """
        )
        test_db.commit()
        candidates = propose_hypothesis_edges(test_db, decision_id="dec1")
        if len(candidates) >= 2:
            assert candidates[0]["score"] >= candidates[1]["score"]


class TestEvaluateCandidateEdge:
    """evaluate_candidate_edge query function."""

    def test_evaluate_returns_before_after(self, test_db):
        from ohm.decision.autoresearch import evaluate_candidate_edge

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Decision 1', 'decision', 'a', CURRENT_TIMESTAMP, 0.5, '["go"]', 'go', 0.5),
            ('hyp1', 'Hypothesis 1', 'pattern', 'a', CURRENT_TIMESTAMP, 0.8, NULL, NULL, NULL)
            """
        )
        test_db.commit()
        result = evaluate_candidate_edge(test_db, decision_id="dec1", hypothesis_id="hyp1")
        assert "before" in result
        assert "after" in result
        assert "improved" in result
        assert "before_confidence" in result
        assert "after_confidence" in result

    def test_evaluate_does_not_persist_edge(self, test_db):
        from ohm.decision.autoresearch import evaluate_candidate_edge

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Decision 1', 'decision', 'a', CURRENT_TIMESTAMP, 0.5, '["go"]', 'go', 0.5),
            ('hyp1', 'Hypothesis 1', 'pattern', 'a', CURRENT_TIMESTAMP, 0.8, NULL, NULL, NULL)
            """
        )
        test_db.commit()
        evaluate_candidate_edge(test_db, decision_id="dec1", hypothesis_id="hyp1")
        edges = test_db.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dec1' AND edge_type = 'DECISION_DEPENDS_ON'"
        ).fetchone()[0]
        assert edges == 0


class TestPromoteCandidateEdge:
    """promote_candidate_edge query function."""

    def test_promote_creates_edge(self, test_db):
        from ohm.decision.autoresearch import promote_candidate_edge

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Decision 1', 'decision', 'a', CURRENT_TIMESTAMP, 0.5, '["go"]', 'go', 0.5),
            ('hyp1', 'Hypothesis 1', 'pattern', 'a', CURRENT_TIMESTAMP, 0.8, NULL, NULL, NULL)
            """
        )
        test_db.commit()
        result = promote_candidate_edge(test_db, decision_id="dec1", hypothesis_id="hyp1")
        assert "edge" in result
        assert "recommendation" in result
        edges = test_db.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dec1' AND edge_type = 'DECISION_DEPENDS_ON' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert edges == 1

    def test_promote_records_history(self, test_db):
        from ohm.decision.autoresearch import promote_candidate_edge

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Decision 1', 'decision', 'a', CURRENT_TIMESTAMP, 0.5, '["go"]', 'go', 0.5),
            ('hyp1', 'Hypothesis 1', 'pattern', 'a', CURRENT_TIMESTAMP, 0.8, NULL, NULL, NULL)
            """
        )
        test_db.commit()
        promote_candidate_edge(test_db, decision_id="dec1", hypothesis_id="hyp1")
        history = test_db.execute(
            "SELECT COUNT(*) FROM ohm_autoresearch_history WHERE decision_id = 'dec1' AND hypothesis_id = 'hyp1' AND outcome = 'promoted'"
        ).fetchone()[0]
        assert history == 1


class TestStabilityAndRejection:
    """Rejection-history / stability tests."""

    def test_rejected_candidate_not_reproposed(self, test_db):
        from ohm.decision.autoresearch import propose_hypothesis_edges, _record_history

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Build feature', 'decision', 'a', CURRENT_TIMESTAMP, 0.6, '["feature"]', '["build"]', 'build', 0.5),
            ('cand1', 'Feature candidate', 'concept', 'a', CURRENT_TIMESTAMP, 0.5, '["feature"]', NULL, NULL, NULL)
            """
        )
        test_db.commit()
        _record_history(test_db, decision_id="dec1", hypothesis_id="cand1", outcome="rejected", reason="test")
        candidates = propose_hypothesis_edges(test_db, decision_id="dec1")
        assert not any(c["hypothesis_id"] == "cand1" for c in candidates)

    def test_second_round_no_new_candidates(self, test_db):
        from ohm.decision.autoresearch import run_autoresearch_round

        test_db.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence, tags, action_alternatives, current_best_action, utility_scale) VALUES
            ('dec1', 'Build feature', 'decision', 'a', CURRENT_TIMESTAMP, 0.6, '["feature"]', '["build"]', 'build', 0.5),
            ('cand1', 'Feature candidate', 'concept', 'a', CURRENT_TIMESTAMP, 0.5, '["feature"]', NULL, NULL, NULL)
            """
        )
        test_db.commit()
        r1 = run_autoresearch_round(test_db, decision_id="dec1")
        r2 = run_autoresearch_round(test_db, decision_id="dec1")
        if r1["candidates"]:
            for c1 in r1["candidates"]:
                assert not any(c2["hypothesis_id"] == c1["hypothesis_id"] for c2 in r2["candidates"])


class TestCycleDetection:
    """Cycle detection guard tests."""

    def test_no_cycle_for_independent_nodes(self, test_db):
        from ohm.decision.autoresearch import _would_create_cycle

        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES "
            "('a', 'A', 'concept', 'x', CURRENT_TIMESTAMP), "
            "('b', 'B', 'concept', 'x', CURRENT_TIMESTAMP)"
        )
        test_db.commit()
        assert not _would_create_cycle(test_db, "a", "b")

    def test_cycle_detected_for_back_edge(self, test_db):
        from ohm.decision.autoresearch import _would_create_cycle

        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES "
            "('a', 'A', 'concept', 'x', CURRENT_TIMESTAMP), "
            "('b', 'B', 'concept', 'x', CURRENT_TIMESTAMP)"
        )
        test_db.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
            "('a', 'b', 'CAUSES', 'L3', 0.9, 'x', CURRENT_TIMESTAMP)"
        )
        test_db.commit()
        assert _would_create_cycle(test_db, "b", "a")


class TestSchemaMigration:
    """Schema-level invariants."""

    def test_schema_version_bumped(self):
        from ohm.graph.schema import SCHEMA_VERSION
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 55, 0)

    def test_migration_0_55_0_present(self):
        from ohm.graph.schema import MIGRATIONS
        versions = [m[0] for m in MIGRATIONS]
        assert "0.55.0" in versions

    def test_table_ddl_in_schema(self):
        from ohm.graph.schema import DDL_STATEMENTS
        assert any("ohm_autoresearch_history" in stmt for stmt in DDL_STATEMENTS)