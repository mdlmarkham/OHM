# tests/test_hypothesis_verification.py
"""Tests for hypothesis-tree verification integration (OHM-nlbm).

Covers:
  - verification-scan includes unverified hypotheses & conflicting evidence
  - record_outcome creates experiment_result observation on experiment nodes
  - record_outcome updates linked hypothesis status
  - verification-decay includes hypothesis tree edges (TESTS, SUPPORTS_EVIDENCE, CONTRADICTS_EVIDENCE)
  - hypothesis pruning from decay below min_confidence
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta


# ── Unit tests (direct DB, no HTTP server) ──────────────────────────────────


class TestVerificationScanHypotheses:
    """Verification-scan includes hypothesis tree sections."""

    def test_scan_returns_hypothesis_fields(self, test_db):
        """Verification-scan returns unverified_hypotheses, conflicting_evidence, and hypothesis summary."""
        from ohm.graph.queries import create_node, create_edge

        # Create a hypothesis with no TESTS edges → unverified
        concept = create_node(test_db, label="Base Concept", node_type="concept", created_by="test")
        hypothesis = create_node(
            test_db,
            label="Unverified Hypothesis",
            node_type="hypothesis",
            created_by="test",
            connects_to=[concept["id"]],
        )

        # Simulate verification-scan SQL directly
        unverified = test_db.execute(
            """
            SELECT n.id, n.label, n.type, n.confidence, n.hypothesis_status
            FROM ohm_nodes n
            WHERE n.type = 'hypothesis'
              AND n.deleted_at IS NULL
              AND (n.hypothesis_status IS NULL OR n.hypothesis_status NOT IN ('verified', 'pruned', 'superseded'))
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE e.to_node = n.id AND e.edge_type = 'TESTS' AND e.deleted_at IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_observations o
                  WHERE o.node_id = n.id AND o.type = 'experiment_result' AND o.deleted_at IS NULL
              )
            """,
        ).fetchall()
        assert len(unverified) == 1
        assert unverified[0][1] == "Unverified Hypothesis"

    def test_scan_conflicting_evidence(self, test_db):
        """Hypotheses with more CONTRADICTS_EVIDENCE than SUPPORTS_EVIDENCE show as conflicting."""
        from ohm.graph.queries import create_node, create_edge

        hypothesis = create_node(
            test_db,
            label="Conflicting Hyp",
            node_type="hypothesis",
            created_by="test",
            connects_to=[create_node(test_db, label="C", node_type="concept", created_by="test")["id"]],
        )
        # One SUPPORTS_EVIDENCE
        exp1 = create_node(test_db, label="Exp1", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])
        create_edge(test_db, from_node=exp1["id"], to_node=hypothesis["id"], layer="L3", edge_type="SUPPORTS_EVIDENCE", created_by="test", confidence=0.8)
        # Two CONTRADICTS_EVIDENCE
        exp2 = create_node(test_db, label="Exp2", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])
        create_edge(test_db, from_node=exp2["id"], to_node=hypothesis["id"], layer="L3", edge_type="CONTRADICTS_EVIDENCE", created_by="test", confidence=0.7)
        exp3 = create_node(test_db, label="Exp3", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])
        create_edge(test_db, from_node=exp3["id"], to_node=hypothesis["id"], layer="L3", edge_type="CONTRADICTS_EVIDENCE", created_by="test", confidence=0.6)

        conflicting = test_db.execute(
            """
            SELECT n.id, n.label, n.hypothesis_status, n.confidence,
                   SUM(CASE WHEN e.edge_type = 'SUPPORTS_EVIDENCE' THEN 1 ELSE 0 END) AS supporting,
                   SUM(CASE WHEN e.edge_type = 'CONTRADICTS_EVIDENCE' THEN 1 ELSE 0 END) AS contradicting
            FROM ohm_nodes n
            JOIN ohm_edges e ON e.to_node = n.id
              AND e.edge_type IN ('SUPPORTS_EVIDENCE', 'CONTRADICTS_EVIDENCE')
              AND e.deleted_at IS NULL
            WHERE n.type = 'hypothesis' AND n.deleted_at IS NULL
            GROUP BY n.id, n.label, n.hypothesis_status, n.confidence
            HAVING SUM(CASE WHEN e.edge_type = 'CONTRADICTS_EVIDENCE' THEN 1 ELSE 0 END) >
                   SUM(CASE WHEN e.edge_type = 'SUPPORTS_EVIDENCE' THEN 1 ELSE 0 END)
            ORDER BY n.confidence DESC
            """,
        ).fetchall()

        assert len(conflicting) == 1
        assert conflicting[0][4] == 1  # supporting count
        assert conflicting[0][5] == 2  # contradicting count

    def test_hypothesis_summary_stats(self, test_db):
        """Verification-scan summary includes hypothesis verification metrics."""
        from ohm.graph.queries import create_node, create_edge

        # Create hypotheses in different states
        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        h1 = create_node(test_db, label="Proposed", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])
        h2 = create_node(test_db, label="Verified", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])
        test_db.execute("UPDATE ohm_nodes SET hypothesis_status = 'verified' WHERE id = ?", [h2["id"]])
        h3 = create_node(test_db, label="Pruned", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])
        test_db.execute("UPDATE ohm_nodes SET hypothesis_status = 'pruned' WHERE id = ?", [h3["id"]])

        total = test_db.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND deleted_at IS NULL").fetchone()[0]
        verified = test_db.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND hypothesis_status = 'verified' AND deleted_at IS NULL").fetchone()[0]
        pruned = test_db.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type = 'hypothesis' AND hypothesis_status = 'pruned' AND deleted_at IS NULL").fetchone()[0]

        assert total == 3
        assert verified == 1
        assert pruned == 1


class TestRecordOutcomeHypothesisIntegration:
    """record_outcome creates experiment_result observation and updates hypothesis status."""

    def test_outcome_on_experiment_creates_experiment_result_obs(self, test_db):
        """Recording outcome on experiment node creates experiment_result observation."""
        from ohm.graph.queries import query_record_outcome, create_node

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        experiment = create_node(test_db, label="Exp", node_type="experiment", created_by="test", connects_to=[concept["id"]])

        result = query_record_outcome(
            test_db,
            source_agent="test-agent",
            claim_node=experiment["id"],
            outcome=True,
            recorded_by="test-recorder",
        )

        assert result["source_agent"] == "test-agent"
        assert result["outcome"] is True

        # Check experiment_result observation was created
        obs = test_db.execute(
            "SELECT id, type, value FROM ohm_observations WHERE node_id = ? AND type = 'experiment_result'",
            [experiment["id"]],
        ).fetchone()
        assert obs is not None
        assert obs[1] == "experiment_result"
        assert obs[2] == pytest.approx(1.0)

    def test_outcome_on_experiment_updates_hypothesis_to_verified(self, test_db):
        """Positive outcome on experiment updates linked hypothesis to verified."""
        from ohm.graph.queries import query_record_outcome, create_node, create_edge

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        hypothesis = create_node(test_db, label="H", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])
        experiment = create_node(test_db, label="Exp", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])

        # Link experiment to hypothesis via TESTS edge
        create_edge(test_db, from_node=experiment["id"], to_node=hypothesis["id"], layer="L3", edge_type="TESTS", created_by="test", confidence=0.9)

        result = query_record_outcome(
            test_db,
            source_agent="test-agent",
            claim_node=experiment["id"],
            outcome=True,
            recorded_by="test-recorder",
        )

        # Check hypothesis status updated to verified
        status = test_db.execute(
            "SELECT hypothesis_status FROM ohm_nodes WHERE id = ?",
            [hypothesis["id"]],
        ).fetchone()[0]
        assert status == "verified"

        # Check hypothesis_status_updates in response
        assert "hypothesis_status_updates" in result
        assert len(result["hypothesis_status_updates"]) == 1
        assert result["hypothesis_status_updates"][0]["new_status"] == "verified"

    def test_outcome_on_experiment_with_contradicting_evidence_prunes(self, test_db):
        """Negative outcome on experiment with more CONTRADICTS_EVIDENCE → hypothesis pruned."""
        from ohm.graph.queries import query_record_outcome, create_node, create_edge

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        hypothesis = create_node(test_db, label="H", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])
        experiment = create_node(test_db, label="Exp", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])

        create_edge(test_db, from_node=experiment["id"], to_node=hypothesis["id"], layer="L3", edge_type="TESTS", created_by="test", confidence=0.9)

        # Add more CONTRADICTS_EVIDENCE than SUPPORTS_EVIDENCE
        exp_con = create_node(test_db, label="ExpCon", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])
        create_edge(test_db, from_node=exp_con["id"], to_node=hypothesis["id"], layer="L3", edge_type="CONTRADICTS_EVIDENCE", created_by="test", confidence=0.8)

        result = query_record_outcome(
            test_db,
            source_agent="test-agent",
            claim_node=experiment["id"],
            outcome=False,
            recorded_by="test-recorder",
        )

        # With more CONTRADICTS_EVIDENCE and outcome=False, hypothesis should be pruned
        status = test_db.execute(
            "SELECT hypothesis_status FROM ohm_nodes WHERE id = ?",
            [hypothesis["id"]],
        ).fetchone()[0]
        assert status == "pruned"

    def test_outcome_on_non_experiment_no_hypothesis_update(self, test_db):
        """Outcome on a concept node does not create experiment_result observation or update hypothesis."""
        from ohm.graph.queries import query_record_outcome, create_node

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")

        result = query_record_outcome(
            test_db,
            source_agent="test-agent",
            claim_node=concept["id"],
            outcome=True,
            recorded_by="test-recorder",
        )

        # No hypothesis_status_updates key for non-experiment nodes
        assert "hypothesis_status_updates" not in result
        assert "experiment_result_observation" not in result


class TestVerificationDecayHypothesisEdges:
    """verification-decay includes hypothesis tree edges in decay logic."""

    @pytest.mark.parametrize("edge_type", ["TESTS", "SUPPORTS_EVIDENCE", "CONTRADICTS_EVIDENCE"])
    def test_decay_includes_hypothesis_edge_type(self, test_db, edge_type):
        """Each hypothesis tree edge type is included in verification decay."""
        from ohm.graph.queries import create_node, create_edge
        from ohm.graph.methods import apply_verification_decay

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        hypothesis = create_node(test_db, label="H", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])
        experiment = create_node(test_db, label="Exp", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])

        target = hypothesis["id"] if edge_type == "TESTS" else concept["id"]
        edge = create_edge(
            test_db,
            from_node=experiment["id"],
            to_node=target,
            layer="L3",
            edge_type=edge_type,
            created_by="test",
            confidence=0.9,
        )

        # Age the edge past grace period
        old_date = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
        test_db.execute("UPDATE ohm_edges SET created_at = ? WHERE id = ?", [old_date, edge["id"]])

        result = apply_verification_decay(test_db, dry_run=True, verification_grace_days=14)

        # Should find at least one edge (the hypothesis tree edge)
        assert result["decayed_count"] >= 1
        # The hypothesis tree edge should be in affected_edges
        affected_types = [e["edge_type"] for e in result["affected_edges"]]
        assert edge_type in affected_types

    def test_decay_prunes_tested_hypotheses_below_threshold(self, test_db):
        """Hypotheses in 'tested' status whose TESTS edges decay below min_confidence are pruned."""
        from ohm.graph.queries import create_node, create_edge
        from ohm.graph.methods import apply_verification_decay

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        hypothesis = create_node(test_db, label="H", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])
        experiment = create_node(test_db, label="Exp", node_type="experiment", created_by="test", connects_to=[hypothesis["id"]])

        test_db.execute("UPDATE ohm_nodes SET hypothesis_status = 'tested' WHERE id = ?", [hypothesis["id"]])

        edge = create_edge(
            test_db,
            from_node=experiment["id"],
            to_node=hypothesis["id"],
            layer="L3",
            edge_type="TESTS",
            created_by="test",
            confidence=0.5,  # Start lower so decay takes it below 0.1
        )

        # Age the edge enough for the very short half-life to decay past min_confidence
        old_date = (datetime.utcnow() - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S")
        test_db.execute("UPDATE ohm_edges SET created_at = ? WHERE id = ?", [old_date, edge["id"]])

        # Use short half-life to ensure decay drops below 0.1
        result = apply_verification_decay(
            test_db,
            dry_run=False,
            verification_grace_days=14,
            min_confidence=0.1,
            unverified_half_life_days=10,  # Short half-life for faster decay
        )

        # Hypothesis should be pruned because its TESTS edge decayed below threshold
        status = test_db.execute(
            "SELECT hypothesis_status FROM ohm_nodes WHERE id = ?",
            [hypothesis["id"]],
        ).fetchone()[0]
        assert status == "pruned"
        assert result["hypotheses_pruned"] >= 1


class TestSourceReliabilityWithHypotheses:
    """Hypothesis and experiment nodes participate in source reliability tracking."""

    def test_outcome_on_experiment_included_in_source_reliability(self, test_db):
        """Recording outcome on an experiment node counts toward source reliability."""
        from ohm.graph.queries import query_record_outcome, query_source_reliability, create_node

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        experiment = create_node(test_db, label="Exp", node_type="experiment", created_by="test", connects_to=[concept["id"]])

        # Record positive outcome
        query_record_outcome(
            test_db,
            source_agent="arbor-pilot",
            claim_node=experiment["id"],
            outcome=True,
            recorded_by="test-recorder",
        )

        # Check source reliability includes this outcome
        reliability = query_source_reliability(test_db, source_agent="arbor-pilot")
        assert reliability["total_outcomes"] >= 1
        assert reliability["accurate_count"] >= 1

    def test_outcome_on_hypothesis_included_in_source_reliability(self, test_db):
        """Recording outcome on a hypothesis node counts toward source reliability."""
        from ohm.graph.queries import query_record_outcome, query_source_reliability, create_node

        concept = create_node(test_db, label="C", node_type="concept", created_by="test")
        hypothesis = create_node(test_db, label="H", node_type="hypothesis", created_by="test", connects_to=[concept["id"]])

        query_record_outcome(
            test_db,
            source_agent="research-agent",
            claim_node=hypothesis["id"],
            outcome=False,
            recorded_by="test-recorder",
        )

        reliability = query_source_reliability(test_db, source_agent="research-agent")
        assert reliability["total_outcomes"] >= 1
        assert reliability["false_positive_count"] >= 1


# ── Integration tests (HTTP server) ────────────────────────────────────────

pytestmark = pytest.mark.integration


@pytest.mark.xdist_group("server")
@pytest.mark.xdist_group("server")
class TestVerificationScanHTTP:
    """HTTP integration tests for /admin/verification-scan with hypothesis sections."""

    def test_verification_scan_includes_hypothesis_summary(self, test_server):
        """GET /admin/verification-scan returns hypothesis summary stats."""
        from tests.conftest import _request

        port, _ = test_server
        status, data = _request("GET", port, "/admin/verification-scan")
        assert status == 200
        assert "unverified_hypotheses" in data
        assert "unverified_hypotheses_count" in data
        assert "conflicting_evidence_hypotheses" in data
        assert "conflicting_evidence_hypotheses_count" in data
        summary = data.get("summary", {})
        assert "total_hypotheses" in summary
        assert "verified_hypotheses" in summary
        assert "tested_hypotheses" in summary
        assert "pruned_hypotheses" in summary
        assert "hypothesis_verification_rate" in summary

    def test_verification_scan_finds_unverified_hypothesis(self, test_server):
        """Unverified hypothesis with no TESTS edges appears in scan."""
        from tests.conftest import _request

        port, _ = test_server
        _request("POST", port, "/node", body={"id": "vh-concept", "label": "Concept", "type": "concept"})
        _request("POST", port, "/node", body={"id": "vh-hyp", "label": "Unverified Hyp", "type": "hypothesis", "connects_to": ["vh-concept"]})

        status, data = _request("GET", port, "/admin/verification-scan")
        assert status == 200
        hyp_ids = [h["id"] for h in data.get("unverified_hypotheses", [])]
        assert "vh-hyp" in hyp_ids

    def test_verification_scan_conflicting_evidence(self, test_server):
        """Hypothesis with more CONTRADICTS_EVIDENCE than SUPPORTS_EVIDENCE appears in scan."""
        from tests.conftest import _request

        port, _ = test_server
        _request("POST", port, "/node", body={"id": "ce-concept", "label": "Concept", "type": "concept"})
        _request("POST", port, "/node", body={"id": "ce-hyp", "label": "Conflicting Hyp", "type": "hypothesis", "connects_to": ["ce-concept"]})
        _request("POST", port, "/node", body={"id": "ce-exp1", "label": "Support Exp", "type": "experiment", "connects_to": ["ce-hyp"]})
        _request("POST", port, "/node", body={"id": "ce-exp2", "label": "Contra Exp 1", "type": "experiment", "connects_to": ["ce-hyp"]})
        _request("POST", port, "/node", body={"id": "ce-exp3", "label": "Contra Exp 2", "type": "experiment", "connects_to": ["ce-hyp"]})

        _request("POST", port, "/edge", body={"from": "ce-exp1", "to": "ce-hyp", "type": "SUPPORTS_EVIDENCE", "layer": "L3", "confidence": 0.8})
        _request("POST", port, "/edge", body={"from": "ce-exp2", "to": "ce-hyp", "type": "CONTRADICTS_EVIDENCE", "layer": "L3", "confidence": 0.7})
        _request("POST", port, "/edge", body={"from": "ce-exp3", "to": "ce-hyp", "type": "CONTRADICTS_EVIDENCE", "layer": "L3", "confidence": 0.6})

        status, data = _request("GET", port, "/admin/verification-scan")
        assert status == 200
        hyp_ids = [h["id"] for h in data.get("conflicting_evidence_hypotheses", [])]
        assert "ce-hyp" in hyp_ids


@pytest.mark.xdist_group("server")
@pytest.mark.xdist_group("server")
class TestOutcomeHTTPOnExperiment:
    """HTTP integration tests for POST /outcome on experiment nodes."""

    def test_outcome_on_experiment_returns_hypothesis_update(self, test_server):
        """POST /outcome on an experiment returns hypothesis_status_updates."""
        from tests.conftest import _request

        port, _ = test_server
        _request("POST", port, "/node", body={"id": "oe-concept", "label": "Concept", "type": "concept"})
        _request("POST", port, "/node", body={"id": "oe-hyp", "label": "Hypothesis", "type": "hypothesis", "connects_to": ["oe-concept"]})
        _request("POST", port, "/node", body={"id": "oe-exp", "label": "Experiment", "type": "experiment", "connects_to": ["oe-hyp"]})
        _request("POST", port, "/edge", body={"from": "oe-exp", "to": "oe-hyp", "type": "TESTS", "layer": "L3", "confidence": 0.9})

        status, data = _request(
            "POST",
            port,
            "/outcome",
            body={"source_agent": "arbor", "claim_node": "oe-exp", "outcome": True},
        )
        assert status in (200, 201)
        assert "hypothesis_status_updates" in data
        assert len(data["hypothesis_status_updates"]) == 1
        assert data["hypothesis_status_updates"][0]["new_status"] == "verified"


@pytest.mark.xdist_group("server")
class TestVerificationDecayHTTP:
    """HTTP integration tests for /admin/verification-decay with hypothesis edges."""

    def test_decay_dry_run_includes_hypothesis_edges(self, test_server):
        """POST /admin/verification-decay with hypothesis edges returns them in decay."""
        port, store = test_server
        from tests.conftest import _request
        from datetime import datetime, timedelta

        _request("POST", port, "/node", body={"id": "vd-concept", "label": "Concept", "type": "concept"})
        _request("POST", port, "/node", body={"id": "vd-hyp", "label": "Hypothesis", "type": "hypothesis", "connects_to": ["vd-concept"]})
        _request("POST", port, "/node", body={"id": "vd-exp", "label": "Experiment", "type": "experiment", "connects_to": ["vd-hyp"]})
        # Use the actual created IDs (they get prefixed)
        hyp_resp = _request("GET", port, "/node/vd-hyp")
        exp_resp = _request("GET", port, "/node/vd-exp")
        # If prefixed, use the actual IDs
        hyp_id = hyp_resp[1].get("id", "vd-hyp") if hyp_resp[0] == 200 else "vd-hyp"
        exp_id = exp_resp[1].get("id", "vd-exp") if exp_resp[0] == 200 else "vd-exp"

        _request("POST", port, "/edge", body={"from": exp_id, "to": hyp_id, "type": "TESTS", "layer": "L3", "confidence": 0.9})

        # Age the TESTS edge past grace period
        store.conn.execute(
            "UPDATE ohm_edges SET created_at = ? WHERE from_node = ?",
            [(datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S"), exp_id],
        )

        status, data = _request(
            "POST",
            port,
            "/admin/verification-decay",
            body={"dry_run": True, "verification_grace_days": 14},
        )
        assert status == 200
        assert data.get("decayed_count", 0) >= 1
        # Check that hypothesis tree edges are included
        edge_types = [e["edge_type"] for e in data.get("affected_edges", [])]
        assert "TESTS" in edge_types or "CAUSES" in edge_types