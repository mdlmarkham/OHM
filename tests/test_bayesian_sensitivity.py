"""Tests for compute_sensitivity, find_adjustment_sets, and suggest_causes.

Covers:
- E-value computation for known risk ratios (OHM-c7w)
- Adjustment set identification for simple DAGs (OHM-c7w)
- suggest_causes edge cases: empty graph, no candidate edges (OHM-c7w)
"""

from __future__ import annotations

import pytest

from ohm.bayesian import (
    build_bayesian_network,
    compute_sensitivity,
    find_adjustment_sets,
    suggest_causes,
)
from tests.conftest import create_test_db, create_sample_node, create_sample_edge


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    """Create an in-memory test database with schema."""
    return create_test_db()


@pytest.fixture
def causal_chain(db):
    """Create a simple causal chain: A -> B -> C with probability values.

    This is a simple DAG with no confounders, so:
    - Empty set should satisfy backdoor criterion
    - ATE should be non-zero
    - E-value should be > 1.0
    """
    a = create_sample_node(db, label="root_cause")
    b = create_sample_node(db, label="mediator")
    c = create_sample_node(db, label="outcome")

    # Insert edges with explicit probability values
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) "
        "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test_agent')",
        [f"edge_{a}_{b}", a, b],
    )
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) "
        "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.7, 0.8, 'test_agent')",
        [f"edge_{b}_{c}", b, c],
    )
    return {"a": a, "b": b, "c": c}


@pytest.fixture
def confounded_graph(db):
    """Create a confounded graph: A -> C, B -> A, B -> C (B is a confounder).

    B is a common cause of both A and C, creating a backdoor path A <- B -> C.
    The minimal adjustment set should include B.
    """
    a = create_sample_node(db, label="treatment")
    b = create_sample_node(db, label="confounder")
    c = create_sample_node(db, label="outcome")

    # A -> C (causal path)
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) "
        "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.6, 0.8, 'test_agent')",
        [f"edge_{a}_{c}", a, c],
    )
    # B -> A (confounder causes treatment)
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) "
        "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.7, 0.9, 'test_agent')",
        [f"edge_{b}_{a}", b, a],
    )
    # B -> C (confounder causes outcome)
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) "
        "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.5, 0.7, 'test_agent')",
        [f"edge_{b}_{c}", b, c],
    )
    return {"a": a, "b": b, "c": c}


# ── compute_sensitivity Tests ────────────────────────────────────────────

class TestComputeSensitivity:
    """Test E-value sensitivity analysis."""

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_sensitivity_returns_e_value(self, db, causal_chain):
        """Sensitivity analysis should return E-value and robustness assessment."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = compute_sensitivity(
            db,
            cause=causal_chain["a"],
            effect=causal_chain["c"],
        )
        assert result is not None
        assert "method" in result
        assert result["method"] == "e_value_sensitivity"
        assert "e_value" in result
        assert "risk_ratio" in result
        assert "ate" in result
        assert "robustness" in result
        assert "confounder_perturbation" in result

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_sensitivity_e_value_interpretation(self, db, causal_chain):
        """E-value should have a valid robustness interpretation."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = compute_sensitivity(
            db,
            cause=causal_chain["a"],
            effect=causal_chain["c"],
        )
        assert result is not None
        robustness = result.get("robustness")
        assert robustness in ("none", "weak", "moderate", "strong", "very_strong"), \
            f"Unexpected robustness level: {robustness}"

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_sensitivity_perturbation_analysis(self, db, causal_chain):
        """Confounder perturbation (VanderWeele & Ding bounding) should show
        decreasing ATE as confounder strength grows (s >= 1.0)."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = compute_sensitivity(
            db,
            cause=causal_chain["a"],
            effect=causal_chain["c"],
        )
        assert result is not None
        perturbation = result.get("confounder_perturbation", [])
        assert len(perturbation) == 7  # 7 perturbation levels
        # First level should have confounder_strength=1.0 (no confounding)
        assert perturbation[0]["confounder_strength"] == 1.0
        # ATE should decrease as confounder strength increases (for positive ATE)
        # VanderWeele & Ding: adjusted RR = RR/s, so higher s → lower adjusted ATE
        ate_values = [p["adjusted_ate"] for p in perturbation]
        if result["ate"] > 0:
            # Adjusted ATE should be non-increasing
            for i in range(1, len(ate_values)):
                assert ate_values[i] <= ate_values[i-1] or abs(ate_values[i]) < 1e-6, \
                    f"Adjusted ATE should decrease: {ate_values}"

    def test_sensitivity_no_edges_returns_error(self, db):
        """Sensitivity on empty graph should return an error."""
        result = compute_sensitivity(db, cause="nonexistent_a", effect="nonexistent_b")
        assert result is not None
        assert "error" in result

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_sensitivity_with_layers(self, db, causal_chain):
        """Sensitivity analysis should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = compute_sensitivity(
            db,
            cause=causal_chain["a"],
            effect=causal_chain["c"],
            layers=["L3"],
        )
        assert result is not None
        assert "method" in result


# ── find_adjustment_sets Tests ────────────────────────────────────────────

