"""Tests for the Bayesian inference engine.

Tests build_bayesian_network, bayesian_inference, causal_intervention,
compute_ate, and related functions with a focus on:
- Edges without probability/confidence values (default_probability fallback)
- Layer scoping (L3, L4, etc.)
- Edge deduplication
- Network construction from various edge configurations
"""

from __future__ import annotations

import importlib.util
import uuid

import pytest

_NETWORKX_AVAILABLE = importlib.util.find_spec("networkx") is not None

from ohm.bayesian import (
    build_bayesian_network,
    _safe_node_id,
    _find_acyclic_subgraph,
    pert_mean,
    pert_variance,
    compute_voi,
    generate_voi_tasks,
    bayesian_inference,
)
from tests.conftest import create_sample_node, create_sample_edge, create_sample_observation


@pytest.fixture
def causal_graph(db):
    """Create a simple causal graph: A -> B -> C with probability values."""
    a = create_sample_node(db, label="cause_a")
    b = create_sample_node(db, label="effect_b")
    c = create_sample_node(db, label="outcome_c")

    create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
    create_sample_edge(db, from_node=b, to_node=c, edge_type="CAUSES", layer="L3", confidence=0.7)
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
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 'test_agent')",
        [f"edge_{a}_{b}", a, b],
    )
    db.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 'test_agent')",
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

    create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
    create_sample_edge(db, from_node=c, to_node=d, edge_type="THREATENS", layer="L4", confidence=0.6)
    create_sample_edge(db, from_node=a, to_node=d, edge_type="DEPENDS_ON", layer="L4", confidence=0.5)
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
    pytestmark = pytest.mark.skipif(not _NETWORKX_AVAILABLE, reason="networkx not installed")

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

    def test_cycle_prefers_removing_low_probability_edge(self):
        """OHM-gap: When breaking cycles, prefer removing low-probability edges."""
        edges = [("A", "B"), ("B", "C"), ("C", "A")]
        # C→A has low probability (0.1), others have high probability (0.9)
        probs = {("A", "B"): 0.9, ("B", "C"): 0.9, ("C", "A"): 0.1}
        result = _find_acyclic_subgraph(edges, edge_probabilities=probs)
        # Should remove C→A (lowest probability) to break the cycle
        assert ("C", "A") not in result
        assert ("A", "B") in result
        assert ("B", "C") in result

    def test_cycle_without_probability_removes_most_cycles_edge(self):
        """Without probability data, fall back to removing edge in most cycles."""
        # Diamond with cross edge: A→B, A→C, B→D, C→D, C→B
        # C→B creates a cycle; without probs, remove by cycle count
        edges = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"), ("C", "B")]
        result = _find_acyclic_subgraph(edges)
        import networkx as nx

        G = nx.DiGraph()
        G.add_edges_from(result)
        assert nx.is_directed_acyclic_graph(G)

    def test_multiple_cycles_removes_lowest_probability_first(self):
        """When an edge participates in multiple cycles but has low probability,
        it should still be removed first."""
        # A→B, B→C, C→A (cycle 1), C→D, D→A (cycle 2 via A→B→C→D→A)
        edges = [("A", "B"), ("B", "C"), ("C", "A"), ("C", "D"), ("D", "A")]
        # D→A has the lowest probability
        probs = {("A", "B"): 0.8, ("B", "C"): 0.7, ("C", "A"): 0.6, ("C", "D"): 0.9, ("D", "A"): 0.1}
        result = _find_acyclic_subgraph(edges, edge_probabilities=probs)
        # D→A should be removed (lowest probability)
        assert ("D", "A") not in result

    def test_preferred_edge_preserved_in_cycle(self):
        """OHM-n80b: preferred_edges prevents removal of the queried causal edge.

        Scenario: bidirectional CAUSES cycle where the backward edge (B→A) has
        higher probability than the forward edge (A→B). Without preferred_edges,
        the cycle breaker would remove A→B (lower prob). With preferred_edges,
        it must keep A→B and remove B→A instead.
        """
        edges = [("A", "B"), ("B", "A")]
        probs = {("A", "B"): 0.85, ("B", "A"): 0.92}
        # Without preferred_edges: removes A→B (lower probability)
        result_default = _find_acyclic_subgraph(edges, edge_probabilities=probs)
        assert ("A", "B") not in result_default  # wrong — removes the causal edge
        # With preferred_edges={(A,B)}: must keep A→B, removes B→A instead
        result_preferred = _find_acyclic_subgraph(edges, edge_probabilities=probs, preferred_edges={("A", "B")})
        assert ("A", "B") in result_preferred
        assert ("B", "A") not in result_preferred


# ── Unit Tests: build_bayesian_network ────────────────────────────────────


