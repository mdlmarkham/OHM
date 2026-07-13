"""Tests for OHM-843: Monte Carlo prospect simulation.

Covers the POST /simulate/{prospect_id} endpoint and the underlying
simulate_prospect query function. Verifies Beta-PERT sampling,
per-expectation statistics, sensitivity ranking, experiment_result
observation persistence, VoI cross-validation, and error cases.
"""

import json
import pytest

from tests.conftest import _request

pytestmark = pytest.mark.integration


@pytest.fixture
def seed_prospect_with_expectations(test_server):
    """Create a prospect with two expectation nodes linked via CONTAINS."""
    port, store = test_server
    conn = store.conn

    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at) VALUES "
        "('target1', 'Revenue Target', 'concept', 'Q3 revenue', 'metis', CURRENT_TIMESTAMP), "
        "('target2', 'Cost Target', 'concept', 'Q3 cost', 'metis', CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at, metadata) VALUES "
        "('prospect1', 'Q3 Campaign', 'prospect', 'Third quarter campaign', 'metis', CURRENT_TIMESTAMP, '{}'), "
        "('exp1', 'Revenue Expectation', 'expectation', 'Revenue target', 'metis', CURRENT_TIMESTAMP, "
        "'{\"p10\": 8.0, \"p50\": 10.0, \"p90\": 12.0, \"unit\": \"M USD\", \"expected_value\": 10.0}'), "
        "('exp2', 'Cost Expectation', 'expectation', 'Cost target', 'metis', CURRENT_TIMESTAMP, "
        "'{\"p10\": 3.0, \"p50\": 5.0, \"p90\": 8.0, \"unit\": \"M USD\", \"expected_value\": 5.0}')"
    )
    conn.execute(
        "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
        "('prospect1', 'exp1', 'CONTAINS', 'L1', 1.0, 'metis', CURRENT_TIMESTAMP), "
        "('prospect1', 'exp2', 'CONTAINS', 'L1', 1.0, 'metis', CURRENT_TIMESTAMP), "
        "('exp1', 'target1', 'EXPECTS', 'L4', 1.0, 'metis', CURRENT_TIMESTAMP), "
        "('exp2', 'target2', 'EXPECTS', 'L4', 1.0, 'metis', CURRENT_TIMESTAMP)"
    )
    conn.commit()
    return port, store


@pytest.fixture
def seed_prospect_no_expectations(test_server):
    """Create a prospect with no expectation children."""
    port, store = test_server
    conn = store.conn

    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at, metadata) VALUES "
        "('prospect_empty', 'Empty Prospect', 'prospect', 'No expectations', 'metis', CURRENT_TIMESTAMP, '{}')"
    )
    conn.commit()
    return port, store


@pytest.fixture
def seed_non_prospect(test_server):
    """Create a non-prospect node."""
    port, store = test_server
    conn = store.conn

    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at) VALUES "
        "('concept1', 'Some Concept', 'concept', 'Not a prospect', 'metis', CURRENT_TIMESTAMP)"
    )
    conn.commit()
    return port, store


