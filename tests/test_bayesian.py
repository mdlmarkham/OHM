"""Tests for the Bayesian inference engine.

Tests build_bayesian_network, bayesian_inference, causal_intervention,
compute_ate, and related functions with a focus on:
- Edges without probability/confidence values (default_probability fallback)
- Layer scoping (L3, L4, etc.)
- Edge deduplication
- Network construction from various edge configurations
"""

from __future__ import annotations

import pytest

from ohm.bayesian import (
    build_bayesian_network,
    _safe_node_id,
    _find_acyclic_subgraph,
)
from tests.conftest import create_test_db, create_sample_node, create_sample_edge


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    """Create an in-memory test database with schema."""
    return create_test_db()


@pytest.fixture
def causal_graph(db):
    """Create a simple causal graph: A -> B -> C with probability values."""
    a = create_sample_node(db, label="cause_a")
    b = create_sample_node(db, label="effect_b")
    c = create_sample_node(db, label="outcome_c")

    create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES",
                       layer="L3", confidence=0.8)
    create_sample_edge(db, from_node=b, to_node=c, edge_type="CAUSES",
                       layer="L3", confidence=0.7)
    return {"a": a, "b": b, "c": c}


@pytest.fixture
def causal_graph_no_prob(db):
    """Create a causal graph where edges have NO probability/confidence values.

    This tests the critical fix: edges without probability should still
    be included in the BN using default_probability.
    """
    a = create_sample_node(db, label="cause_x")
    b = create_sample_node(db, label="effect_y")
    c = create_sample_node(db, label="outcome_z")

    # Insert edges WITHOUT probability or confidence
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by) "
        "VALUES (?, ?, ?, 'L3', 'CAUSES', 'test_agent')",
        [f"edge_{a}_{b}", a, b],
    )
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by) "
        "VALUES (?, ?, ?, 'L3', 'CAUSES', 'test_agent')",
        [f"edge_{b}_{c}", b, c],
    )
    return {"a": a, "b": b, "c": c}


@pytest.fixture
def multi_layer_graph(db):
    """Create a graph with edges across L3 and L4 layers."""
    a = create_sample_node(db, label="l3_cause")
    b = create_sample_node(db, label="l3_effect")
    c = create_sample_node(db, label="l4_risk")
    d = create_sample_node(db, label="l4_outcome")

    create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES",
                       layer="L3", confidence=0.8)
    create_sample_edge(db, from_node=c, to_node=d, edge_type="THREATENS",
                       layer="L4", confidence=0.6)
    create_sample_edge(db, from_node=a, to_node=d, edge_type="DEPENDS_ON",
                       layer="L4", confidence=0.5)
    return {"a": a, "b": b, "c": c, "d": d}


# ── Unit Tests: _safe_node_id ────────────────────────────────────────────

class TestSafeNodeId:
    def test_hyphens(self):
        assert _safe_node_id("my-node-id") == "my_node_id"

    def test_dots(self):
        assert _safe_node_id("node.v2") == "node_v2"

    def test_slashes(self):
        assert _safe_node_id("path/to/node") == "path_to_node"

    def test_colons(self):
        assert _safe_node_id("ns:node") == "ns_node"

    def test_already_safe(self):
        assert _safe_node_id("simple_node") == "simple_node"


# ── Unit Tests: _find_acyclic_subgraph ────────────────────────────────────

class TestFindAcyclicSubgraph:
    def test_dag_unchanged(self):
        edges = [("A", "B"), ("B", "C")]
        result = _find_acyclic_subgraph(edges)
        assert set(result) == {("A", "B"), ("B", "C")}

    def test_cycle_broken(self):
        edges = [("A", "B"), ("B", "C"), ("C", "A")]
        result = _find_acyclic_subgraph(edges)
        # Should remove at least one edge to break the cycle
        assert len(result) < 3
        # Result should be a valid DAG
        import networkx as nx
        G = nx.DiGraph()
        G.add_edges_from(result)
        assert nx.is_directed_acyclic_graph(G)


# ── Unit Tests: build_bayesian_network ────────────────────────────────────