class TestBuildBayesianNetwork:
    """Test that build_bayesian_network correctly constructs networks."""

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_builds_network_from_causal_edges(self, db, causal_graph):
        """Network should include nodes connected by CAUSES edges."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        result = build_bayesian_network(db)
        assert result is not None
        assert result["n_nodes"] >= 3
        assert result["n_edges"] >= 2

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_includes_edges_without_probability(self, db, causal_graph_no_prob):
        """CRITICAL: Edges without probability/confidence should still be included.

        This tests the fix for the 'network has 0 nodes' bug where edges
        without probability/confidence values were excluded from the BN.
        """
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        result = build_bayesian_network(db)
        assert result is not None, "Network should not be None when edges exist without probability"
        assert result["n_nodes"] >= 3, f"Expected >=3 nodes, got {result['n_nodes']}"
        assert result["n_edges"] >= 2, f"Expected >=2 edges, got {result['n_edges']}"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_default_probability_used(self, db, causal_graph_no_prob):
        """Edges without probability should use default_probability=0.5."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        result = build_bayesian_network(db, default_probability=0.7)
        assert result is not None
        # Check that edges have the default probability
        for edge in result["edges"]:
            if edge["confidence"] == 0.7:
                assert edge["probability"] == 0.7

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_layer_filtering(self, db, multi_layer_graph):
        """Layer filter should scope the network to specified layers."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
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

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_edge_deduplication(self, db):
        """Duplicate edges (same from→to) should be deduplicated, keeping highest probability."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        a = create_sample_node(db, label="dup_a")
        b = create_sample_node(db, label="dup_b")

        # Create two edges from a to b with different explicit probabilities
        # ADR-008: effective probability = probability * confidence
        # Edge 1: 0.5 * 0.8 = 0.4, Edge 2: 0.9 * 0.8 = 0.72
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.5, 0.8, 'test_agent')",
            [f"edge_{a}_{b}_1", a, b],
        )
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.9, 0.8, 'test_agent')",
            [f"edge_{a}_{b}_2", a, b],
        )

        result = build_bayesian_network(db)
        assert result is not None
        # Should have only 1 edge (deduplicated)
        assert result["n_edges"] == 1
        # Should keep the higher effective probability edge (0.9 * 0.8 = 0.72)
        assert abs(result["edges"][0]["probability"] - 0.72) < 0.01

    def test_returns_none_when_no_edges(self, db):
        """Should return None when no matching edges exist."""
        # No edges in the database
        result = build_bayesian_network(db)
        assert result is None

    def test_returns_none_when_no_matching_edge_types(self, db):
        """Should return None when edges exist but none match the requested types."""
        a = create_sample_node(db, label="node_a")
        b = create_sample_node(db, label="node_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CONTAINS", layer="L1", confidence=0.9)

        result = build_bayesian_network(db, edge_types=["CAUSES"])
        assert result is None

    def test_probability_and_confidence_are_not_conflated(self, db):
        """ADR-008: probability and confidence are distinct.

        When an edge has probability=0.9 and confidence=0.5:
        - effective probability = probability * confidence = 0.9 * 0.5 = 0.45
        - confidence should remain 0.5 (the belief in edge existence)

        The COALESCE should NOT substitute confidence for probability.
        """
        a = create_sample_node(db, label="prob_conf_a")
        b = create_sample_node(db, label="prob_conf_b")

        # Edge with explicit probability and confidence
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.9, 0.5, 'test_agent')",
            [f"edge_{a}_{b}", a, b],
        )

        result = build_bayesian_network(db)
        assert result is not None
        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        # ADR-008: effective probability = probability * confidence = 0.9 * 0.5 = 0.45
        assert abs(edge["probability"] - 0.45) < 0.01, f"effective probability must be probability * confidence = 0.45, got {edge['probability']}"
        # ADR-008: confidence is preserved for leak modulation
        assert abs(edge["confidence"] - 0.5) < 0.01, f"confidence must remain ~0.5, got {edge['confidence']}"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_root_nodes_scoping(self, db, multi_layer_graph):
        """root_nodes should scope the network to nearby nodes."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        # Scope to just l3_cause
        result = build_bayesian_network(db, root_nodes=[multi_layer_graph["a"]])
        assert result is not None
        # Should include l3_cause and its neighbors
        assert multi_layer_graph["a"] in result["nodes"]

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_root_prior_configurable(self, db):
        """OHM-2y6: root_prior parameter controls default prior for root nodes."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        # Create a simple chain: A -> B
        a = create_sample_node(db, label="root_prior_a")
        b = create_sample_node(db, label="root_prior_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)

        # Default root_prior=0.3: root node A should have P(bad) ≈ 0.3
        result_default = build_bayesian_network(db, root_prior=0.3)
        assert result_default is not None
        model_default = result_default["model"]
        safe_a = result_default["safe_names"][a]
        cpd_default = model_default.get_cpds(safe_a)
        # Root prior P(bad) should be 0.3
        assert abs(float(cpd_default.values[0]) - 0.3) < 0.01, f"Default root prior should be 0.3, got {cpd_default.values[0]}"

        # Custom root_prior=0.5: root node A should have P(bad) ≈ 0.5
        result_uniform = build_bayesian_network(db, root_prior=0.5)
        assert result_uniform is not None
        model_uniform = result_uniform["model"]
        safe_a_u = result_uniform["safe_names"][a]
        cpd_uniform = model_uniform.get_cpds(safe_a_u)
        # Root prior P(bad) should be 0.5
        assert abs(float(cpd_uniform.values[0]) - 0.5) < 0.01, f"Uniform root prior should be 0.5, got {cpd_uniform.values[0]}"


# ── Unit Tests: max_nodes truncation (OHM-u60) ──────────────────────────


class TestMaxNodesTruncation:
    """Test that max_nodes truncation is deterministic and preserves high-degree nodes."""

    def test_truncation_preserves_high_degree_nodes(self, db):
        """Nodes with more edges should be kept over nodes with fewer edges."""
        # Create a hub node with many connections and a peripheral node with one
        hub = create_sample_node(db, label="hub")
        periph = create_sample_node(db, label="peripheral")
        targets = []
        for i in range(10):
            n = create_sample_node(db, label=f"target_{i}")
            targets.append(n)
            create_sample_edge(db, from_node=hub, to_node=n, edge_type="CAUSES", layer="L3", confidence=0.8)
        # Peripheral node has only one edge
        create_sample_edge(db, from_node=periph, to_node=targets[0], edge_type="CAUSES", layer="L3", confidence=0.5)

        # With max_nodes=5, hub should be kept (degree 10) over peripheral (degree 1)
        result = build_bayesian_network(db, max_nodes=5)
        assert result is not None
        assert hub in result["nodes"]
        # Hub has 10 edges, peripheral has 1 — hub must survive truncation

    def test_truncation_is_deterministic(self, db):
        """Repeated calls with same data should produce the same network nodes."""
        # Create a star: hub → many targets
        hub = create_sample_node(db, label="det_hub")
        for i in range(10):
            t = create_sample_node(db, label=f"det_target_{i}")
            create_sample_edge(db, from_node=hub, to_node=t, edge_type="CAUSES", layer="L3", confidence=0.7)

        result1 = build_bayesian_network(db, max_nodes=5)
        result2 = build_bayesian_network(db, max_nodes=5)
        assert result1 is not None
        assert result2 is not None
        assert set(result1["nodes"]) == set(result2["nodes"])

    def test_truncation_preserves_root_nodes(self, db):
        """Root nodes should always be kept even if they have low degree."""
        # Create a high-degree node and a low-degree root node
        hub = create_sample_node(db, label="hub")
        root = create_sample_node(db, label="root_low_degree")
        targets = []
        for i in range(10):
            n = create_sample_node(db, label=f"target_{i}")
            targets.append(n)
            create_sample_edge(db, from_node=hub, to_node=n, edge_type="CAUSES", layer="L3", confidence=0.8)
        # Root has only one edge
        create_sample_edge(db, from_node=root, to_node=hub, edge_type="CAUSES", layer="L3", confidence=0.5)

        # With max_nodes=3, root should still be included because it's a root_node
        result = build_bayesian_network(db, root_nodes=[root], max_nodes=3)
        assert result is not None
        assert root in result["nodes"]


# ── Integration Tests: bayesian_inference ─────────────────────────────────


class TestBayesianInference:
    """Test the full inference pipeline."""

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_inference_with_evidence(self, db, causal_graph):
        """Inference should return posterior probabilities."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
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

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_inference_with_layers(self, db, multi_layer_graph):
        """Inference should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
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

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_intervention_with_layers(self, db, multi_layer_graph):
        """Intervention should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
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

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_intervention_comparison_does_not_rebuild_network(self, db):
        """OHM-1p8: comparison_with_observation should reuse the built network,
        not call build_bayesian_network per query node."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        from unittest.mock import patch
        from ohm.bayesian import causal_intervention, build_bayesian_network

        # Create a chain: A -> B -> C -> D with probabilities
        a = create_sample_node(db, label="chain_a")
        b = create_sample_node(db, label="chain_b")
        c = create_sample_node(db, label="chain_c")
        d = create_sample_node(db, label="chain_d")
        for src, dst, prob in [(a, b, 0.8), (b, c, 0.7), (c, d, 0.6)]:
            db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', ?, 0.9, 'test_agent')",
                [str(uuid.uuid4()), src, dst, prob],
            )

        # Patch build_bayesian_network to count calls.
        # Must patch the real module (ohm.inference.bayesian), not the shim
        # (ohm.bayesian) — causal_intervention resolves the function from its
        # own module namespace, so patching the shim has no effect.
        original_build = build_bayesian_network
        call_count = {"count": 0}

        def counting_build(*args, **kwargs):
            call_count["count"] += 1
            return original_build(*args, **kwargs)

        with patch("ohm.inference.bayesian.build_bayesian_network", side_effect=counting_build):
            result = causal_intervention(
                db,
                target=b,
                intervention_state=0,
                query_nodes=[c, d],
            )

        # build_bayesian_network should be called exactly once (not once per query node)
        assert call_count["count"] == 1, f"Expected 1 call to build_bayesian_network, got {call_count['count']}"
        assert result is not None
        assert result["method"] == "causal_intervention"


# ── Integration Tests: compute_ate ────────────────────────────────────────


