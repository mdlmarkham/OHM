"""Tests for source diversity score (OHM-qi6r, ADR-033)."""

from __future__ import annotations

import pytest
from ohm.graph.methods import source_diversity_score
from ohm.framework.sdk import Graph


def _setup_evidence_graph(conn):
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
               source_author, source_institution, data_origin)
           VALUES ('target', 'target claim', 'concept', 'agent1', 'team', 0.9,
                   NULL, NULL, NULL)"""
    )
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
               source_author, source_institution, data_origin)
           VALUES ('src1', 'source 1', 'source', 'agent1', 'team', 0.8,
                   'Smith', 'MIT', 'peer_reviewed')"""
    )
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
               source_author, source_institution, data_origin)
           VALUES ('src2', 'source 2', 'source', 'agent2', 'team', 0.7,
                   'Jones', 'Stanford', 'peer_reviewed')"""
    )
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
               source_author, source_institution, data_origin)
           VALUES ('src3', 'source 3', 'source', 'agent3', 'team', 0.6,
                   'Kim', 'GovLab', 'government')"""
    )
    conn.execute(
        """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
           VALUES ('e1', 'src1', 'target', 'CAUSES', 'L3', 'agent1', 0.8)"""
    )
    conn.execute(
        """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
           VALUES ('e2', 'src2', 'target', 'SUPPORTS', 'L3', 'agent2', 0.7)"""
    )
    conn.execute(
        """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
           VALUES ('e3', 'src3', 'target', 'SUPPORTS', 'L3', 'agent3', 0.6)"""
    )


class TestSourceDiversityScore:
    def test_no_evidence_returns_zero(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'isolated', 'concept', 'test', 'team', 1.0)"""
        )
        result = source_diversity_score(conn, "n1")
        assert result["score"] == 0.0
        assert result["evidence_count"] == 0

    def test_single_source_low_diversity(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
                   source_author, source_institution, data_origin)
               VALUES ('target', 'claim', 'concept', 'a1', 'team', 0.9, 'Smith', 'MIT', 'ugc')"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
                   source_author, source_institution, data_origin)
               VALUES ('src1', 'evidence', 'source', 'a1', 'team', 0.8, 'Smith', 'MIT', 'ugc')"""
        )
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
               VALUES ('e1', 'src1', 'target', 'CAUSES', 'L3', 'a1', 0.8)"""
        )
        result = source_diversity_score(conn, "target")
        assert result["evidence_count"] == 1
        assert result["score"] == 0.0
        assert result["distinct_authors"] == 1

    def test_independent_sources_high_diversity(self, test_db):
        conn = test_db
        _setup_evidence_graph(conn)
        result = source_diversity_score(conn, "target")
        assert result["evidence_count"] == 3
        assert result["score"] > 0.7
        assert result["distinct_authors"] == 3
        assert result["distinct_institutions"] == 3
        assert result["distinct_origins"] >= 2

    def test_homogeneous_ugc_low_diversity(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
                   source_author, source_institution, data_origin)
               VALUES ('target', 'claim', 'concept', 'a1', 'team', 0.9, NULL, NULL, NULL)"""
        )
        for i in range(5):
            conn.execute(
                """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence,
                       source_author, source_institution, data_origin)
                   VALUES (?, ?, 'source', 'a1', 'team', 0.7, 'anon', 'Reddit', 'ugc')""",
                [f"src{i}", f"ugc source {i}"],
            )
            conn.execute(
                """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
                   VALUES (?, ?, 'target', 'SUPPORTS', 'L3', 'a1', 0.7)""",
                [f"e{i}", f"src{i}"],
            )
        result = source_diversity_score(conn, "target")
        assert result["score"] < 0.3
        assert result["distinct_authors"] == 1
        assert result["distinct_origins"] == 1

    def test_fallback_to_created_by(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('target', 'claim', 'concept', 'a1', 'team', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('src1', 'evidence', 'source', 'agent_x', 'team', 0.8)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('src2', 'evidence', 'source', 'agent_y', 'team', 0.7)"""
        )
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
               VALUES ('e1', 'src1', 'target', 'CAUSES', 'L3', 'a1', 0.8)"""
        )
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
               VALUES ('e2', 'src2', 'target', 'SUPPORTS', 'L3', 'a1', 0.7)"""
        )
        result = source_diversity_score(conn, "target")
        assert result["evidence_count"] == 2
        assert result["distinct_authors"] == 2

    def test_expected_keys(self, test_db):
        conn = test_db
        _setup_evidence_graph(conn)
        result = source_diversity_score(conn, "target")
        expected = {"node_id", "score", "evidence_count", "author_diversity", "institution_diversity", "origin_diversity", "distinct_authors", "distinct_institutions", "distinct_origins", "method"}
        assert expected <= set(result.keys())

    def test_weighting_formula(self, test_db):
        conn = test_db
        _setup_evidence_graph(conn)
        result = source_diversity_score(conn, "target")
        expected = 0.4 * result["author_diversity"] + 0.4 * result["institution_diversity"] + 0.2 * result["origin_diversity"]
        assert abs(result["score"] - expected) < 0.001


class TestSDKSourceDiversity:
    def test_sdk_source_diversity(self, test_db):
        conn = test_db
        _setup_evidence_graph(conn)
        with Graph(conn, actor="test") as g:
            result = g.source_diversity("target")
            assert result["evidence_count"] == 3
            assert result["score"] > 0.5