class TestPostSimulate:
    """POST /simulate/{prospect_id} — Monte Carlo prospect simulation."""

    def test_simulate_basic(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 1000,
            "seed": 42,
        })
        assert status == 200
        assert data["prospect_id"] == "prospect1"
        assert data["n_iterations"] == 1000
        assert data["seed"] == 42
        assert len(data["expectations"]) == 2

    def test_simulate_returns_distribution_stats(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 2000,
            "seed": 42,
        })
        assert status == 200
        for exp in data["expectations"]:
            assert "mean" in exp
            assert "std" in exp
            assert "p5" in exp
            assert "p50_sim" in exp
            assert "p95" in exp
            assert "sensitivity_score" in exp

    def test_simulate_pert_mean_close_to_p50(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 5000,
            "seed": 42,
        })
        assert status == 200
        for exp in data["expectations"]:
            p10 = exp["p10"]
            p50 = exp["p50"]
            p90 = exp["p90"]
            pert_mean = (p10 + 4.0 * p50 + p90) / 6.0
            assert abs(exp["mean"] - pert_mean) < 1.0

    def test_simulate_persists_experiment_result_observation(self, seed_prospect_with_expectations):
        port, store = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 500,
            "seed": 42,
        })
        assert status == 200

        obs = store.read_conn.execute(
            "SELECT * FROM ohm_observations WHERE node_id = 'prospect1' AND type = 'experiment_result' AND deleted_at IS NULL"
        ).fetchall()
        assert len(obs) >= 1

    def test_simulate_with_default_iterations(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {})
        assert status == 200
        assert data["n_iterations"] == 5000

    def test_simulate_reproducible_with_seed(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status1, data1 = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 1000, "seed": 42,
        })
        status2, data2 = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 1000, "seed": 42,
        })
        assert status1 == 200
        assert status2 == 200
        for e1, e2 in zip(data1["expectations"], data2["expectations"]):
            assert e1["mean"] == e2["mean"]

    def test_simulate_different_seeds_different_results(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status1, data1 = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 1000, "seed": 42,
        })
        status2, data2 = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 1000, "seed": 99,
        })
        assert status1 == 200
        assert status2 == 200
        assert data1["expectations"][0]["mean"] != data2["expectations"][0]["mean"]

    def test_simulate_aggregate_stats(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 1000, "seed": 42,
        })
        assert status == 200
        assert "aggregate" in data
        assert "mean" in data["aggregate"]
        assert "std" in data["aggregate"]

    def test_simulate_sensitivity_ranking_sorted(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 2000, "seed": 42,
        })
        assert status == 200
        scores = [e["sensitivity_score"] for e in data["expectations"]]
        assert scores == sorted(scores, reverse=True)

    def test_simulate_no_hardcoded_units(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 500, "seed": 42,
        })
        assert status == 200
        for exp in data["expectations"]:
            assert exp["unit"] == "M USD"

    def test_simulate_voi_cross_validation_field(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {
            "n_iterations": 1000, "seed": 42,
        })
        assert status == 200
        assert "voi_cross_validation" in data
        assert "spearman_correlation" in data["voi_cross_validation"]
        assert data["voi_cross_validation"]["threshold"] == 0.5

    def test_simulate_note_mentions_v2_deferral(self, seed_prospect_with_expectations):
        port, _ = seed_prospect_with_expectations
        status, data = _request("POST", port, "/simulate/prospect1", {})
        assert status == 200
        assert "v2" in data["note"].lower()

    def test_simulate_single_expectation(self, test_server):
        port, store = test_server
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at, metadata) VALUES "
            "('prospect_single', 'Single Exp Prospect', 'prospect', 'One expectation', 'metis', CURRENT_TIMESTAMP, '{}'), "
            "('exp_single', 'Only Expectation', 'expectation', 'The only one', 'metis', CURRENT_TIMESTAMP, "
            "'{\"p10\": 0.1, \"p50\": 0.5, \"p90\": 0.9, \"unit\": \"ratio\", \"expected_value\": 0.5}')"
        )
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
            "('prospect_single', 'exp_single', 'CONTAINS', 'L1', 1.0, 'metis', CURRENT_TIMESTAMP)"
        )
        conn.commit()

        status, data = _request("POST", port, "/simulate/prospect_single", {
            "n_iterations": 500, "seed": 42,
        })
        assert status == 200
        assert len(data["expectations"]) == 1
        assert data["expectations"][0]["sensitivity_score"] == 1.0


