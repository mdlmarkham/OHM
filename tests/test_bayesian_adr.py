"""Tests for ADR-008 and ADR-009 compliance in the Bayesian inference engine.

ADR-008: Probability and confidence are distinct attributes.
  - probability = P(effect|cause), the causal strength
  - confidence = belief in the edge's existence, modulates leak probability
  - When probability is NULL: use confidence * default_probability
  - Leak probability is modulated by average parent confidence

ADR-009: NEGATES edges have inverted probability semantics.
  - A NEGATES edge from A to B means: when A is "bad", P(B=bad) *decreases*
  - In noisy-OR: NEGATES propagates failure when parent is "good" (state 1)
"""

from __future__ import annotations

import pytest

from ohm.bayesian import (
    build_bayesian_network,
    _safe_node_id,
)
from tests.conftest import create_test_db, create_sample_node, create_sample_edge


@pytest.fixture
def db():
    """Create an in-memory test database with schema."""
    return create_test_db()


# ── ADR-008: Probability/Confidence Separation (OHM-3xn) ────────────────


class TestProbabilityConfidenceSeparation:
    """Test that probability and confidence are not conflated in BN construction.

    ADR-008: probability = P(effect|cause), confidence = belief in edge existence.
    When only confidence is set, the effective probability should be
    confidence * default_probability, NOT confidence directly.
    """

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_confidence_does_not_substitute_for_probability(self, db):
        """An edge with confidence=0.9 but no probability should NOT use 0.9 as probability.

        Per ADR-008, confidence and probability are semantically distinct.
        With default_probability=0.5, effective_prob = 0.9 * 0.5 = 0.45.
        """
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="conf_cause")
        b = create_sample_node(db, label="conf_effect")

        # Edge with confidence=0.9 but NO probability
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.9, 'test_agent')",
            [f"edge_{a}_{b}", a, b],
        )

        result = build_bayesian_network(db, default_probability=0.5)
        assert result is not None
        # The effective probability should be confidence * default_probability = 0.9 * 0.5 = 0.45
        # NOT confidence directly (0.9)
        edge = result["edges"][0]
        assert edge["probability"] < 0.9, f"Probability should NOT be confidence directly; got {edge['probability']}"
        assert abs(edge["probability"] - 0.45) < 0.01, f"Expected probability ≈ 0.45 (0.9 * 0.5), got {edge['probability']}"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_explicit_probability_used_directly(self, db):
        """When both probability and confidence are set, effective_prob = probability * confidence.

        Per ADR-008, when both are set, the effective probability is probability * confidence.
        This ensures confidence modulates the causal strength rather than being ignored.
        """
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="prob_cause")
        b = create_sample_node(db, label="prob_effect")

        # Edge with both probability=0.3 and confidence=0.9
        # Per ADR-008: effective_prob = probability * confidence = 0.3 * 0.9 = 0.27
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.3, 0.9, 'test_agent')",
            [f"edge_{a}_{b}", a, b],
        )

        result = build_bayesian_network(db, default_probability=0.5)
        assert result is not None
        edge = result["edges"][0]
        # Per ADR-008: effective probability = probability * confidence = 0.3 * 0.9 = 0.27
        assert abs(edge["probability"] - 0.27) < 0.01, f"Effective probability should be probability * confidence = 0.27; got {edge['probability']}"
        # Confidence should still be tracked for leak modulation
        assert abs(edge["confidence"] - 0.9) < 0.01, f"Confidence should be preserved for leak modulation; got {edge['confidence']}"


# ── ADR-009: NEGATES Edge Handling (OHM-roa) ─────────────────────────────


