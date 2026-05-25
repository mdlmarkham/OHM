"""Tests for BayesianContext — network caching layer (OHM-7bc).

Tests that BayesianContext builds the network once and reuses it
across multiple inference/intervention/ATE calls.
"""

from __future__ import annotations

import pytest

from ohm.bayesian import BayesianContext
from tests.conftest import create_sample_node, create_sample_edge


@pytest.fixture
def causal_chain(db):
    """Create a simple causal chain: A -> B -> C."""
    a = create_sample_node(db, label="cause")
    b = create_sample_node(db, label="mediator")
    c = create_sample_node(db, label="outcome")
    create_sample_edge(db, from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.8)
    create_sample_edge(db, from_node=b, to_node=c, edge_type="CAUSES", layer="L3", confidence=0.7)
    return {"a": a, "b": b, "c": c}


class TestBayesianContext:
    """Test BayesianContext builds network once and reuses it."""

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_builds_network_once(self, db, causal_chain):
        """BayesianContext should build the network once, not per method call."""
        from unittest.mock import patch
        from ohm.bayesian import build_bayesian_network as original_build

        call_count = {"count": 0}

        def counting_build(*args, **kwargs):
            call_count["count"] += 1
            return original_build(*args, **kwargs)

        with patch("ohm.inference.bayesian.build_bayesian_network", side_effect=counting_build):
            ctx = BayesianContext(
                db,
                edge_types=["CAUSES"],
                layers=["L3"],
            )
            # Network should be built exactly once during construction
            assert call_count["count"] == 1, f"Expected 1 build call, got {call_count['count']}"

            # Multiple method calls should NOT rebuild the network
            ctx.inference(causal_chain["c"], {causal_chain["a"]: 1})
            ctx.inference(causal_chain["b"], {causal_chain["a"]: 0})
            # Still only 1 build call
            assert call_count["count"] == 1, f"Expected 1 build call after inference, got {call_count['count']}"

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_inference(self, db, causal_chain):
        """BayesianContext.inference should return posterior probabilities."""
        ctx = BayesianContext(db, edge_types=["CAUSES"], layers=["L3"])
        result = ctx.inference(causal_chain["c"], {causal_chain["a"]: 1})
        assert result is not None
        assert "posterior" in result
        assert "good" in result["posterior"] or "bad" in result["posterior"]

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_intervention(self, db, causal_chain):
        """BayesianContext.intervention should return intervention results."""
        ctx = BayesianContext(db, edge_types=["CAUSES"], layers=["L3"])
        result = ctx.intervention(causal_chain["a"], 0)
        assert result is not None
        assert "method" in result

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_ate(self, db, causal_chain):
        """BayesianContext.ate should return ATE results."""
        ctx = BayesianContext(db, edge_types=["CAUSES"], layers=["L3"])
        result = ctx.ate(causal_chain["a"], causal_chain["c"])
        assert result is not None
        assert "ate" in result

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_sensitivity(self, db, causal_chain):
        """BayesianContext.sensitivity should return E-value results."""
        ctx = BayesianContext(db, edge_types=["CAUSES"], layers=["L3"])
        result = ctx.sensitivity(causal_chain["a"], causal_chain["c"])
        assert result is not None
        assert "e_value" in result

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_adjustment_sets(self, db, causal_chain):
        """BayesianContext.adjustment_sets should return adjustment sets."""
        ctx = BayesianContext(db, edge_types=["CAUSES"], layers=["L3"])
        result = ctx.adjustment_sets(causal_chain["a"], causal_chain["c"])
        assert result is not None
        assert "method" in result

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_manager(self, db, causal_chain):
        """BayesianContext should work as a context manager."""
        with BayesianContext(db, edge_types=["CAUSES"], layers=["L3"]) as ctx:
            result = ctx.inference(causal_chain["c"], {causal_chain["a"]: 1})
            assert result is not None

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not installed"), reason="pgmpy not available")
    def test_context_no_edges_returns_none(self, db):
        """BayesianContext with no edges should have no network."""
        ctx = BayesianContext(db, edge_types=["CAUSES"])
        assert ctx.network is None

    @pytest.mark.skipif(not pytest.importorskip("pgmpy", reason="pgmpy not available"), reason="pgmpy not available")
    def test_context_custom_root_prior(self, db, causal_chain):
        """BayesianContext should accept root_prior parameter."""
        ctx = BayesianContext(db, edge_types=["CAUSES"], layers=["L3"], root_prior=0.5)
        assert ctx.network is not None
        result = ctx.inference(causal_chain["c"], {causal_chain["a"]: 1})
        assert result is not None
