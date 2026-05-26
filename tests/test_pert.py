"""Tests for PERT three-point estimation functions (src/ohm/pert.py)."""

import pytest
from ohm.pert import (
    PERTELError,
    anchored_pert,
    aggregate_mixture_of_experts,
    auto_pert_from_observations,
    auto_pert_from_edge_distribution,
    compute_pert_mean,
    compute_pert_variance,
    scale_pert_variance,
    validate_pert,
)


class TestValidatePert:
    """Tests for validate_pert()."""

    @pytest.mark.parametrize(
        "p05,p50,p95",
        [
            (0.1, 0.5, 0.9),
            (0.0, 0.0, 0.1),
            (0.0, 0.5, 1.0),
            (0.3, 0.3, 0.7),  # p05 == p50 is allowed
        ],
    )
    def test_valid_triples(self, p05, p50, p95):
        validate_pert(p05, p50, p95)  # should not raise

    def test_p05_below_lower_bound(self):
        with pytest.raises(PERTELError, match="p05"):
            validate_pert(-0.1, 0.5, 0.9)

    def test_p95_above_upper_bound(self):
        with pytest.raises(PERTELError, match="p95"):
            validate_pert(0.1, 0.5, 1.1)

    def test_p50_out_of_bounds(self):
        with pytest.raises(PERTELError, match="p50"):
            validate_pert(0.1, 1.2, 1.5)

    def test_p05_exceeds_p50(self):
        with pytest.raises(PERTELError, match="optimistic exceeds most-likely"):
            validate_pert(0.7, 0.5, 0.9)

    def test_p50_exceeds_p95(self):
        with pytest.raises(PERTELError, match="most-likely exceeds pessimistic"):
            validate_pert(0.1, 0.8, 0.5)

    def test_degenerate_zero_spread(self):
        with pytest.raises(PERTELError, match="degenerate"):
            validate_pert(0.5, 0.5, 0.5)

    def test_custom_bounds(self):
        validate_pert(1.0, 5.0, 9.0, bounds=(0.0, 10.0))

    def test_custom_bounds_violation(self):
        with pytest.raises(PERTELError):
            validate_pert(1.0, 5.0, 11.0, bounds=(0.0, 10.0))


class TestComputePertMean:
    """Tests for compute_pert_mean()."""

    @pytest.mark.parametrize(
        "p05,p50,p95,expected",
        [
            (0.0, 0.5, 1.0, 0.5),  # symmetric → mean == p50
            (0.0, 0.0, 0.6, 0.1),  # (0 + 0 + 0.6) / 6
            (0.2, 0.4, 1.0, 0.4666666),  # (0.2 + 1.6 + 1.0) / 6
            (0.1, 0.5, 0.9, 0.5),  # symmetric
        ],
    )
    def test_known_values(self, p05, p50, p95, expected):
        result = compute_pert_mean(p05, p50, p95)
        assert abs(result - expected) < 1e-5

    def test_mean_between_p05_and_p95(self):
        result = compute_pert_mean(0.1, 0.6, 0.8)
        assert 0.1 <= result <= 0.8

    def test_mean_weights_mode_4x(self):
        # μ = (O + 4M + P) / 6; mode contributes 4/6 weight
        result = compute_pert_mean(0.0, 1.0, 0.0)
        assert abs(result - 4 / 6) < 1e-10


class TestComputePertVariance:
    """Tests for compute_pert_variance()."""

    @pytest.mark.parametrize(
        "p05,p95,expected",
        [
            (0.0, 0.6, 0.01),  # ((0.6-0.0)/6)^2 = 0.1^2 = 0.01
            (0.1, 0.7, 0.01),  # ((0.6)/6)^2 = 0.1^2 = 0.01
            (0.4, 0.4, 0.0),  # zero spread (p05==p95)
            (0.0, 1.0, (1 / 6) ** 2),  # full range
        ],
    )
    def test_known_values(self, p05, p95, expected):
        result = compute_pert_variance(p05, p95)
        assert abs(result - expected) < 1e-10

    def test_variance_nonnegative(self):
        assert compute_pert_variance(0.2, 0.8) >= 0.0

    def test_larger_spread_higher_variance(self):
        narrow = compute_pert_variance(0.4, 0.6)
        wide = compute_pert_variance(0.1, 0.9)
        assert wide > narrow