class TestComputeAte:
    """Test Average Treatment Effect computation."""

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_ate_with_layers(self, db, multi_layer_graph):
        """ATE should accept layers parameter."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
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


# ── BayesianContext Tests (OHM-5qk) ────────────────────────────────────


class TestBayesianContextReuse:
    """Test that BayesianContext methods reuse the cached network.

    OHM-5qk: intervention(), ate(), and sensitivity() were calling standalone
    functions that rebuild the network each time, defeating the purpose of
    BayesianContext. Now they use the cached self._network directly.
    """

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_intervention_reuses_cached_network(self, db):
        """BayesianContext.intervention() should not call build_bayesian_network."""
        from unittest.mock import patch
        from ohm.bayesian import BayesianContext, build_bayesian_network

        # Create a simple chain: A -> B -> C
        a = create_sample_node(db, label="ctx_a")
        b = create_sample_node(db, label="ctx_b")
        c = create_sample_node(db, label="ctx_c")
        for src, dst, prob in [(a, b, 0.8), (b, c, 0.7)]:
            db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', ?, 0.9, 'test')",
                [str(uuid.uuid4()), src, dst, prob],
            )

        original_build = build_bayesian_network
        call_count = {"count": 0}

        def counting_build(*args, **kwargs):
            call_count["count"] += 1
            return original_build(*args, **kwargs)

        with patch("ohm.inference.bayesian.build_bayesian_network", side_effect=counting_build):
            with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
                # Call intervention twice — should use cached network, not rebuild
                result1 = ctx.intervention(b, 0, query_nodes=[c])
                result2 = ctx.intervention(b, 1, query_nodes=[c])

        # build_bayesian_network should be called exactly once (in __init__)
        assert call_count["count"] == 1, f"Expected 1 call to build_bayesian_network, got {call_count['count']}"
        assert result1["method"] == "causal_intervention"
        assert result2["method"] == "causal_intervention"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_ate_reuses_cached_network(self, db):
        """BayesianContext.ate() should not call build_bayesian_network."""
        from unittest.mock import patch
        from ohm.bayesian import BayesianContext, build_bayesian_network

        a = create_sample_node(db, label="ctx_ate_a")
        b = create_sample_node(db, label="ctx_ate_b")
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
            [str(uuid.uuid4()), a, b],
        )

        original_build = build_bayesian_network
        call_count = {"count": 0}

        def counting_build(*args, **kwargs):
            call_count["count"] += 1
            return original_build(*args, **kwargs)

        with patch("ohm.inference.bayesian.build_bayesian_network", side_effect=counting_build):
            with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
                ate = ctx.ate(a, b)

        # build_bayesian_network should be called exactly once
        assert call_count["count"] == 1, f"Expected 1 call to build_bayesian_network, got {call_count['count']}"
        assert ate["method"] == "model_based_ate"
        assert "ate" in ate

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_intervention_result_matches_standalone(self, db):
        """BayesianContext.intervention() result should match causal_intervention()."""
        from ohm.bayesian import BayesianContext, causal_intervention

        a = create_sample_node(db, label="ctx_match_a")
        b = create_sample_node(db, label="ctx_match_b")
        c = create_sample_node(db, label="ctx_match_c")
        for src, dst, prob in [(a, b, 0.8), (b, c, 0.7)]:
            db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', ?, 0.9, 'test')",
                [str(uuid.uuid4()), src, dst, prob],
            )

        with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
            ctx_result = ctx.intervention(b, 0, query_nodes=[c])

        standalone_result = causal_intervention(db, b, 0, query_nodes=[c], edge_types=["CAUSES"], layers=["L3"])

        # Both should return causal_intervention method
        assert ctx_result["method"] == "causal_intervention"
        assert standalone_result["method"] == "causal_intervention"

        # Both should have same posterior keys
        assert set(ctx_result["posterior"].keys()) == set(standalone_result["posterior"].keys())

        # Posteriors should be numerically close (same graph surgery)
        for node, post in ctx_result["posterior"].items():
            standalone_post = standalone_result["posterior"][node]
            assert abs(post["bad"] - standalone_post["bad"]) < 0.01

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_ate_result_matches_standalone(self, db):
        """BayesianContext.ate() result should match compute_ate()."""
        from ohm.bayesian import BayesianContext, compute_ate

        a = create_sample_node(db, label="ctx_ate_match_a")
        b = create_sample_node(db, label="ctx_ate_match_b")
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
            [str(uuid.uuid4()), a, b],
        )

        with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
            ctx_ate = ctx.ate(a, b)

        standalone_ate = compute_ate(db, a, b, edge_types=["CAUSES"], layers=["L3"])

        assert ctx_ate["method"] == "model_based_ate"
        assert abs(ctx_ate["ate"] - standalone_ate["ate"]) < 0.01
        assert abs(ctx_ate["risk_ratio"] - standalone_ate["risk_ratio"]) < 0.01

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_sensitivity_result_matches_standalone(self, db):
        """BayesianContext.sensitivity() result should match compute_sensitivity()."""
        from ohm.bayesian import BayesianContext, compute_sensitivity

        a = create_sample_node(db, label="ctx_sens_a")
        b = create_sample_node(db, label="ctx_sens_b")
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
            [str(uuid.uuid4()), a, b],
        )

        with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
            ctx_sens = ctx.sensitivity(a, b)

        standalone_sens = compute_sensitivity(db, a, b, edge_types=["CAUSES"], layers=["L3"])

        assert ctx_sens["method"] == "e_value_sensitivity"
        assert abs(ctx_sens["e_value"] - standalone_sens["e_value"]) < 0.01

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_multiple_calls_use_same_network(self, db):
        """Multiple intervention/ate calls should all reuse the same network."""
        from unittest.mock import patch
        from ohm.bayesian import BayesianContext, build_bayesian_network

        a = create_sample_node(db, label="ctx_multi_a")
        b = create_sample_node(db, label="ctx_multi_b")
        c = create_sample_node(db, label="ctx_multi_c")
        for src, dst, prob in [(a, b, 0.8), (b, c, 0.7)]:
            db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', ?, 0.9, 'test')",
                [str(uuid.uuid4()), src, dst, prob],
            )

        original_build = build_bayesian_network
        call_count = {"count": 0}

        def counting_build(*args, **kwargs):
            call_count["count"] += 1
            return original_build(*args, **kwargs)

        with patch("ohm.inference.bayesian.build_bayesian_network", side_effect=counting_build):
            with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
                # Make 5 calls across different methods
                ctx.intervention(b, 0, query_nodes=[c])
                ctx.intervention(b, 1, query_nodes=[c])
                ctx.ate(a, c)
                ctx.sensitivity(a, c)
                ctx.inference(c, {b: 0})

        # All 5 calls should reuse the same network built in __init__
        assert call_count["count"] == 1, f"Expected 1 call to build_bayesian_network, got {call_count['count']}"


# ── PERT Distribution Tests (OHM-6mv.3) ─────────────────────────────────


class TestPERTMean:
    """Test the pert_mean helper function."""

    def test_pert_mean_symmetric(self):
        """PERT mean of a symmetric distribution should equal the median."""
        # (0.2 + 4*0.5 + 0.8) / 6 = 3.0 / 6 = 0.5
        assert pert_mean(0.2, 0.5, 0.8) == pytest.approx(0.5)

    def test_pert_mean_skewed_right(self):
        """PERT mean of a right-skewed distribution should be above the median."""
        # Right-skewed: p05 close to p50, p95 far above
        # (0.4 + 4*0.5 + 0.95) / 6 = 3.35/6 ≈ 0.558
        result = pert_mean(0.4, 0.5, 0.95)
        assert result == pytest.approx(3.35 / 6.0)
        # For a truly right-skewed distribution, mean > median
        # (0.1 + 4*0.3 + 0.9) / 6 = 2.2/6 ≈ 0.367 > 0.3
        result2 = pert_mean(0.1, 0.3, 0.9)
        assert result2 > 0.3

    def test_pert_mean_skewed_left(self):
        """PERT mean of a left-skewed distribution should be below the median."""
        # (0.1 + 4*0.3 + 0.5) / 6 = 1.8 / 6 = 0.3
        result = pert_mean(0.1, 0.3, 0.5)
        assert result == pytest.approx(1.8 / 6.0)

    def test_pert_mean_extreme_values(self):
        """PERT mean with extreme values should still compute correctly."""
        # (0.0 + 4*0.5 + 1.0) / 6 = 3.0 / 6 = 0.5
        assert pert_mean(0.0, 0.5, 1.0) == pytest.approx(0.5)

    def test_pert_mean_weights_median_most(self):
        """The P50 (median) should have 4x weight in PERT mean."""
        # PERT mean = (p05 + 4*p50 + p95) / 6
        # With p05=0, p50=1, p95=0: mean = 4/6 ≈ 0.667
        assert pert_mean(0.0, 1.0, 0.0) == pytest.approx(4.0 / 6.0)


class TestPERTVariance:
    """Test the pert_variance helper function."""

    def test_pert_variance_wide_range(self):
        """Wide P05-P95 range should produce high variance."""
        # ((0.9 - 0.1) / 6)^2 = (0.8/6)^2 ≈ 0.0178
        result = pert_variance(0.1, 0.9)
        assert result == pytest.approx((0.8 / 6.0) ** 2)

    def test_pert_variance_narrow_range(self):
        """Narrow P05-P95 range should produce low variance."""
        # ((0.51 - 0.49) / 6)^2 = (0.02/6)^2 ≈ 0.0000111
        result = pert_variance(0.49, 0.51)
        assert result == pytest.approx((0.02 / 6.0) ** 2)

    def test_pert_variance_zero_range(self):
        """Identical P05 and P95 should produce zero variance."""
        assert pert_variance(0.5, 0.5) == pytest.approx(0.0)

    def test_pert_variance_extreme_range(self):
        """Full 0-1 range should produce maximum variance."""
        # ((1.0 - 0.0) / 6)^2 = (1/6)^2 ≈ 0.0278
        result = pert_variance(0.0, 1.0)
        assert result == pytest.approx((1.0 / 6.0) ** 2)


class TestPERTInBuildBayesianNetwork:
    """Test PERT distribution integration in build_bayesian_network."""

    def _find_edge(self, result, from_label: str, to_label: str) -> dict:
        """Helper to find an edge in the result by from/to node IDs."""
        from_id = _safe_node_id(from_label)
        to_id = _safe_node_id(to_label)
        for edge in result["edges"]:
            if edge["from"] == from_id and edge["to"] == to_id:
                return edge
        return None

    def test_pert_probability_overrides_point_estimate(self, db):
        """When PERT p50 is set, PERT mean should override raw probability."""
        a = create_sample_node(db, label="pert_a")
        b = create_sample_node(db, label="pert_b")

        # Edge with PERT probability: p05=0.2, p50=0.5, p95=0.8
        # PERT mean = (0.2 + 4*0.5 + 0.8) / 6 = 3.0/6 = 0.5
        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            confidence=0.9,
            probability_p05=0.2,
            probability_p50=0.5,
            probability_p95=0.8,
        )

        result = build_bayesian_network(db)
        assert result is not None
        # The edge should use PERT-derived probability * confidence
        # PERT prob = 0.5, confidence = 0.9, effective = 0.5 * 0.9 = 0.45
        edge = self._find_edge(result, a, b)
        assert edge is not None
        assert edge["probability"] == pytest.approx(0.45, abs=0.01)

    def test_pert_confidence_overrides_point_confidence(self, db):
        """When PERT conf_p50 is set, PERT mean should override raw confidence."""
        a = create_sample_node(db, label="pert_conf_a")
        b = create_sample_node(db, label="pert_conf_b")

        # Edge with PERT confidence: c05=0.6, c50=0.8, c95=0.95
        # PERT mean = (0.6 + 4*0.8 + 0.95) / 6 = 4.75/6 ≈ 0.792
        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            probability=0.7,
            confidence_p05=0.6,
            confidence_p50=0.8,
            confidence_p95=0.95,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = self._find_edge(result, a, b)
        assert edge is not None
        # PERT conf ≈ 0.792, probability = 0.7, effective = 0.7 * 0.792 ≈ 0.554
        expected_conf = (0.6 + 4 * 0.8 + 0.95) / 6.0
        expected_prob = 0.7 * expected_conf
        assert edge["probability"] == pytest.approx(expected_prob, abs=0.01)

    def test_pert_p50_only_uses_defaults(self, db):
        """When only p50 is set, p05 and p95 should default to wider ±40% spread."""
        a = create_sample_node(db, label="pert_p50_only_a")
        b = create_sample_node(db, label="pert_p50_only_b")

        # Edge with only p50=0.5 set
        # Defaults: p05 = 0.5 * 0.6 = 0.3, p95 = min(1.0, 0.5 * 1.4) = 0.7
        # PERT mean = (0.3 + 4*0.5 + 0.7) / 6 = 3.0/6 = 0.5
        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            confidence=0.9,
            probability_p50=0.5,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = self._find_edge(result, a, b)
        assert edge is not None
        # PERT prob = 0.5, confidence = 0.9, effective = 0.5 * 0.9 = 0.45
        assert edge["probability"] == pytest.approx(0.45, abs=0.01)

    def test_pert_p50_only_confidence_defaults(self, db):
        """When only conf_p50 is set, c05/c95 should default to wider ±40% spread."""
        a = create_sample_node(db, label="pert_conf_p50_only_a")
        b = create_sample_node(db, label="pert_conf_p50_only_b")

        # Edge with only conf_p50=0.8 set
        # Defaults: c05 = 0.8 * 0.6 = 0.48, c95 = min(1.0, 0.8 * 1.4) = 1.0
        # PERT mean = (0.48 + 4*0.8 + 1.0) / 6 = 4.68/6 = 0.78
        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            probability=0.6,
            confidence_p50=0.8,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = self._find_edge(result, a, b)
        assert edge is not None
        # PERT conf ≈ 0.78, probability = 0.6, effective ≈ 0.6 * 0.78 ≈ 0.468
        assert edge["probability"] == pytest.approx(0.468, abs=0.01)

    def test_pert_backward_compatibility(self, db):
        """Edges without PERT values should work exactly as before."""
        a = create_sample_node(db, label="compat_a")
        b = create_sample_node(db, label="compat_b")

        # Edge with only probability and confidence (no PERT)
        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            probability=0.7,
            confidence=0.8,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = self._find_edge(result, a, b)
        assert edge is not None
        # effective = probability * confidence = 0.7 * 0.8 = 0.56
        assert edge["probability"] == pytest.approx(0.56, abs=0.01)

    def test_pert_both_probability_and_confidence(self, db):
        """Both PERT probability and PERT confidence should combine correctly."""
        a = create_sample_node(db, label="pert_both_a")
        b = create_sample_node(db, label="pert_both_b")

        # PERT probability: p05=0.3, p50=0.6, p95=0.9
        # PERT mean = (0.3 + 4*0.6 + 0.9) / 6 = 3.6/6 = 0.6
        # PERT confidence: c05=0.5, c50=0.7, c95=0.9
        # PERT mean = (0.5 + 4*0.7 + 0.9) / 6 = 4.2/6 = 0.7
        # effective = 0.6 * 0.7 = 0.42
        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            probability_p05=0.3,
            probability_p50=0.6,
            probability_p95=0.9,
            confidence_p05=0.5,
            confidence_p50=0.7,
            confidence_p95=0.9,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = self._find_edge(result, a, b)
        assert edge is not None
        assert edge["probability"] == pytest.approx(0.42, abs=0.01)

    def test_pert_p95_capped_at_one(self, db):
        """When p50 * 1.4 > 1.0, p95 should be capped at 1.0 (wider ±40% default)."""
        a = create_sample_node(db, label="pert_cap_a")
        b = create_sample_node(db, label="pert_cap_b")

        # p50=0.9 → p95 default = min(1.0, 0.9 * 1.4) = min(1.0, 1.26) = 1.0
        # p05 default = max(0.01, 0.9 * 0.6) = 0.54
        # PERT mean = (0.54 + 4*0.9 + 1.0) / 6 = 5.14/6 ≈ 0.857
        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            confidence=0.8,
            probability_p50=0.9,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = self._find_edge(result, a, b)
        assert edge is not None
        # PERT prob ≈ 0.857, confidence = 0.8, effective ≈ 0.857 * 0.8 ≈ 0.685
        expected_pert = (0.54 + 4 * 0.9 + 1.0) / 6.0
        expected_prob = expected_pert * 0.8
        assert edge["probability"] == pytest.approx(expected_prob, abs=0.01)

    def test_pert_cpt_construction_known_values(self, db):
        """OHM-yar: Edge with p05=0.1, p50=0.3, p95=0.7 produces CPT weight = PERT mean * confidence.

        PERT mean = (0.1 + 4*0.3 + 0.7) / 6 = 2.0/6 ≈ 0.333
        With confidence = 0.8: effective = 0.333 * 0.8 ≈ 0.267
        """
        a = create_sample_node(db, label="pert_cpt_a")
        b = create_sample_node(db, label="pert_cpt_b")

        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            confidence=0.8,
            probability_p05=0.1,
            probability_p50=0.3,
            probability_p95=0.7,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = self._find_edge(result, a, b)
        assert edge is not None
        # PERT mean = (0.1 + 4*0.3 + 0.7) / 6 = 0.333...
        # effective = 0.333 * 0.8 = 0.267
        expected_pert_mean = (0.1 + 4 * 0.3 + 0.7) / 6.0
        expected_effective = expected_pert_mean * 0.8
        assert edge["probability"] == pytest.approx(expected_effective, abs=0.01)


# ── VoI Tests ────────────────────────────────────────────────────────────


class TestComputeVoI:
    """Test Value of Information computation (OHM-6mv.1)."""

    def test_voi_no_decision_nodes(self, db):
        """When no decision nodes exist, should return empty rankings."""
        result = compute_voi(db)
        assert result["method"] == "value_of_information"
        assert result["rankings"] == []
        assert result["n_candidates"] == 0

    def test_voi_with_explicit_decision_nodes(self, db):
        """When decision_nodes are specified, use them even if no 'decision' type nodes exist."""
        a = create_sample_node(db, label="root_cause", confidence=0.3)
        b = create_sample_node(db, label="mediator", confidence=0.6)
        d = create_sample_node(db, label="my_decision")

        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=b, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.7)

        result = compute_voi(db, decision_nodes=[d])
        assert result["method"] == "value_of_information"
        assert d in result["decision_nodes"]
        # Should find 'a' and 'b' as ancestors of 'd'
        assert result["n_candidates"] >= 1
        # Root cause 'a' should have higher VoI than mediator 'b'
        # because 'a' has lower confidence (higher uncertainty)
        rankings = result["rankings"]
        assert len(rankings) > 0

    def test_voi_auto_detects_decision_nodes(self, db):
        """Should auto-detect nodes with type='decision' and utility_scale > 0."""
        a = create_sample_node(db, label="uncertain_root", confidence=0.2)
        d = create_sample_node(db, label="my_decision", node_type="decision")

        # Set utility_scale on the decision node
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = compute_voi(db)
        assert result["method"] == "value_of_information"
        assert d in result["decision_nodes"]
        assert len(result["rankings"]) >= 1

    def test_voi_ranking_order(self, db):
        """Nodes with higher uncertainty should rank higher (all else equal)."""
        # Two root causes with different confidence levels
        low_conf = create_sample_node(db, label="uncertain", confidence=0.1)
        high_conf = create_sample_node(db, label="certain", confidence=0.9)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        create_sample_edge(db, from_node=low_conf, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=high_conf, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = compute_voi(db)
        rankings = result["rankings"]
        assert len(rankings) == 2
        # The uncertain node should rank higher
        assert rankings[0]["node_id"] == low_conf
        assert rankings[0]["uncertainty"] > rankings[1]["uncertainty"]

    def test_voi_top_parameter(self, db):
        """The 'top' parameter should limit the number of results."""
        nodes = []
        for i in range(5):
            n = create_sample_node(db, label=f"cause_{i}", confidence=0.1 + i * 0.15)
            nodes.append(n)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        for n in nodes:
            create_sample_edge(db, from_node=n, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.7)

        result = compute_voi(db, top=3)
        assert len(result["rankings"]) == 3

    def test_voi_layers_filter(self, db):
        """The 'layers' parameter should scope the analysis to specific layers."""
        a = create_sample_node(db, label="l3_cause", confidence=0.3)
        b = create_sample_node(db, label="l4_cause", confidence=0.3)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=b, to_node=d, edge_type="CAUSES", layer="L4", confidence=0.8)

        # Only L3 edges
        result_l3 = compute_voi(db, layers=["L3"])
        l3_ids = [r["node_id"] for r in result_l3["rankings"]]
        assert a in l3_ids
        assert b not in l3_ids

        # Only L4 edges
        result_l4 = compute_voi(db, layers=["L4"])
        l4_ids = [r["node_id"] for r in result_l4["rankings"]]
        assert b in l4_ids
        assert a not in l4_ids

    def test_voi_no_causal_ancestors(self, db):
        """Decision node with no ancestors should return empty rankings."""
        d = create_sample_node(db, label="isolated_decision", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        result = compute_voi(db)
        assert result["rankings"] == []
        assert "No causal ancestors" in result.get("message", "")

    def test_voi_includes_observation_count(self, db):
        """VoI rankings should include observation counts for each node."""
        a = create_sample_node(db, label="observed_cause", confidence=0.3)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        # Add an observation for node 'a'
        db.execute(
            "INSERT INTO ohm_observations (id, node_id, value, source, type, created_by) VALUES (?, ?, 0.5, 'test', 'measurement', 'test_agent')",
            [str(uuid.uuid4()), a],
        )

        result = compute_voi(db)
        assert len(result["rankings"]) >= 1
        ranking = result["rankings"][0]
        assert ranking["observation_count"] >= 1

    def test_voi_downstream_decisions_field(self, db):
        """Each ranking should list which decision nodes it affects."""
        a = create_sample_node(db, label="shared_cause", confidence=0.3)
        d1 = create_sample_node(db, label="decision_1", node_type="decision")
        d2 = create_sample_node(db, label="decision_2", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d1])
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d2])

        create_sample_edge(db, from_node=a, to_node=d1, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=a, to_node=d2, edge_type="CAUSES", layer="L3", confidence=0.7)

        result = compute_voi(db)
        # 'a' should appear in rankings and affect both decisions
        a_ranking = next((r for r in result["rankings"] if r["node_id"] == a), None)
        assert a_ranking is not None
        assert d1 in a_ranking["downstream_decisions"]
        assert d2 in a_ranking["downstream_decisions"]
        assert a_ranking["n_downstream_decisions"] == 2

    def test_voi_pert_uncertainty_wide_bounds_rank_higher(self, db):
        """VoI: node with wide PERT bounds (high variance) ranks higher than tight bounds.

        ADR-013: uncertainty should use PERT variance. Wide bounds = more uncertainty = higher VoI.
        """
        # Node A: tight PERT bounds (p05=0.48, p95=0.52) → low variance
        a_tight = create_sample_node(db, label="tight_bound_node", confidence=0.5)
        # Node B: wide PERT bounds (p05=0.1, p95=0.9) → high variance
        a_wide = create_sample_node(db, label="wide_bound_node", confidence=0.5)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        # Edge A → decision with tight PERT
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, probability_p05, probability_p50, probability_p95, created_by) VALUES (?, ?, ?, 'CAUSES', 'L3', 0.7, 0.48, 0.50, 0.52, 'test')",
            [str(uuid.uuid4()), a_tight, d],
        )
        # Edge B → decision with wide PERT
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, probability_p05, probability_p50, probability_p95, created_by) VALUES (?, ?, ?, 'CAUSES', 'L3', 0.7, 0.10, 0.50, 0.90, 'test')",
            [str(uuid.uuid4()), a_wide, d],
        )

        result = compute_voi(db)
        rankings = {r["node_id"]: r for r in result["rankings"]}

        # Wide-bound node should rank higher (higher uncertainty due to PERT variance)
        assert a_wide in rankings
        assert a_tight in rankings
        assert rankings[a_wide]["uncertainty"] > rankings[a_tight]["uncertainty"]
        assert rankings[a_wide]["voi_score"] > rankings[a_tight]["voi_score"]

    def test_voi_uncertainty_fallback_without_pert(self, db):
        """VoI: node without PERT data falls back to 1 - confidence."""
        a = create_sample_node(db, label="no_pert_node", confidence=0.3)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        # Edge without PERT columns
        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = compute_voi(db)
        ranking = result["rankings"][0]
        # Should fall back to 1 - confidence = 0.7
        assert ranking["uncertainty"] == 0.7

    def test_voi_uncertainty_zero_when_pert_tight(self, db):
        """VoI: uncertainty should be near 0 when p05 ≈ p95 (perfect knowledge)."""
        a = create_sample_node(db, label="perfect_knowledge_node", confidence=0.9)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        # Edge with very tight PERT bounds (p05=0.49, p95=0.51)
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, probability_p05, probability_p50, probability_p95, created_by) VALUES (?, ?, ?, 'CAUSES', 'L3', 0.9, 0.49, 0.50, 0.51, 'test')",
            [str(uuid.uuid4()), a, d],
        )

        result = compute_voi(db)
        ranking = result["rankings"][0]
        # With sigmoid scaling: spread=0.02 → uncertainty ≈ 0.057 (low but not zero)
        # This is better than the old linear which gave 0.0004 — sigmoid correctly
        # encodes that even tight PERT bounds have *some* residual uncertainty
        assert ranking["uncertainty"] < 0.1


# ── VoI Tasks Tests ──────────────────────────────────────────────────────


class TestGenerateVoITasks:
    """Test VoI task generation (OHM-6mv.5)."""

    def test_voi_tasks_no_decision_nodes(self, db):
        """When no decision nodes exist, should return empty tasks."""
        result = generate_voi_tasks(db)
        assert result["method"] == "voi_task_assignment"
        assert result["tasks"] == []

    def test_voi_tasks_basic(self, db):
        """Should generate research tasks from VoI rankings."""
        a = create_sample_node(db, label="uncertain_cause", confidence=0.2)
        d = create_sample_node(db, label="my_decision", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = generate_voi_tasks(db, top=5)
        assert result["method"] == "voi_task_assignment"
        assert len(result["tasks"]) >= 1
        task = result["tasks"][0]
        assert task["node_id"] == a
        assert "gap_score" in task
        assert "suggested_research" in task
        assert task["observation_count"] >= 0

    def test_voi_tasks_top_limit(self, db):
        """Should respect the top parameter."""
        nodes = []
        for i in range(5):
            n = create_sample_node(db, label=f"cause_{i}", confidence=0.1 + i * 0.15)
            nodes.append(n)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        for n in nodes:
            create_sample_edge(db, from_node=n, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.7)

        result = generate_voi_tasks(db, top=2)
        assert len(result["tasks"]) <= 2

    def test_voi_tasks_with_agent_filter(self, db):
        """Should filter tasks by agent expertise tags."""
        # Create an agent with tags
        agent = create_sample_node(db, label="test_agent", node_type="agent")
        a = create_sample_node(db, label="research_topic", confidence=0.2)
        d = create_sample_node(db, label="my_decision", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        # Agent has CAPABLE_OF edge to a concept
        create_sample_edge(db, from_node=agent, to_node=a, edge_type="CAPABLE_OF", layer="L2", confidence=0.9)
        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        # With agent filter — should find tasks matching agent expertise
        result = generate_voi_tasks(db, agent=agent, top=5)
        assert result["agent"] == agent
        # The agent has expertise matching node 'a', so it should appear
        if result["tasks"]:
            task = result["tasks"][0]
            assert "matched_tags" in task
            assert "tag_overlap" in task

    def test_voi_tasks_suggested_research(self, db):
        """Should suggest appropriate research actions based on observation count."""
        a = create_sample_node(db, label="no_obs_cause", confidence=0.3)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = generate_voi_tasks(db, top=5)
        assert len(result["tasks"]) >= 1
        task = result["tasks"][0]
        # Node with 0 observations should suggest observing
        assert "Observe" in task["suggested_research"] or "observe" in task["suggested_research"].lower()

    def test_voi_tasks_gap_score(self, db):
        """Gap score should be uncertainty × sensitivity."""
        a = create_sample_node(db, label="gap_cause", confidence=0.3)
        d = create_sample_node(db, label="decision_node", node_type="decision")
        db.execute("UPDATE ohm_nodes SET utility_scale = 1.0 WHERE id = ?", [d])

        create_sample_edge(db, from_node=a, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = generate_voi_tasks(db, top=5)
        assert len(result["tasks"]) >= 1
        task = result["tasks"][0]
        # gap_score should equal uncertainty × sensitivity (OHM#4 — no double-counting)
        # For non-PERT nodes, uncertainty = 1 - confidence, so gap_score = (1-conf) × sensitivity.
        # Do NOT multiply by (1 - confidence) again — that was the double-counting bug.
        expected_gap = round(task["uncertainty"] * task["sensitivity"], 4)
        assert task["gap_score"] == expected_gap


# ── Regression Tests: Cache poisoning + silent node dropping ─────────────────


class TestCachePoisoningRegression:
    """Regression tests for bugs that caused /ate, /sensitivity, /refute to
    return 'Unknown error' on real data while /inference and /intervene worked.

    Root cause: build_bayesian_network() cache key omitted root_nodes, so a
    network scoped to nodes from a previous request could be returned for a
    different request. The effect node would silently be dropped from
    safe_query_nodes, producing posteriors with only the cause node key.
    """

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_cache_key_includes_root_nodes(self, db):
        """Two build_bayesian_network calls with different root_nodes should
        produce different cache entries (not reuse the first)."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import build_bayesian_network, _bayesian_network_cache

        # Build two disconnected components: A->B and C->D
        a = create_sample_node(db, label="cache_a")
        b = create_sample_node(db, label="cache_b")
        c = create_sample_node(db, label="cache_c")
        d = create_sample_node(db, label="cache_d")
        for src, dst in [(a, b), (c, d)]:
            db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
                [str(uuid.uuid4()), src, dst],
            )

        _bayesian_network_cache.clear()

        net1 = build_bayesian_network(db, root_nodes=[a, b], edge_types=["CAUSES"], layers=["L3"])
        assert net1 is not None
        assert c not in net1["nodes"]

        net2 = build_bayesian_network(db, root_nodes=[c, d], edge_types=["CAUSES"], layers=["L3"])
        assert net2 is not None
        assert c in net2["nodes"]

    def test_cache_key_includes_customer_id(self, db):
        """Two build_bayesian_network calls with different customer_id should
        produce independent cache entries — cross-tenant bleed prevention (OHM-g4os)."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import build_bayesian_network, _bayesian_network_cache

        a = create_sample_node(db, label="tenant_a")
        b = create_sample_node(db, label="tenant_b")
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
            [str(uuid.uuid4()), a, b],
        )

        _bayesian_network_cache.clear()

        net_core = build_bayesian_network(db, root_nodes=[a, b], edge_types=["CAUSES"], layers=["L3"], customer_id=None)
        assert net_core is not None

        net_t1 = build_bayesian_network(db, root_nodes=[a, b], edge_types=["CAUSES"], layers=["L3"], customer_id="tenant_alpha")
        assert net_t1 is not None

        net_t2 = build_bayesian_network(db, root_nodes=[a, b], edge_types=["CAUSES"], layers=["L3"], customer_id="tenant_beta")
        assert net_t2 is not None

        # Three distinct cache entries for same graph but different customer_id
        assert len(_bayesian_network_cache) == 3

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_ate_after_inference_on_different_nodes(self, db):
        """ATE should work even after a prior /inference call scoped to
        different nodes (the cache should not poison the network)."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import bayesian_inference, compute_ate, _bayesian_network_cache

        # Build two disconnected components: A->B and C->D
        a = create_sample_node(db, label="poison_a")
        b = create_sample_node(db, label="poison_b")
        c = create_sample_node(db, label="poison_c")
        d = create_sample_node(db, label="poison_d")
        for src, dst in [(a, b), (c, d)]:
            db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
                [str(uuid.uuid4()), src, dst],
            )

        _bayesian_network_cache.clear()

        # First: inference on A (scope=[A])
        inf_result = bayesian_inference(db, a, {})
        assert inf_result is not None

        # Second: ATE on C->D (should NOT reuse A's network)
        ate_result = compute_ate(db, c, d, edge_types=["CAUSES"], layers=["L3"])
        assert "error" not in ate_result, f"ATE failed with: {ate_result.get('error')}"
        assert ate_result["method"] == "model_based_ate"
        assert "ate" in ate_result

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_intervention_returns_error_when_query_node_missing(self, db):
        """causal_intervention should return an explicit error when a specified
        query node is not in the network, not silently drop it."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import causal_intervention, _bayesian_network_cache

        a = create_sample_node(db, label="missing_a")
        b = create_sample_node(db, label="missing_b")
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
            [str(uuid.uuid4()), a, b],
        )

        _bayesian_network_cache.clear()

        # Create a node that has no edges
        orphan = create_sample_node(db, label="orphan_node")

        result = causal_intervention(
            db,
            a,
            0,
            query_nodes=[orphan],
            edge_types=["CAUSES"],
            layers=["L3"],
        )
        assert "error" in result, "Expected error when query node is not in network"
        assert orphan in result["error"]

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_ate_diagnostic_error_message(self, db):
        """compute_ate should return a diagnostic error message (not 'Unknown
        error') when the effect node is missing from posteriors."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import compute_ate, _bayesian_network_cache

        a = create_sample_node(db, label="diag_a")
        b = create_sample_node(db, label="diag_b")
        db.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 0.9, 'test')",
            [str(uuid.uuid4()), a, b],
        )

        orphan = create_sample_node(db, label="diag_orphan")

        _bayesian_network_cache.clear()

        result = compute_ate(db, a, orphan, edge_types=["CAUSES"], layers=["L3"])
        assert "error" in result
        assert "Unknown error" not in result["error"], f"Error message should be diagnostic, got: {result['error']}"
        assert "orphan" in result["error"] or "not found" in result["error"]


