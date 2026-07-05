"""Tests for the Monte Carlo delegation module (OHM-lqpk.4).

Tests the pure-Python fallback path (the Rust extension is not built on
dev boxes without a Rust toolchain). When the Rust extension IS available,
these tests verify the same boundary contract holds.
"""

import pytest

from ohm.mc import monte_carlo_sim, has_rust_extension, _python_sim


def _sample_adjacency():
    """Build a small adjacency list for testing.

    Graph: A -> B (conf=1.0, prob=1.0) -> C (conf=1.0, prob=1.0)
           A -> D (conf=1.0, prob=0.0)
    """
    return {
        "A": [("B", 1.0, 1.0), ("D", 1.0, 0.0)],
        "B": [("C", 1.0, 1.0)],
    }


class TestMonteCarloSim:
    def test_deterministic_with_seed(self):
        """Same seed produces same results."""
        adj = _sample_adjacency()
        counts1, totals1 = monte_carlo_sim(adj, "A", 100, 5, seed=42)
        counts2, totals2 = monte_carlo_sim(adj, "A", 100, 5, seed=42)
        assert counts1 == counts2
        assert totals1 == totals2

    def test_certain_propagation(self):
        """With conf=1.0 and prob=1.0, all reachable nodes are always activated."""
        adj = _sample_adjacency()
        counts, totals = monte_carlo_sim(adj, "A", 100, 5, seed=0)
        # B and C should be activated in every trial (conf=1.0, prob=1.0)
        assert counts["B"] == 100
        assert counts["C"] == 100
        # D has prob=0.0, so it should never be activated
        assert "D" not in counts or counts["D"] == 0

    def test_zero_probabilitites(self):
        """With all probabilities at 0, no targets are activated."""
        adj = {"A": [("B", 1.0, 0.0), ("C", 1.0, 0.0)]}
        counts, totals = monte_carlo_sim(adj, "A", 50, 3, seed=0)
        assert counts == {}
        assert all(t == 0 for t in totals)

    def test_depth_limit(self):
        """Depth=1 only activates immediate neighbors, not grandchildren."""
        adj = _sample_adjacency()
        counts, _ = monte_carlo_sim(adj, "A", 100, 1, seed=0)
        assert counts["B"] == 100  # B is depth 1
        assert "C" not in counts or counts["C"] == 0  # C is depth 2

    def test_source_not_counted(self):
        """The source node is NOT in impact_counts (only targets)."""
        adj = {"A": [("B", 1.0, 1.0)]}
        counts, _ = monte_carlo_sim(adj, "A", 10, 3, seed=0)
        assert "A" not in counts
        assert counts["B"] == 10

    def test_per_trial_totals(self):
        """per_trial_totals has one entry per trial."""
        adj = _sample_adjacency()
        _, totals = monte_carlo_sim(adj, "A", 50, 5, seed=0)
        assert len(totals) == 50

    def test_empty_adjacency(self):
        """Source with no outgoing edges produces empty counts."""
        counts, totals = monte_carlo_sim({}, "lonely", 10, 3, seed=0)
        assert counts == {}
        assert all(t == 0 for t in totals)

    def test_no_seed_varies(self):
        """Without a seed, results vary between runs (probabilistic)."""
        adj = {"A": [("B", 0.5, 0.5)]}
        counts1, _ = monte_carlo_sim(adj, "A", 1000, 3)
        counts2, _ = monte_carlo_sim(adj, "A", 1000, 3)
        # Extremely unlikely to get identical results without seed
        assert counts1 != counts2

    def test_has_rust_extension_returns_bool(self):
        """has_rust_extension() returns a boolean."""
        assert isinstance(has_rust_extension(), bool)

    def test_python_sim_matches_delegation(self):
        """When Rust is not available, monte_carlo_sim delegates to _python_sim."""
        if has_rust_extension():
            pytest.skip("Rust extension is available — fallback not tested")
        adj = _sample_adjacency()
        c1, t1 = monte_carlo_sim(adj, "A", 50, 5, seed=99)
        c2, t2 = _python_sim(adj, "A", 50, 5, seed=99)
        assert c1 == c2
        assert t1 == t2

    def test_stochastic_sampling_ratio(self):
        """With conf=0.5 and prob=1.0, ~50% of trials activate the target."""
        adj = {"A": [("B", 0.5, 1.0)]}
        counts, _ = monte_carlo_sim(adj, "A", 10000, 1, seed=42)
        # Expected ~5000, allow ±500 (5% tolerance)
        assert 4500 <= counts.get("B", 0) <= 5500