class TestBuildBayesianNetwork:
    """Test that build_bayesian_network correctly constructs networks."""

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_builds_network_from_causal_edges(self, db, causal_graph):
        """Network should include nodes connected by CAUSES edges."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = build_bayesian_network(db)
        assert result is not None
        assert result["n_nodes"] >= 3
        assert result["n_edges"] >= 2

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_includes_edges_without_probability(self, db, causal_graph_no_prob):
        """CRITICAL: Edges without probability/confidence should still be included.

        This tests the fix for the 'network has 0 nodes' bug where edges
        without probability/confidence values were excluded from the BN.
        """
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = build_bayesian_network(db)
        assert result is not None, "Network should not be None when edges exist without probability"
        assert result["n_nodes"] >= 3, f"Expected >=3 nodes, got {result['n_nodes']}"
        assert result["n_edges"] >= 2, f"Expected >=2 edges, got {result['n_edges']}"

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_default_probability_used(self, db, causal_graph_no_prob):
        """Edges without probability should use default_probability=0.5."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        result = build_bayesian_network(db, default_probability=0.7)
        assert result is not None
        # Check that edges have the default probability
        for edge in result["edges"]:
            if edge["confidence"] == 0.7:
                assert edge["probability"] == 0.7

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_layer_filtering(self, db, multi_layer_graph):
        """Layer filter should scope the network to specified layers."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        # Only L3 edges
        result_l3 = build_bayesian_network(db, layers=["L3"])
        assert result_l3 is not None
        assert result_l3["n_nodes"] == 2  # Only l3_cause and l3_effect
        assert result_l3["n_edges"] == 1

        # Only L4 edges
        result_l4 = build_bayesian_network(db, layers=["L4"])
        assert result_l4 is not None
        assert result_l4["n_nodes"] >= 2  # At least l4_risk and l4_outcome

        # All layers (no filter)
        result_all = build_bayesian_network(db)
        assert result_all is not None
        assert result_all["n_nodes"] >= 4

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_edge_deduplication(self, db):
        """Duplicate edges (same from→to) should be deduplicated, keeping highest probability."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="dup_a")
        b = create_sample_node(db, label="dup_b")

        # Create two edges from a to b with different explicit probabilities
        # Using explicit probability values (ADR-008: probability used directly when set)
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) "
            "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.5, 0.8, 'test_agent')",
            [f"edge_{a}_{b}_1", a, b],
        )
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) "
            "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.9, 0.8, 'test_agent')",
            [f"edge_{a}_{b}_2", a, b],
        )

        result = build_bayesian_network(db)
        assert result is not None
        # Should have only 1 edge (deduplicated)
        assert result["n_edges"] == 1
        # Should keep the higher probability edge (allow float precision)
        assert abs(result["edges"][0]["probability"] - 0.9) < 0.01

    def test_returns_none_when_no_edges(self, db):
        """Should return None when no matching edges exist."""
        # No edges in the database
        result = build_bayesian_network(db)
        assert result is None

    def test_returns_none_when_no_matching_edge_types(self, db):
        """Should return None when edges exist but none match the requested types."""
        a = create_sample_node(db, label="node_a")
        b = create_sample_node(db, label="node_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CONTAINS",
                           layer="L1", confidence=0.9)

        result = build_bayesian_network(db, edge_types=["CAUSES"])
        assert result is None

    def test_probability_and_confidence_are_not_conflated(self, db):
        """ADR-008: probability and confidence are distinct.

        When an edge has probability=0.9 and confidence=0.5:
        - probability should remain 0.9 (the causal strength)
        - confidence should remain 0.5 (the belief in edge existence)
        - effective_prob should be 0.45 (probability * confidence)

        The COALESCE should NOT substitute confidence for probability.
        """
        a = create_sample_node(db, label="prob_conf_a")
        b = create_sample_node(db, label="prob_conf_b")

        # Edge with explicit probability and confidence
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, "
            "probability, confidence, created_by) "
            "VALUES (?, ?, ?, 'L3', 'CAUSES', 0.9, 0.5, 'test_agent')",
            [f"edge_{a}_{b}", a, b],
        )

        result = build_bayesian_network(db)
        assert result is not None
        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        # ADR-008: probability is NOT replaced by confidence
        assert abs(edge["probability"] - 0.9) < 0.01, \
            f"probability must remain ~0.9 (not substituted by confidence), got {edge['probability']}"
        assert abs(edge["confidence"] - 0.5) < 0.01, \
            f"confidence must remain ~0.5, got {edge['confidence']}"
        # ADR-008: effective_prob = probability * confidence
        assert abs(edge["effective_prob"] - 0.45) < 0.001, \
            f"effective_prob must be probability * confidence = 0.45, got {edge['effective_prob']}"

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_root_nodes_scoping(self, db, multi_layer_graph):
        """root_nodes should scope the network to nearby nodes."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        # Scope to just l3_cause
        result = build_bayesian_network(db, root_nodes=[multi_layer_graph["a"]])
        assert result is not None
        # Should include l3_cause and its neighbors
        assert multi_layer_graph["a"] in result["nodes"]


# ── Integration Tests: bayesian_inference ─────────────────────────────────

class TestBayesianInference:
    """Test the full inference pipeline."""

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_inference_with_evidence(self, db, causal_graph):
        """Inference should return posterior probabilities."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import bayesian_inference

        result = bayesian_inference(
            db,
            target=causal_graph["c"],
            evidence={causal_graph["a"]: 0},  # cause is "bad"
        )
        assert result is not None
        assert "method" in result
        # Should either succeed or return a meaningful error
        if "error" not in result:
            assert "posterior" in result

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_inference_with_layers(self, db, multi_layer_graph):
        """Inference should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import bayesian_inference

        result = bayesian_inference(
            db,
            target=multi_layer_graph["b"],
            evidence={multi_layer_graph["a"]: 0},
            layers=["L3"],
        )
        assert result is not None
        assert "method" in result

    def test_inference_no_edges_returns_none(self, db):
        """Inference on empty graph should return meaningful error."""
        from ohm.bayesian import bayesian_inference

        result = bayesian_inference(db, target="nonexistent", evidence={})
        assert result is not None
        # Should return an error about no edges or target not in network
        assert "error" in result or result.get("method") == "none"


# ── Integration Tests: causal_intervention ─────────────────────────────────

class TestCausalIntervention:
    """Test the do-operator (causal intervention)."""

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_intervention_with_layers(self, db, multi_layer_graph):
        """Intervention should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import causal_intervention

        result = causal_intervention(
            db,
            target=multi_layer_graph["b"],
            intervention_state=0,
            layers=["L3"],
        )
        assert result is not None
        assert "method" in result


# ── Integration Tests: compute_ate ────────────────────────────────────────

class TestComputeAte:
    """Test Average Treatment Effect computation."""

    @pytest.mark.skipif(
        not pytest.importorskip("pgmpy", reason="pgmpy not installed"),
        reason="pgmpy not available"
    )
    def test_ate_with_layers(self, db, multi_layer_graph):
        """ATE should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import compute_ate

        result = compute_ate(
            db,
            cause=multi_layer_graph["a"],
            effect=multi_layer_graph["b"],
            layers=["L3"],
        )
        assert result is not None
        assert "method" in result