class TestAggregateMixtureOfExperts:
    """Tests for aggregate_mixture_of_experts()."""

    def test_single_expert_passthrough(self):
        result = aggregate_mixture_of_experts([(0.1, 0.5, 0.9)])
        assert abs(result["mean"] - 0.5) < 1e-6
        assert abs(result["between_variance"]) < 1e-10

    def test_empty_returns_zeros(self):
        result = aggregate_mixture_of_experts([])
        assert result["mean"] == 0.0
        assert result["variance"] == 0.0

    def test_uniform_weights_default(self):
        estimates = [(0.1, 0.3, 0.5), (0.3, 0.5, 0.7)]
        result = aggregate_mixture_of_experts(estimates)
        # Each expert has weight 0.5; means are 0.3 and 0.5
        expected_mean = 0.5 * compute_pert_mean(0.1, 0.3, 0.5) + 0.5 * compute_pert_mean(0.3, 0.5, 0.7)
        assert abs(result["mean"] - expected_mean) < 1e-6

    def test_custom_weights_normalized(self):
        estimates = [(0.0, 0.2, 0.4), (0.6, 0.8, 1.0)]
        result = aggregate_mixture_of_experts(estimates, weights=[1.0, 3.0])
        # Weight 1.0 for first, 3.0 for second → normalized [0.25, 0.75]
        m1 = compute_pert_mean(0.0, 0.2, 0.4)
        m2 = compute_pert_mean(0.6, 0.8, 1.0)
        expected = 0.25 * m1 + 0.75 * m2
        assert abs(result["mean"] - expected) < 1e-6

    def test_between_variance_nonzero_when_experts_disagree(self):
        result = aggregate_mixture_of_experts([(0.0, 0.1, 0.2), (0.8, 0.9, 1.0)])
        assert result["between_variance"] > 0.0

    def test_total_variance_equals_within_plus_between(self):
        estimates = [(0.1, 0.3, 0.5), (0.4, 0.6, 0.8)]
        result = aggregate_mixture_of_experts(estimates)
        assert abs(result["total_variance"] - (result["variance"] + result["between_variance"])) < 1e-10

    def test_zero_weights_raises(self):
        with pytest.raises(PERTELError, match="positive"):
            aggregate_mixture_of_experts([(0.1, 0.5, 0.9)], weights=[0.0])

    def test_aggregated_p05_p50_p95_in_result(self):
        estimates = [(0.1, 0.5, 0.9)]
        result = aggregate_mixture_of_experts(estimates)
        assert result["p05"] == pytest.approx(0.1)
        assert result["p50"] == pytest.approx(0.5)
        assert result["p95"] == pytest.approx(0.9)


class TestAnchoredPert:
    """Tests for anchored_pert()."""

    def test_no_adjustment_returns_pert_values(self):
        result = anchored_pert(0.1, 0.5, 0.9, reference_class=0.3, adjustment_factor=0.0)
        pert_mean = compute_pert_mean(0.1, 0.5, 0.9)
        assert abs(result["mean"] - pert_mean) < 1e-6

    def test_full_adjustment_pulls_to_reference(self):
        result = anchored_pert(0.1, 0.5, 0.9, reference_class=0.3, adjustment_factor=1.0)
        assert abs(result["mean"] - 0.3) < 1e-6

    def test_half_adjustment(self):
        pert_mean = compute_pert_mean(0.2, 0.6, 0.8)
        result = anchored_pert(0.2, 0.6, 0.8, reference_class=0.0, adjustment_factor=0.5)
        expected = pert_mean * 0.5 + 0.0 * 0.5
        assert abs(result["mean"] - expected) < 1e-6

    def test_result_keys_present(self):
        result = anchored_pert(0.1, 0.5, 0.9, reference_class=0.4)
        assert {"p05", "p50", "p95", "mean", "variance"} == set(result.keys())

    def test_p05_nonnegative(self):
        result = anchored_pert(0.0, 0.1, 0.2, reference_class=0.0, adjustment_factor=0.9)
        assert result["p05"] >= 0.0

    def test_p95_at_most_one(self):
        result = anchored_pert(0.8, 0.9, 1.0, reference_class=1.0, adjustment_factor=0.9)
        assert result["p95"] <= 1.0

    def test_invalid_adjustment_factor_raises(self):
        with pytest.raises(PERTELError, match="adjustment_factor"):
            anchored_pert(0.1, 0.5, 0.9, reference_class=0.3, adjustment_factor=1.5)

    def test_negative_adjustment_factor_raises(self):
        with pytest.raises(PERTELError):
            anchored_pert(0.1, 0.5, 0.9, reference_class=0.3, adjustment_factor=-0.1)

    def test_variance_in_result(self):
        result = anchored_pert(0.1, 0.5, 0.9, reference_class=0.5)
        expected_var = compute_pert_variance(result["p05"], result["p95"])
        assert abs(result["variance"] - expected_var) < 1e-10