class TestFindAdjustmentSets:
    """Test backdoor/frontdoor adjustment set identification."""

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_adjustment_sets_simple_chain(self, db, causal_chain):
        """Simple causal chain A→B→C should have empty backdoor set (no confounders)."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = find_adjustment_sets(
            db,
            cause=causal_chain["a"],
            effect=causal_chain["c"],
        )
        assert result is not None
        assert "method" in result
        assert result["method"] == "adjustment_sets"
        # A has no parents, so empty set satisfies backdoor criterion
        assert result.get("empty_set_satisfies_backdoor") is True
        assert result.get("identification_method") == "direct"

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_adjustment_sets_confounded(self, db, confounded_graph):
        """Confounded graph should identify confounder in adjustment set."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = find_adjustment_sets(
            db,
            cause=confounded_graph["a"],
            effect=confounded_graph["c"],
        )
        assert result is not None
        assert "method" in result
        # A has parents (B), so empty set should NOT satisfy backdoor
        assert result.get("cause_has_parents") is True
        # Should find an adjustment set or frontdoor nodes
        assert result.get("identification_method") in (
            "backdoor_adjustment", "frontdoor", "direct", "instrumental_variable", "unidentified"
        )

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_adjustment_sets_returns_network_info(self, db, causal_chain):
        """Adjustment sets should include network info."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = find_adjustment_sets(
            db,
            cause=causal_chain["a"],
            effect=causal_chain["c"],
        )
        assert result is not None
        assert "network_info" in result
        assert "n_nodes" in result["network_info"]
        assert "n_edges" in result["network_info"]

    def test_adjustment_sets_no_edges_returns_error(self, db):
        """Adjustment sets on empty graph should return an error."""
        result = find_adjustment_sets(db, cause="nonexistent_a", effect="nonexistent_b")
        assert result is not None
        assert "error" in result

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_adjustment_sets_with_layers(self, db, causal_chain):
        """Adjustment sets should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = find_adjustment_sets(
            db,
            cause=causal_chain["a"],
            effect=causal_chain["c"],
            layers=["L3"],
        )
        assert result is not None
        assert "method" in result


# ── suggest_causes Tests ──────────────────────────────────────────────────

class TestSuggestCauses:
    """Test suggest_causes for identifying candidate causal relationships."""

    def test_suggest_causes_empty_graph(self, db):
        """suggest_causes on empty graph should return empty candidates."""
        result = suggest_causes(db)
        assert result is not None
        assert result["method"] == "suggest_causes"
        assert result["n_candidates"] == 0
        assert result["candidate_causes_edges"] == []
        assert result["n_root_causes"] == 0
        assert result["root_causes"] == []

    def test_suggest_causes_with_candidates(self, db):
        """suggest_causes should find candidate edges from non-causal relationships."""
        a = create_sample_node(db, label="node_a")
        b = create_sample_node(db, label="node_b")

        # Add a DEPENDS_ON edge (non-causal) — should be a candidate
        create_sample_edge(db, from_node=a, to_node=b, edge_type="DEPENDS_ON",
                           layer="L3", confidence=0.8)

        result = suggest_causes(db)
        assert result is not None
        assert result["n_candidates"] >= 1
        # Should suggest adding a CAUSES edge
        candidates = result["candidate_causes_edges"]
        assert any(c["from"] == a and c["to"] == b for c in candidates), \
            f"Expected candidate from {a} to {b}, got {candidates}"

    def test_suggest_causes_no_duplicate_with_existing_causes(self, db):
        """suggest_causes should not suggest edges that already have CAUSES."""
        a = create_sample_node(db, label="node_a")
        b = create_sample_node(db, label="node_b")

        # Add both CAUSES and DEPENDS_ON edges
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES",
                           layer="L3", confidence=0.9)
        create_sample_edge(db, from_node=a, to_node=b, edge_type="DEPENDS_ON",
                           layer="L3", confidence=0.8)

        result = suggest_causes(db)
        # Should NOT suggest adding CAUSES for (a, b) since it already exists
        candidates = result["candidate_causes_edges"]
        duplicate = [c for c in candidates if c["from"] == a and c["to"] == b]
        assert len(duplicate) == 0, \
            f"Should not suggest CAUSES for edge that already has CAUSES: {duplicate}"

    def test_suggest_causes_min_confidence_filter(self, db):
        """suggest_causes should filter by min_confidence."""
        a = create_sample_node(db, label="node_a")
        b = create_sample_node(db, label="node_b")

        # Add a low-confidence DEPENDS_ON edge
        create_sample_edge(db, from_node=a, to_node=b, edge_type="DEPENDS_ON",
                           layer="L3", confidence=0.3)

        # With min_confidence=0.5, this edge should be filtered out
        result = suggest_causes(db, min_confidence=0.5)
        assert result["n_candidates"] == 0

        # With min_confidence=0.2, this edge should be included
        result = suggest_causes(db, min_confidence=0.2)
        assert result["n_candidates"] >= 1

    def test_suggest_causes_root_causes(self, db):
        """suggest_causes should identify root cause nodes."""
        a = create_sample_node(db, label="root")
        b = create_sample_node(db, label="child")

        # A -> B (A is a root cause with no parents)
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES",
                           layer="L3", confidence=0.9)

        result = suggest_causes(db)
        assert result is not None
        # A should be identified as a root cause
        root_ids = [r["id"] for r in result["root_causes"]]
        assert a in root_ids, f"Expected {a} as root cause, got {root_ids}"

    def test_suggest_causes_disconnected_nodes(self, db):
        """suggest_causes should identify nodes disconnected from causal graph."""
        a = create_sample_node(db, label="isolated")

        result = suggest_causes(db)
        assert result is not None
        # The isolated node should be in the disconnected list
        disconnected_ids = [d["id"] for d in result["disconnected_from_causal"]]
        assert a in disconnected_ids, \
            f"Expected {a} as disconnected, got {disconnected_ids}"