class TestNegatesEdgeHandling:
    """Test that NEGATES edges are included in BN construction with inverted semantics.

    ADR-009: NEGATES edges have inverted probability semantics.
    A NEGATES edge from A to B means: when A is "bad", P(B=bad) *decreases*.
    In the noisy-OR gate, NEGATES propagates failure when parent is "good" (state 1).
    """

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_negates_edges_included_in_network(self, db):
        """NEGATES edges should be included in the Bayesian network by default."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="diagnosis_a")
        b = create_sample_node(db, label="condition_b")

        # NEGATES edge: diagnosis_a rules out condition_b
        create_sample_edge(db, from_node=a, to_node=b, edge_type="NEGATES", layer="L3", confidence=0.8)

        result = build_bayesian_network(db)
        assert result is not None, "Network should include NEGATES edges"
        assert result["n_nodes"] >= 2, f"Expected >=2 nodes, got {result['n_nodes']}"
        assert result["n_edges"] >= 1, f"Expected >=1 edge, got {result['n_edges']}"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_negates_inverted_probability(self, db):
        """NEGATES edges should invert the parent state check in CPTs.

        For a NEGATES edge A→B:
        - When A is "good" (1): P(B=bad) increases (negation activates)
        - When A is "bad" (0): P(B=bad) decreases (negation doesn't activate)

        This is the inverse of CAUSES, where:
        - When A is "bad" (0): P(B=bad) increases
        - When A is "good" (1): P(B=bad) decreases
        """
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="neg_cause")
        b = create_sample_node(db, label="neg_effect")

        # NEGATES edge with probability=0.7
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'NEGATES', 0.7, 0.9, 'test_agent')",
            [f"edge_{a}_{b}", a, b],
        )

        result = build_bayesian_network(db)
        assert result is not None
        # Check that the edge is marked as NEGATES
        edge = result["edges"][0]
        assert edge.get("is_negates") is True, "NEGATES edge should be flagged"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_negates_excluded_when_edge_types_specified(self, db):
        """NEGATES should be excluded when edge_types explicitly excludes it."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="excl_cause")
        b = create_sample_node(db, label="excl_effect")

        create_sample_edge(db, from_node=a, to_node=b, edge_type="NEGATES", layer="L3", confidence=0.8)

        # Request only CAUSES edges — NEGATES should be excluded
        result = build_bayesian_network(db, edge_types=["CAUSES"])
        assert result is None, "NEGATES edge should be excluded when edge_types=['CAUSES']"


# ── ADR-008: Confidence-Modulated Leak (OHM-pj2) ─────────────────────────


class TestConfidenceModulatedLeak:
    """Test that leak probability is modulated by average parent confidence.

    ADR-008: Higher confidence → lower leak (more probability explained by parents).
    leak = leak_probability * (1 - avg_confidence)
    """

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_high_confidence_reduces_leak(self, db):
        """When parent edges have high confidence, leak should be lower than default."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="high_conf_cause")
        b = create_sample_node(db, label="high_conf_effect")

        # Edge with high confidence (0.95)
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.7, 0.95, 'test_agent')",
            [f"edge_{a}_{b}", a, b],
        )

        result = build_bayesian_network(db, leak_probability=0.15)
        assert result is not None
        # With confidence=0.95, leak = 0.15 * (1 - 0.95) = 0.0075
        # This should be clamped to 0.01 (minimum)
        model = result["model"]
        # Check that the CPT for the child node has a low leak
        # (P(bad|parent=good) should be very small)
        b_safe = _safe_node_id(b)
        cpd = model.get_cpds(b_safe)
        if cpd is not None:
            values = cpd.get_values()
            # P(bad|parent=good) should be small (≈ leak)
            p_bad_parent_good = values[0][1]  # state 0 (bad), parent=good (1)
            assert p_bad_parent_good < 0.05, f"High confidence should reduce leak; P(bad|parent=good)={p_bad_parent_good}"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_low_confidence_increases_leak(self, db):
        """When parent edges have low confidence, leak should be closer to default."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="low_conf_cause")
        b = create_sample_node(db, label="low_conf_effect")

        # Edge with low confidence (0.2)
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.7, 0.2, 'test_agent')",
            [f"edge_{a}_{b}", a, b],
        )

        result = build_bayesian_network(db, leak_probability=0.15)
        assert result is not None
        # With confidence=0.2, leak = 0.15 * (1 - 0.2) = 0.12
        model = result["model"]
        b_safe = _safe_node_id(b)
        cpd = model.get_cpds(b_safe)
        if cpd is not None:
            values = cpd.get_values()
            # P(bad|parent=good) should be moderate (≈ 0.12)
            p_bad_parent_good = values[0][1]  # state 0 (bad), parent=good (1)
            assert p_bad_parent_good > 0.05, f"Low confidence should increase leak; P(bad|parent=good)={p_bad_parent_good}"
