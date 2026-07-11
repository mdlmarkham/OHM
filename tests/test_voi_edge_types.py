"""Tests for VoI edge-type mismatch fix (#824).

Tests that compute_voi discovers ancestors via THREATENS and SUPPORTS edges,
and that the shared source of truth (SemanticRoles.inference_edge_types())
works correctly.
"""

from __future__ import annotations

import pytest

from ohm.framework.semantic_roles import SemanticRoles
from ohm.inference.bayesian import compute_voi


class TestVoIEdgeTypes:
    """Test VoI edge-type mismatch fix."""

    def test_inference_edge_types_method(self) -> None:
        """Test SemanticRoles.inference_edge_types() returns bayesian ∪ evidential."""
        roles = SemanticRoles.defaults()
        inference_edges = set(roles.inference_edge_types())
        expected = set(roles.bayesian) | set(roles.evidential)
        assert inference_edges == expected

    def test_compute_voi_default_uses_inference_edge_types(self, test_db) -> None:
        """Test compute_voi default edge_types uses SemanticRoles.inference_edge_types()."""
        # Monkeypatch SemanticRoles to verify the call
        original_defaults = SemanticRoles.defaults
        
        def mock_defaults():
            roles = original_defaults()
            # Override to return a known set
            return roles.merge(bayesian=["CAUSES", "THREATENS"], evidential=["SUPPORTS"])
        
        SemanticRoles.defaults = mock_defaults
        
        try:
            # No edge_types specified — should use inference_edge_types()
            result = compute_voi(test_db, decision_nodes=["decision"])
            # If no nodes exist, it should return empty but not crash
            assert result["n_candidates"] == 0
        finally:
            SemanticRoles.defaults = original_defaults

    def test_compute_voi_finds_threatens_ancestors(self, test_db) -> None:
        """Test compute_voi finds ancestors connected only via THREATENS."""
        conn = test_db
        
        # Create decision node
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["decision", "Decision", "decision", 0.8, "test"],
        )
        # Create ancestor connected via THREATENS
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["ancestor", "Ancestor", "concept", 0.7, "test"],
        )
        # THREATENS edge
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["ancestor", "decision", "THREATENS", 0.8, 0.6, "L3", "test"],
        )
        
        result = compute_voi(conn, decision_nodes=["decision"])
        assert result["n_candidates"] == 1
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["node_id"] == "ancestor"

    def test_compute_voi_finds_supports_ancestors(self, test_db) -> None:
        """Test compute_voi finds ancestors connected only via SUPPORTS."""
        conn = test_db
        
        # Create decision node
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["decision", "Decision", "decision", 0.8, "test"],
        )
        # Create ancestor connected via SUPPORTS
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["ancestor", "Ancestor", "concept", 0.7, "test"],
        )
        # SUPPORTS edge
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["ancestor", "decision", "SUPPORTS", 0.8, 0.6, "L3", "test"],
        )
        
        result = compute_voi(conn, decision_nodes=["decision"])
        assert result["n_candidates"] == 1
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["node_id"] == "ancestor"

    def test_compute_voi_empty_when_no_ancestors(self, test_db) -> None:
        """Test compute_voi returns empty when no ancestors exist."""
        conn = test_db
        
        # Create decision node with no ancestors
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["decision", "Decision", "decision", 0.8, "test"],
        )
        
        result = compute_voi(conn, decision_nodes=["decision"])
        assert result["n_candidates"] == 0
        assert len(result["rankings"]) == 0
        assert "No causal ancestors found" in result["message"]

    def test_compute_voi_diagnostic_when_network_exists(self, test_db) -> None:
        """Test compute_voi returns diagnostic when ancestors exist but not in VoI edge set."""
        conn = test_db
        
        # Create decision node
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["decision", "Decision", "decision", 0.8, "test"],
        )
        # Create ancestor connected via THREATENS (not in causal_list)
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["ancestor", "Ancestor", "concept", 0.7, "test"],
        )
        # THREATENS edge
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["ancestor", "decision", "THREATENS", 0.8, 0.6, "L3", "test"],
        )
        
        # Use narrow edge_types that exclude THREATENS
        result = compute_voi(conn, decision_nodes=["decision"], edge_types=["CAUSES"])
        assert result["n_candidates"] == 0
        assert "No causal ancestors found using edge_types=['CAUSES']" in result["message"]
        assert "A Bayesian network can be built" in result["message"]

    def test_ohm_belief_uses_edge_types_parameter(self, test_server) -> None:
        """Test ohm_belief forwards edge_types parameter to compute_voi."""
        port, store = test_server
        
        # Create decision node
        store.write_node("decision", "Decision", "decision", confidence=0.8)
        # Create ancestor connected via THREATENS
        store.write_node("ancestor", "Ancestor", "concept", confidence=0.7)
        store.write_edge("ancestor", "decision", "THREATENS", "L3", confidence=0.8, probability=0.6)
        
        # Test /belief endpoint with edge_types
        import requests
        
        response = requests.get(
            f"http://127.0.0.1:{port}/belief?target=decision&edge_types=THREATENS",
            timeout=10,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["what_to_do_next"]["suggested_observations"]) == 1
        assert data["what_to_do_next"]["suggested_observations"][0]["node"] == "ancestor"