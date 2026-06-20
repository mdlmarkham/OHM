"""Tests for emerging concept detection (OHM-tlqz, ADR-034)."""
from __future__ import annotations

import pytest
from ohm.graph.methods import (
    compute_residual_mass,
    compute_emerging_concept_stability,
    detect_unknown_ingredients,
    update_emerging_concept_score,
    promote_emerging_concept,
)
from ohm.graph.queries import update_node_hd_fingerprint
from ohm.framework.sdk import Graph


def _setup_graph_with_fingerprints(conn):
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
           VALUES ('concept1', 'nuclear reactor safety', 'concept', 'test', 'team', 0.9)"""
    )
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
           VALUES ('concept2', 'nuclear reactor design', 'concept', 'test', 'team', 0.8)"""
    )
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
           VALUES ('emerging1', 'quantum biology entanglement', 'concept', 'test', 'team', 0.5)"""
    )
    update_node_hd_fingerprint(conn, "concept1")
    update_node_hd_fingerprint(conn, "concept2")
    update_node_hd_fingerprint(conn, "emerging1")
    for i in range(5):
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
               VALUES (?, 'concept1', 'emerging1', 'SUPPORTS', 'L3', 'test', 0.7)""",
            [f"e{i}"],
        )


class TestComputeResidualMass:
    def test_no_concepts_returns_one(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'test', 'team', 1.0)"""
        )
        result = compute_residual_mass(conn, "n1")
        assert result["residual_mass"] == 1.0
        assert result["concept_count"] == 0

    def test_orthogonal_concept_high_residual(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        result = compute_residual_mass(conn, "emerging1")
        assert result["residual_mass"] > 0.3
        assert result["concept_count"] == 2

    def test_similar_concept_low_residual(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        result = compute_residual_mass(conn, "concept1")
        assert result["residual_mass"] < 0.5

    def test_missing_node_raises(self, test_db):
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            compute_residual_mass(test_db, "nonexistent")


class TestEmergingConceptStability:
    def test_no_evidence_low_stability(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'test', 'team', 1.0)"""
        )
        result = compute_emerging_concept_stability(conn, "n1")
        assert result["stability"] == 0.0
        assert result["total_observations"] == 0

    def test_with_evidence_high_stability(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        result = compute_emerging_concept_stability(conn, "emerging1")
        assert result["evidence_count"] == 5
        assert result["stability"] > 0.0


class TestDetectUnknownIngredients:
    def test_finds_emerging_concepts(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        results = detect_unknown_ingredients(
            conn, residual_mass_threshold=0.0, stability_threshold=0.0, min_observations=1,
        )
        assert len(results) >= 1
        assert all("residual_mass" in r for r in results)
        assert all("stability" in r for r in results)

    def test_threshold_filters(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        results = detect_unknown_ingredients(
            conn, residual_mass_threshold=0.99, stability_threshold=0.99,
        )
        assert len(results) == 0

    def test_min_observations_filters(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'test', 'team', 1.0)"""
        )
        results = detect_unknown_ingredients(conn, min_observations=10)
        assert len(results) == 0

    def test_sorted_by_residual_mass_desc(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        results = detect_unknown_ingredients(
            conn, residual_mass_threshold=0.0, stability_threshold=0.0, min_observations=1,
        )
        masses = [r["residual_mass"] for r in results]
        assert masses == sorted(masses, reverse=True)


class TestUpdateEmergingConceptScore:
    def test_stores_score(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        result = update_emerging_concept_score(conn, "emerging1")
        assert result["stored"] is True
        assert "emerging_concept_score" in result
        assert "residual_mass" in result["emerging_concept_score"]
        assert "stability" in result["emerging_concept_score"]

    def test_status_naming_candidate(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        result = update_emerging_concept_score(conn, "emerging1")
        if result["emerging_concept_score"]["stability"] >= 0.7:
            assert result["emerging_concept_score"]["status"] == "naming_candidate"
        else:
            assert result["emerging_concept_score"]["status"] == "unnamed"


class TestPromoteEmergingConcept:
    def test_promote_stable_concept(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        result = promote_emerging_concept(
            conn, node_id="emerging1", new_label="Quantum Biology", promoted_by="test",
        )
        assert result["status"] == "named"
        assert result["new_label"] == "Quantum Biology"
        row = conn.execute("SELECT label FROM ohm_nodes WHERE id = 'emerging1'").fetchone()
        assert row[0] == "Quantum Biology"

    def test_promote_unstable_raises(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'isolated', 'concept', 'test', 'team', 1.0)"""
        )
        with pytest.raises(ValueError, match="stability"):
            promote_emerging_concept(conn, node_id="n1", new_label="New", promoted_by="test")

    def test_empty_label_raises(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        with pytest.raises(ValueError, match="non-empty"):
            promote_emerging_concept(conn, node_id="emerging1", new_label="", promoted_by="test")


class TestSDKEmergingConcepts:
    def test_sdk_detect_emerging(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        with Graph(conn, actor="test") as g:
            results = g.detect_emerging_concepts(
                residual_mass_threshold=0.0, stability_threshold=0.0, min_observations=1,
            )
            assert len(results) >= 1

    def test_sdk_compute_residual_mass(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        with Graph(conn, actor="test") as g:
            result = g.compute_residual_mass("emerging1")
            assert "residual_mass" in result

    def test_sdk_name_emerging_concept(self, test_db):
        conn = test_db
        _setup_graph_with_fingerprints(conn)
        with Graph(conn, actor="test") as g:
            result = g.name_emerging_concept("emerging1", "Quantum Biology")
            assert result["status"] == "named"
