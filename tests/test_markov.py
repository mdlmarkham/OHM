"""Tests for OHM Markov chain analysis (OHM-g09)."""

from __future__ import annotations

import pytest

from ohm.markov import NUMPY_AVAILABLE, markov_absorbing_risk, markov_expected_steps
from tests.conftest import create_sample_edge, create_sample_node

pytestmark = pytest.mark.skipif(not NUMPY_AVAILABLE, reason="numpy not installed")


@pytest.fixture
def test_db():
    from tests.conftest import create_test_db
    return create_test_db()


def _build_linear_chain(conn):
    healthy = create_sample_node(conn, label="healthy")
    symptomatic = create_sample_node(conn, label="symptomatic")
    critical = create_sample_node(conn, label="critical")
    deceased = create_sample_node(conn, label="deceased")

    create_sample_edge(conn, from_node=healthy, to_node=symptomatic, edge_type="TRANSITIONS_TO", probability=0.3)
    create_sample_edge(conn, from_node=healthy, to_node=healthy, edge_type="TRANSITIONS_TO", probability=0.7)
    create_sample_edge(conn, from_node=symptomatic, to_node=critical, edge_type="TRANSITIONS_TO", probability=0.2)
    create_sample_edge(conn, from_node=symptomatic, to_node=symptomatic, edge_type="TRANSITIONS_TO", probability=0.5)
    create_sample_edge(conn, from_node=symptomatic, to_node=healthy, edge_type="TRANSITIONS_TO", probability=0.3)
    create_sample_edge(conn, from_node=critical, to_node=deceased, edge_type="TRANSITIONS_TO", probability=0.4)
    create_sample_edge(conn, from_node=critical, to_node=critical, edge_type="TRANSITIONS_TO", probability=0.6)

    return healthy, symptomatic, critical, deceased


class TestAbsorbingRisk:
    def test_linear_chain_absorption(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_absorbing_risk(
            test_db, healthy, edge_types=["TRANSITIONS_TO"]
        )

        assert result["method"] == "markov_absorbing_risk"
        assert result["start_node"] == healthy
        assert deceased in result["absorption_probabilities"]
        assert 0 < result["absorption_probabilities"][deceased] <= 1.0
        assert deceased in result["absorbing_states"]
        assert healthy in result["transient_states"]

    def test_start_node_is_absorbing(self, test_db):
        node_a = create_sample_node(test_db, label="absorbing_only")
        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])
        assert result["absorption_probabilities"] == {node_a: 1.0}

    def test_start_node_not_in_graph(self, test_db):
        result = markov_absorbing_risk(test_db, "nonexistent", edge_types=["TRANSITIONS_TO"])
        assert "error" in result
        assert "not in graph" in result["error"]

    def test_no_edges(self, test_db):
        node_a = create_sample_node(test_db, label="isolated")
        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])
        assert result["absorption_probabilities"] == {node_a: 1.0}

    def test_absorption_probabilities_sum_to_one(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_absorbing_risk(
            test_db, healthy, edge_types=["TRANSITIONS_TO"]
        )

        total = sum(result["absorption_probabilities"].values())
        assert abs(total - 1.0) < 0.01

    def test_multiple_absorbing_states(self, test_db):
        start = create_sample_node(test_db, label="start")
        sink_a = create_sample_node(test_db, label="sink_a")
        sink_b = create_sample_node(test_db, label="sink_b")

        create_sample_edge(conn=test_db, from_node=start, to_node=sink_a, edge_type="TRANSITIONS_TO", probability=0.6)
        create_sample_edge(conn=test_db, from_node=start, to_node=sink_b, edge_type="TRANSITIONS_TO", probability=0.4)

        result = markov_absorbing_risk(test_db, start, edge_types=["TRANSITIONS_TO"])

        assert abs(result["absorption_probabilities"][sink_a] - 0.6) < 0.01
        assert abs(result["absorption_probabilities"][sink_b] - 0.4) < 0.01


class TestExpectedSteps:
    def test_linear_chain_steps(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_expected_steps(
            test_db, healthy, edge_types=["TRANSITIONS_TO"]
        )

        assert result["method"] == "markov_expected_steps"
        assert result["expected_steps"] > 0
        assert result["expected_steps_per_state"][healthy] > 0

    def test_absorbing_node_zero_steps(self, test_db):
        node_a = create_sample_node(test_db, label="absorbing")
        result = markov_expected_steps(test_db, node_a, edge_types=["TRANSITIONS_TO"])
        assert result["expected_steps"] == 0.0

    def test_transient_steps_greater_than_closer_states(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_expected_steps(
            test_db, healthy, edge_types=["TRANSITIONS_TO"]
        )

        healthy_steps = result["expected_steps_per_state"][healthy]
        symptomatic_steps = result["expected_steps_per_state"][symptomatic]
        assert healthy_steps > symptomatic_steps

    def test_target_state(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_expected_steps(
            test_db, healthy, target_state=deceased, edge_types=["TRANSITIONS_TO"]
        )

        assert "target_state" in result
        assert "target_probability" in result
        assert result["target_probability"] > 0