class TestPostSimulateErrors:
    """Error cases for POST /simulate/{prospect_id}."""

    def test_simulate_nonexistent_prospect(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/simulate/nonexistent", {})
        assert status == 422
        assert "error" in data

    def test_simulate_non_prospect_node(self, seed_non_prospect):
        port, _ = seed_non_prospect
        status, data = _request("POST", port, "/simulate/concept1", {})
        assert status == 422
        assert "not a prospect" in data.get("message", "").lower() or "not" in data.get("message", "").lower()

    def test_simulate_prospect_no_expectations(self, seed_prospect_no_expectations):
        port, _ = seed_prospect_no_expectations
        status, data = _request("POST", port, "/simulate/prospect_empty", {})
        assert status == 422
        assert "no expectation" in data.get("message", "").lower()


class TestSimulateQuery:
    """Direct query function tests (bypassing HTTP)."""

    def test_simulate_prospect_function(self, test_db):
        from ohm.graph.queries.simulate import simulate_prospect

        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at, metadata) VALUES "
            "('p1', 'Test Prospect', 'prospect', 'Test', 'agent', CURRENT_TIMESTAMP, '{}'), "
            "('e1', 'Exp 1', 'expectation', 'Expectation 1', 'agent', CURRENT_TIMESTAMP, "
            "'{\"p10\": 1.0, \"p50\": 2.0, \"p90\": 3.0, \"unit\": \"units\", \"expected_value\": 2.0}')"
        )
        test_db.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
            "('p1', 'e1', 'CONTAINS', 'L1', 1.0, 'agent', CURRENT_TIMESTAMP)"
        )
        test_db.commit()

        result = simulate_prospect(test_db, prospect_id="p1", n_iterations=100, seed=42)
        assert result["prospect_id"] == "p1"
        assert len(result["expectations"]) == 1
        assert result["expectations"][0]["mean"] > 1.0
        assert result["expectations"][0]["mean"] < 3.0

    def test_simulate_prospect_not_found(self, test_db):
        from ohm.graph.queries.simulate import simulate_prospect

        with pytest.raises(ValueError, match="not found"):
            simulate_prospect(test_db, prospect_id="nonexistent")

    def test_simulate_prospect_wrong_type(self, test_db):
        from ohm.graph.queries.simulate import simulate_prospect

        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES "
            "('c1', 'Concept', 'concept', 'agent', CURRENT_TIMESTAMP)"
        )
        test_db.commit()

        with pytest.raises(ValueError, match="not 'prospect'"):
            simulate_prospect(test_db, prospect_id="c1")

    def test_simulate_prospect_no_expectations_raises(self, test_db):
        from ohm.graph.queries.simulate import simulate_prospect

        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at, metadata) VALUES "
            "('p2', 'Empty', 'prospect', 'agent', CURRENT_TIMESTAMP, '{}')"
        )
        test_db.commit()

        with pytest.raises(ValueError, match="no expectation"):
            simulate_prospect(test_db, prospect_id="p2")


class TestSpearmanRankCorrelation:
    """Unit tests for the Spearman rank correlation helper."""

    def test_perfect_positive_correlation(self):
        from ohm.graph.queries.simulate import _spearman_rank_correlation
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        assert _spearman_rank_correlation(x, y) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self):
        from ohm.graph.queries.simulate import _spearman_rank_correlation
        x = [1, 2, 3, 4, 5]
        y = [5, 4, 3, 2, 1]
        assert _spearman_rank_correlation(x, y) == pytest.approx(-1.0)

    def test_no_correlation(self):
        from ohm.graph.queries.simulate import _spearman_rank_correlation
        x = [1, 2, 3, 4, 5]
        y = [3, 1, 5, 2, 4]
        corr = _spearman_rank_correlation(x, y)
        assert -0.5 < corr < 0.5

    def test_zero_variance_returns_zero(self):
        from ohm.graph.queries.simulate import _spearman_rank_correlation
        x = [1, 1, 1, 1]
        y = [1, 2, 3, 4]
        assert _spearman_rank_correlation(x, y) == 0.0

    def test_too_short_returns_zero(self):
        from ohm.graph.queries.simulate import _spearman_rank_correlation
        assert _spearman_rank_correlation([1], [2]) == 0.0

    def test_handles_ties(self):
        from ohm.graph.queries.simulate import _spearman_rank_correlation
        x = [1, 2, 2, 3]
        y = [1, 2, 2, 3]
        assert _spearman_rank_correlation(x, y) == pytest.approx(1.0)


class TestBetaPertSample:
    """Unit tests for the Beta-PERT sampling function."""

    def test_sample_within_bounds(self):
        from ohm.graph.queries.simulate import _beta_pert_sample
        import random
        rng = random.Random(42)
        for _ in range(100):
            val = _beta_pert_sample(0.1, 0.5, 0.9, rng)
            assert 0.1 <= val <= 0.9

    def test_sample_mean_close_to_pert_mean(self):
        from ohm.graph.queries.simulate import _beta_pert_sample
        import random
        rng = random.Random(42)
        samples = [_beta_pert_sample(1.0, 2.0, 3.0, rng) for _ in range(5000)]
        mean = sum(samples) / len(samples)
        pert_mean = (1.0 + 4.0 * 2.0 + 3.0) / 6.0
        assert abs(mean - pert_mean) < 0.2

    def test_degenerate_returns_p50(self):
        from ohm.graph.queries.simulate import _beta_pert_sample
        import random
        rng = random.Random(42)
        val = _beta_pert_sample(5.0, 5.0, 5.0, rng)
        assert val == 5.0