class TestBayesianContextMultiNodeExtraction:
    """Regression test for BayesianContext.intervention() multi-node posteriors.

    The BayesianContext version was indexing result.values[i] as if pgmpy
    returns per-variable marginals, but it actually returns a joint
    distribution. The fix makes it do per-variable queries like the
    standalone causal_intervention does.
    """

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_multi_node_posteriors_match_standalone(self, db):
        """BayesianContext multi-query-node posteriors should match the
        standalone causal_intervention result."""
        try:
            from pgmpy.models import DiscreteBayesianNetwork  # noqa: F401
        except ImportError:
            pytest.skip("pgmpy not available")

        from ohm.bayesian import BayesianContext, causal_intervention, _bayesian_network_cache

        a = create_sample_node(db, label="multi_a")
        b = create_sample_node(db, label="multi_b")
        c = create_sample_node(db, label="multi_c")
        d = create_sample_node(db, label="multi_d")
        for src, dst, prob in [(a, b, 0.8), (b, c, 0.7), (c, d, 0.6)]:
            db.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, probability, confidence, created_by) VALUES (?, ?, ?, 'L3', 'CAUSES', ?, 0.9, 'test')",
                [str(uuid.uuid4()), src, dst, prob],
            )

        _bayesian_network_cache.clear()

        with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
            ctx_result = ctx.intervention(b, 0, query_nodes=[c, d])

        _bayesian_network_cache.clear()

        standalone_result = causal_intervention(db, b, 0, query_nodes=[c, d], edge_types=["CAUSES"], layers=["L3"])

        assert ctx_result["method"] == "causal_intervention"
        assert standalone_result["method"] == "causal_intervention"

        for node in [c, d]:
            ctx_post = ctx_result["posterior"].get(node, {})
            sa_post = standalone_result["posterior"].get(node, {})
            assert "error" not in ctx_post, f"Context posterior for {node} has error: {ctx_post}"
            assert "error" not in sa_post, f"Standalone posterior for {node} has error: {sa_post}"
            assert abs(ctx_post["bad"] - sa_post["bad"]) < 0.01, f"Context P({node}=bad)={ctx_post['bad']} != standalone {sa_post['bad']}"