class TestAutoPertFromObservations:
    """Tests for auto_pert_from_observations()."""

    def test_insufficient_data_returns_zeros(self):
        result = auto_pert_from_observations([0.5])
        assert result["p05"] == 0.0
        assert result["p50"] == 0.0
        assert result["p95"] == 0.0
        assert result["method"] == "insufficient_data"
        assert result["n"] == 1

    def test_three_observations_exact_percentiles(self):
        values = [0.2, 0.5, 0.8]
        result = auto_pert_from_observations(values)
        assert result["n"] == 3
        assert result["method"] == "empirical_percentiles"
        assert result["p50"] == 0.5

    def test_five_observations_interpolated_percentiles(self):
        values = [0.1, 0.3, 0.5, 0.7, 0.9]
        result = auto_pert_from_observations(values)
        assert result["n"] == 5
        assert 0.0 <= result["p05"] <= 0.5
        assert 0.0 <= result["p50"] <= 1.0

    def test_mean_bounded_by_p05_and_p95(self):
        values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        result = auto_pert_from_observations(values)
        mean = compute_pert_mean(result["p05"], result["p50"], result["p95"])
        assert result["p05"] <= mean <= result["p95"]

    def test_custom_bounds(self):
        values = [2.0, 5.0, 8.0]
        result = auto_pert_from_observations(values, bounds=(0.0, 10.0))
        assert result["p05"] >= 0.0
        assert result["p95"] <= 10.0

    def test_empty_list_returns_zeros(self):
        result = auto_pert_from_observations([])
        assert result["p05"] == 0.0
        assert result["p50"] == 0.0
        assert result["p95"] == 0.0
        assert result["method"] == "insufficient_data"
        assert result["n"] == 0


class TestAutoPertFromEdgeDistribution:
    """Tests for auto_pert_from_edge_distribution()."""

    def test_empty_probs_returns_no_data(self):
        result = auto_pert_from_edge_distribution([None, None])
        assert result["p05"] == 0.0
        assert result["p50"] == 0.0
        assert result["p95"] == 0.0
        assert result["method"] == "no_data"
        assert result["n"] == 0

    def test_single_probability_uses_spread(self):
        result = auto_pert_from_edge_distribution([0.5])
        assert result["n"] == 1
        assert result["p50"] == 0.5
        assert result["method"] == "single_value_with_spread"
        assert result["p05"] < result["p50"] < result["p95"]

    def test_multiple_probabilities_uses_percentiles(self):
        probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        result = auto_pert_from_edge_distribution(probs)
        assert result["n"] == 9
        assert result["method"] == "edge_distribution_percentiles"
        assert result["p05"] <= result["p50"] <= result["p95"]

    def test_none_filtered_out(self):
        probs = [0.2, 0.4, None, 0.6, 0.8, None]
        result = auto_pert_from_edge_distribution(probs)
        assert result["n"] == 4

    def test_custom_default_spread(self):
        result = auto_pert_from_edge_distribution([0.5], default_spread=0.3)
        assert result["p50"] == 0.5
        assert result["p05"] == pytest.approx(0.35)
        assert result["p95"] == pytest.approx(0.65)

    def test_mean_bounded_by_p05_and_p95(self):
        probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        result = auto_pert_from_edge_distribution(probs)
        mean = compute_pert_mean(result["p05"], result["p50"], result["p95"])
        assert result["p05"] <= mean <= result["p95"]


class TestScalePertVariance:
    """Tests for scale_pert_variance()."""

    @pytest.mark.parametrize(
        "spread,expected_min",
        [
            (0.1, 0.08),  # low spread → low uncertainty signal
            (0.3, 0.46),  # moderate
            (0.5, 0.86),  # wide
            (1.0, 0.98),  # full range → high
        ],
    )
    def test_spread_values(self, spread, expected_min):
        result = scale_pert_variance(spread)
        assert 0.0 <= result <= 1.0
        assert result >= expected_min

    def test_returns_float(self):
        result = scale_pert_variance(0.4)
        assert isinstance(result, float)

    def test_zero_spread(self):
        result = scale_pert_variance(0.0)
        assert result < 0.5  # should be on the low side