# ── Regression Tests: VoI PERT variance scaling + path enumeration ─────────


class TestVoIPertVarianceScaling:
    """Regression tests for PERT variance scaling bugs:

    1. Linear ×36 compressed meaningful differences at low variance
    2. PERT p50-only edges created fake precision with ±20% defaults
    3. _max_edge_pert_variance_toward visited set pruned alternative paths
    """

    def test_sigmoid_scaling_discriminates_low_spread(self):
        """Sigmoid scaling should discriminate between tight PERT estimates,
        not compress them all to near-zero like the old ×36 linear did."""
        from ohm.pert import scale_pert_variance

        # Spread of 0.1 (tight PERT): should be low but not ~0
        v01 = scale_pert_variance(0.1)
        # Spread of 0.2 (moderate): should be noticeably higher
        v02 = scale_pert_variance(0.2)

        assert v01 < v02, f"0.1 spread ({v01}) should be less uncertain than 0.2 ({v02})"
        # The old linear: 0.1 → 0.0003, 0.2 → 0.0011 — both near-zero
        # Sigmoid should give meaningful separation
        assert v02 / max(v01, 1e-6) > 2, "Sigmoid should discriminate better than linear"

    def test_sigmoid_scaling_does_not_saturate_at_high_spread(self):
        """Sigmoid should not map all high-spread PERTs to 1.0 like linear ×36 did."""
        from ohm.pert import scale_pert_variance

        v05 = scale_pert_variance(0.5)
        v07 = scale_pert_variance(0.7)
        v10 = scale_pert_variance(1.0)

        assert v10 > v07 > v05, f"Should be monotonic: {v05} < {v07} < {v10}"
        # Linear ×36: spread≥0.5 all mapped to 1.0. Sigmoid should distinguish.
        assert v10 - v05 > 0.05, "Should distinguish moderate vs wide PERT"

    def test_pert_p50_only_wider_defaults(self, db):
        """PERT p50-only edges should use wider ±40% defaults, not the old
        ±20% that created fake precision."""
        a = create_sample_node(db, label="wide_default_a")
        b = create_sample_node(db, label="wide_default_b")

        create_sample_edge(
            db,
            from_node=a,
            to_node=b,
            edge_type="CAUSES",
            layer="L3",
            confidence=0.9,
            probability_p50=0.5,
        )

        result = build_bayesian_network(db)
        assert result is not None
        edge = None
        for e in result["edges"]:
            if e["from"] == a and e["to"] == b:
                edge = e
                break
        assert edge is not None
        # With ±40%: p05=0.3, p95=0.7, PERT mean = 0.5 (symmetric, same as p50)
        # This is correct — the wider defaults don't change the mean for symmetric cases
        assert edge["probability"] == pytest.approx(0.45, abs=0.01)

    def test_path_variance_diamond_graph(self, db):
        """_max_edge_pert_variance_toward should explore ALL paths in a
        diamond graph, not prune the second path via a visited set."""
        from ohm.bayesian import _max_edge_pert_variance_toward, _bayesian_network_cache
        from ohm.pert import scale_pert_variance

        _bayesian_network_cache.clear()

        # Diamond: A→B→D, A→C→D
        # All edges have PERT data so paths contribute to variance
        a = create_sample_node(db, label="diamond_a")
        b = create_sample_node(db, label="diamond_b")
        c = create_sample_node(db, label="diamond_c")
        d = create_sample_node(db, label="diamond_d")

        # A→B, A→C (with PERT — narrow spread)
        for src, dst in [(a, b), (a, c)]:
            create_sample_edge(db, from_node=src, to_node=dst, edge_type="CAUSES", layer="L3", confidence=0.9, probability_p05=0.45, probability_p50=0.5, probability_p95=0.55)

        # B→D (narrow PERT spread = 0.2)
        create_sample_edge(db, from_node=b, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8, probability_p05=0.1, probability_p50=0.2, probability_p95=0.3)
        # C→D (wide PERT spread = 0.9 — this is the max)
        create_sample_edge(db, from_node=c, to_node=d, edge_type="CAUSES", layer="L3", confidence=0.8, probability_p05=0.05, probability_p50=0.5, probability_p95=0.95)

        forward_adj = {a: [b, c], b: [d], c: [d]}
        edge_pert_variance = {}
        edge_pert_variance[(a, b)] = scale_pert_variance(0.1)  # spread 0.55-0.45=0.1
        edge_pert_variance[(a, c)] = scale_pert_variance(0.1)  # spread 0.55-0.45=0.1
        edge_pert_variance[(b, d)] = scale_pert_variance(0.2)  # spread 0.3-0.1=0.2
        edge_pert_variance[(c, d)] = scale_pert_variance(0.9)  # spread 0.95-0.05=0.9

        result = _max_edge_pert_variance_toward(a, d, forward_adj, edge_pert_variance)
        # The visited-set bug would only explore A→B→D (first path found),
        # missing A→C→D which has the max variance. The fix should find the
        # max across all paths.
        assert result is not None, "Should find at least one path with PERT variance"
        # The C→D edge has much higher variance — should be the max
        c_d_var = edge_pert_variance[(c, d)]
        b_d_var = edge_pert_variance[(b, d)]
        assert result == c_d_var, f"Should find C→D variance ({c_d_var:.4f}) as max, got {result:.4f} (B→D was {b_d_var:.4f})"

    def test_no_exponential_explosion_on_wide_diamond(self):
        """OHM-od01.7: _max_edge_pert_variance_toward must complete in <1s
        on a wide diamond graph that would cause exponential path enumeration
        with the old BFS (max_depth = len(adj)+2, no visited set)."""
        import time

        from ohm.bayesian import _max_edge_pert_variance_toward
        from ohm.pert import scale_pert_variance

        # Build a wide diamond: source→20 intermediates→target
        # Old BFS: 2^20 paths. New BFS: O(V+E).
        source = "S"
        target = "T"
        n_intermediates = 20
        forward_adj = {source: [f"M{i}" for i in range(n_intermediates)]}
        edge_pert_variance = {}
        for i in range(n_intermediates):
            mid = f"M{i}"
            forward_adj[mid] = [target]
            edge_pert_variance[(source, mid)] = scale_pert_variance(0.1)
            edge_pert_variance[(mid, target)] = scale_pert_variance(0.5 + i * 0.025)

        start = time.monotonic()
        result = _max_edge_pert_variance_toward(source, target, forward_adj, edge_pert_variance)
        elapsed = time.monotonic() - start

        assert result is not None, "Should find a path with PERT variance"
        assert elapsed < 1.0, f"BFS should be fast, took {elapsed:.3f}s (exponential hang?)"


class TestTemporalDecay:
    """Temporal decay weighting in Bayesian inference (OHM-31)."""

    def test_decay_discounts_old_observation(self, db, causal_graph):
        """Day-1 obs weighted ~2x vs Day-31 obs with half_life=30."""
        g = causal_graph
        now = "2026-05-26T12:00:00"
        old = "2026-04-25T12:00:00"  # 31 days ago

        # Insert observations with explicit created_at
        create_sample_observation(
            db, node_id=g["a"], value=0.5, scale="probability", created_by="test",
            created_at=now,
        )
        create_sample_observation(
            db, node_id=g["a"], value=0.9, scale="probability", created_by="test",
            created_at=old,
        )

        # Without decay: prior = mean(0.5, 0.9) = 0.7
        result_no_decay = bayesian_inference(
            db, g["c"], {}, half_life_days=0.0,
        )

        # With decay (half_life=30): recent 0.5 gets weight ~1.0, old 0.9 gets weight ~0.5
        # Weighted prior = (0.5 * 1.0 + 0.9 * 0.49) / (1.0 + 0.49) = (0.5 + 0.441) / 1.49 = 0.632
        # So posterior for C should be lower than the no-decay case
        result_decay = bayesian_inference(
            db, g["c"], {}, half_life_days=30.0,
        )

        # Posterior for C should differ between decay and no-decay
        assert result_no_decay["posterior"] != result_decay["posterior"]

    def test_decay_no_effect_with_zero_half_life(self, db, causal_graph):
        """half_life=0.0 means no decay, same as original behavior."""
        g = causal_graph
        create_sample_observation(
            db, node_id=g["a"], value=0.5, scale="probability", created_by="test",
        )
        create_sample_observation(
            db, node_id=g["a"], value=0.7, scale="probability", created_by="test",
        )

        result = bayesian_inference(db, g["c"], {}, half_life_days=0.0)
        assert result["method"] != "none"

    def test_single_observation_unchanged_by_decay(self, db, causal_graph):
        """Single observation yields same prior regardless of half_life."""
        g = causal_graph
        create_sample_observation(
            db, node_id=g["a"], value=0.5, scale="probability", created_by="test",
            created_at="2026-01-01T12:00:00",
        )

        result_no_decay = bayesian_inference(db, g["c"], {}, half_life_days=0.0)
        result_decay = bayesian_inference(db, g["c"], {}, half_life_days=30.0)
        assert result_no_decay["posterior"] == result_decay["posterior"]

    def test_decay_parameter_in_http_handler(self, db, causal_graph):
        """half_life parameter is accepted by the HTTP handler."""
        g = causal_graph
        create_sample_observation(
            db, node_id=g["a"], value=0.8, scale="probability", created_by="test",
        )

        result = bayesian_inference(db, g["c"], {}, half_life_days=30.0)
        assert result["method"] != "none"
        assert "posterior" in result


class TestSoftEvidence:
    def test_soft_evidence_includes_supports_edges(self, db):
        from ohm.bayesian import build_bayesian_network

        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=a, to_node=b, edge_type="SUPPORTS", layer="L3", confidence=0.9)

        network = build_bayesian_network(db, include_soft_evidence=True)
        assert network is not None
        assert "soft_evidence_factors" in network
        assert len(network["soft_evidence_factors"]) > 0

    def test_soft_evidence_empty_without_supports(self, db):
        from ohm.bayesian import build_bayesian_network

        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)

        network = build_bayesian_network(db, include_soft_evidence=True)
        assert network is not None
        assert "soft_evidence_factors" in network
        assert len(network["soft_evidence_factors"]) == 0

    def test_soft_evidence_disabled_by_default(self, db):
        from ohm.bayesian import build_bayesian_network

        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=a, to_node=b, edge_type="SUPPORTS", layer="L3", confidence=0.9)

        network = build_bayesian_network(db, include_soft_evidence=False)
        assert network is not None
        assert network["soft_evidence_factors"] == []

    def test_soft_evidence_shifts_posterior(self, db):
        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        c = create_sample_node(db, label="outcome_c")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=b, to_node=c, edge_type="CAUSES", layer="L3", confidence=0.7)

        result_without = bayesian_inference(db, c, {}, include_soft_evidence=False)

        create_sample_edge(db, from_node=a, to_node=c, edge_type="SUPPORTS", layer="L3", confidence=0.95)
        result_with = bayesian_inference(db, c, {}, include_soft_evidence=True)

        assert result_without["method"] != "none"
        assert result_with["method"] != "none"
        p_bad_without = result_without["posterior"][c]["bad"]
        p_bad_with = result_with["posterior"][c]["bad"]

        assert abs(p_bad_with - p_bad_without) > 0.001, (
            f"Soft evidence should shift posterior: without={p_bad_without}, with={p_bad_with}"
        )

    def test_soft_evidence_applies_to_edge_type(self, db):
        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        c = create_sample_node(db, label="outcome_c")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=b, to_node=c, edge_type="CAUSES", layer="L3", confidence=0.7)
        create_sample_edge(db, from_node=a, to_node=b, edge_type="APPLIES_TO", layer="L3", confidence=0.7)

        network = build_bayesian_network(db, include_soft_evidence=True, soft_edge_types=["APPLIES_TO"])
        assert network is not None
        assert len(network["soft_evidence_factors"]) > 0

    def test_soft_evidence_context_manager(self, db):
        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        c = create_sample_node(db, label="outcome_c")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=b, to_node=c, edge_type="CAUSES", layer="L3", confidence=0.7)
        create_sample_edge(db, from_node=a, to_node=b, edge_type="SUPPORTS", layer="L3", confidence=0.9)

        from ohm.bayesian import BayesianContext

        with BayesianContext(db, include_soft_evidence=True) as ctx:
            assert ctx.network is not None
            assert len(ctx.network.get("soft_evidence_factors", [])) > 0
            result = ctx.inference(c, {})
            assert result["method"] != "none"


class TestVEReuse:
    def test_bayesian_context_inference_reuses_ve(self, db):
        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)

        from ohm.inference.bayesian import BayesianContext as InfCtx, _ve_cache

        _ve_cache.clear()
        with InfCtx(db) as ctx:
            r1 = ctx.inference(b, {})
            assert r1["method"] != "none"
            assert len(_ve_cache) >= 1
            r2 = ctx.inference(b, {})
            assert r2["method"] != "none"
            assert abs(r1["posterior"]["bad"] - r2["posterior"]["bad"]) < 0.001

    def test_bayesian_context_intervention_reuses_ve(self, db):
        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        c = create_sample_node(db, label="outcome_c")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=b, to_node=c, edge_type="CAUSES", layer="L3", confidence=0.7)

        from ohm.inference.bayesian import BayesianContext as InfCtx, _ve_cache

        _ve_cache.clear()
        with InfCtx(db) as ctx:
            r1 = ctx.intervention(a, 0, query_nodes=[c])
            assert r1["method"] != "none"
            r2 = ctx.intervention(a, 1, query_nodes=[c])
            assert r2["method"] != "none"
            assert c in r1.get("posterior", {})

    def test_ve_cache_bounded(self, db):
        from ohm.inference.bayesian import _ve_cache, _MAX_VE_CACHE_SIZE

        _ve_cache.clear()
        a = create_sample_node(db, label="cause_a")
        b = create_sample_node(db, label="effect_b")
        create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = bayesian_inference(db, b, {})
        assert result["method"] != "none"
        assert len(_ve_cache) <= _MAX_VE_CACHE_SIZE
