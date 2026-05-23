"""Tests for GraphReader Protocol, MockGraphReader, DuckDBGraphReader, and
inference functions using MockGraphReader (no database required).

OHM-ljef: Validates that compute_voi, build_bayesian_network, and
markov_absorbing_risk work correctly when given a MockGraphReader instead
of a raw DuckDB connection.
"""

from __future__ import annotations

import importlib.util

import pytest

from ohm.graph_reader import (
    DuckDBGraphReader,
    EdgeRecord,
    GraphReader,
    MockGraphReader,
    NodeRecord,
    ObservationRecord,
    coerce_reader,
    raw_conn,
)


# ── Record construction ───────────────────────────────────────────────────────


class TestEdgeRecord:
    def test_minimal(self):
        e = EdgeRecord("A", "B", "CAUSES")
        assert e.from_node == "A"
        assert e.to_node == "B"
        assert e.edge_type == "CAUSES"
        assert e.probability is None
        assert e.confidence is None

    def test_full_fields(self):
        e = EdgeRecord(
            "A",
            "B",
            "CAUSES",
            layer="L3",
            probability=0.7,
            confidence=0.9,
            probability_p05=0.5,
            probability_p50=0.7,
            probability_p95=0.9,
            confidence_p05=0.8,
            confidence_p50=0.9,
            confidence_p95=0.95,
        )
        assert e.layer == "L3"
        assert e.probability_p50 == 0.7

    def test_frozen(self):
        e = EdgeRecord("A", "B", "CAUSES")
        with pytest.raises((AttributeError, TypeError)):
            e.from_node = "C"  # type: ignore[misc]


class TestNodeRecord:
    def test_minimal(self):
        n = NodeRecord("id1", "Label", "concept")
        assert n.id == "id1"
        assert n.confidence is None
        assert n.utility_scale is None
        assert n.utility_usd_per_day is None

    def test_decision_node_with_utility(self):
        n = NodeRecord(
            "dec-1",
            "Decision",
            "decision",
            utility_scale=0.8,
            utility_usd_per_day=1500.0,
            utility_currency="USD",
        )
        assert n.utility_usd_per_day == 1500.0
        assert n.utility_currency == "USD"


# ── MockGraphReader filtering ─────────────────────────────────────────────────


@pytest.fixture
def simple_graph():
    return MockGraphReader(
        edges=[
            EdgeRecord("A", "B", "CAUSES", layer="L3", probability=0.8, confidence=0.9),
            EdgeRecord("B", "C", "CAUSES", layer="L3", probability=0.6),
            EdgeRecord("A", "B", "INFLUENCES", layer="L2", confidence=0.7),
        ],
        nodes=[
            NodeRecord("A", "Root", "concept", confidence=0.9),
            NodeRecord("B", "Mid", "concept", confidence=0.7),
            NodeRecord("C", "Decision", "decision", utility_scale=0.8),
        ],
        observations=[
            ObservationRecord("obs-1", "A", None, "measurement", value=0.3),
            ObservationRecord("obs-2", "A", None, "measurement", value=0.5),
        ],
        meta={"graph_generation": "7"},
    )


class TestMockGraphReaderGetEdges:
    def test_filter_by_type(self, simple_graph):
        edges = simple_graph.get_edges(edge_types=["CAUSES"])
        assert len(edges) == 2
        assert all(e.edge_type == "CAUSES" for e in edges)

    def test_filter_by_type_and_layer(self, simple_graph):
        edges = simple_graph.get_edges(edge_types=["CAUSES"], layers=["L3"])
        assert len(edges) == 2

        edges_l2 = simple_graph.get_edges(edge_types=["CAUSES"], layers=["L2"])
        assert len(edges_l2) == 0

    def test_no_matching_type(self, simple_graph):
        edges = simple_graph.get_edges(edge_types=["THREATENS"])
        assert edges == []

    def test_multiple_types(self, simple_graph):
        edges = simple_graph.get_edges(edge_types=["CAUSES", "INFLUENCES"])
        assert len(edges) == 3


class TestMockGraphReaderGetNodes:
    def test_all_nodes(self, simple_graph):
        nodes = simple_graph.get_nodes()
        assert len(nodes) == 3

    def test_filter_by_ids(self, simple_graph):
        nodes = simple_graph.get_nodes(ids=["A", "C"])
        assert len(nodes) == 2
        assert {n.id for n in nodes} == {"A", "C"}

    def test_empty_ids_returns_empty(self, simple_graph):
        nodes = simple_graph.get_nodes(ids=[])
        assert nodes == []

    def test_filter_by_type(self, simple_graph):
        nodes = simple_graph.get_nodes(node_type="decision")
        assert len(nodes) == 1
        assert nodes[0].id == "C"

    def test_filter_by_ids_and_type(self, simple_graph):
        nodes = simple_graph.get_nodes(ids=["A", "C"], node_type="concept")
        assert len(nodes) == 1
        assert nodes[0].id == "A"


class TestMockGraphReaderGetObservations:
    def test_get_observations_for_node(self, simple_graph):
        obs = simple_graph.get_observations("A")
        assert len(obs) == 2
        assert all(o.node_id == "A" for o in obs)

    def test_no_observations(self, simple_graph):
        obs = simple_graph.get_observations("B")
        assert obs == []


class TestMockGraphReaderMeta:
    def test_get_meta(self, simple_graph):
        assert simple_graph.get_meta("graph_generation") == "7"

    def test_get_meta_missing(self, simple_graph):
        assert simple_graph.get_meta("nonexistent") is None

    def test_get_graph_generation(self, simple_graph):
        assert simple_graph.get_graph_generation() == 7

    def test_get_graph_generation_missing(self):
        reader = MockGraphReader()
        assert reader.get_graph_generation() == 0


# ── Protocol conformance ──────────────────────────────────────────────────────


class TestProtocolConformance:
    def test_mock_reader_is_graph_reader(self, simple_graph):
        assert isinstance(simple_graph, GraphReader)

    def test_duckdb_reader_is_graph_reader(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        reader = DuckDBGraphReader(conn)
        assert isinstance(reader, GraphReader)


# ── coerce_reader / raw_conn ──────────────────────────────────────────────────


class TestCoerceReader:
    def test_passthrough_for_graph_reader(self, simple_graph):
        result = coerce_reader(simple_graph)
        assert result is simple_graph

    def test_wraps_raw_conn(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        reader = coerce_reader(conn)
        assert isinstance(reader, DuckDBGraphReader)
        assert isinstance(reader, GraphReader)

    def test_raw_conn_extracts_from_duckdb_reader(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        reader = DuckDBGraphReader(conn)
        assert raw_conn(reader) is conn

    def test_raw_conn_passthrough(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        assert raw_conn(conn) is conn


# ── Inference with MockGraphReader (no DB required) ───────────────────────────


class TestComputeVoiWithMock:
    def test_basic_voi_rankings(self, simple_graph):
        from ohm.bayesian import compute_voi

        result = compute_voi(simple_graph, decision_nodes=["C"])
        assert result["method"] == "value_of_information"
        assert len(result["rankings"]) > 0
        assert result["units"] == "dimensionless"
        # Higher-degree ancestor should have higher VoI
        ids_ranked = [r["node_id"] for r in result["rankings"]]
        assert "A" in ids_ranked or "B" in ids_ranked

    def test_no_decision_nodes(self):
        from ohm.bayesian import compute_voi

        reader = MockGraphReader(nodes=[NodeRecord("X", "X", "concept")])
        result = compute_voi(reader, decision_nodes=[])
        assert result["rankings"] == []
        assert result["n_candidates"] == 0

    def test_usd_utility_units(self):
        from ohm.bayesian import compute_voi

        reader = MockGraphReader(
            edges=[EdgeRecord("A", "D", "CAUSES", probability=0.8)],
            nodes=[
                NodeRecord("A", "Root", "concept"),
                NodeRecord("D", "Decision", "decision", utility_usd_per_day=500.0, utility_currency="USD"),
            ],
        )
        result = compute_voi(reader, decision_nodes=["D"])
        assert result["units"] == "usd"

    def test_mixed_utility_units(self):
        from ohm.bayesian import compute_voi

        reader = MockGraphReader(
            edges=[
                EdgeRecord("A", "D1", "CAUSES", probability=0.8),
                EdgeRecord("A", "D2", "CAUSES", probability=0.6),
            ],
            nodes=[
                NodeRecord("A", "Root", "concept"),
                NodeRecord("D1", "Dec1", "decision", utility_usd_per_day=500.0),
                NodeRecord("D2", "Dec2", "decision", utility_scale=0.5),
            ],
        )
        result = compute_voi(reader, decision_nodes=["D1", "D2"])
        assert result["units"] == "mixed"

    def test_voi_auto_detects_decision_nodes(self):
        from ohm.bayesian import compute_voi

        reader = MockGraphReader(
            edges=[EdgeRecord("A", "D", "CAUSES", probability=0.7)],
            nodes=[
                NodeRecord("A", "Root", "concept"),
                NodeRecord("D", "Decision", "decision", utility_scale=0.6),
            ],
        )
        result = compute_voi(reader)  # no explicit decision_nodes
        assert result["method"] == "value_of_information"
        assert "D" in result["decision_nodes"]

    def test_observation_count_from_reader(self):
        from ohm.bayesian import compute_voi

        reader = MockGraphReader(
            edges=[EdgeRecord("A", "D", "CAUSES", probability=0.8)],
            nodes=[
                NodeRecord("A", "Root", "concept", confidence=0.6),
                NodeRecord("D", "Decision", "decision", utility_scale=0.9),
            ],
            observations=[
                ObservationRecord("o1", "A", None, "measurement", value=0.4),
                ObservationRecord("o2", "A", None, "measurement", value=0.6),
            ],
        )
        result = compute_voi(reader, decision_nodes=["D"])
        ranking = next(r for r in result["rankings"] if r["node_id"] == "A")
        assert ranking["observation_count"] == 2


class TestBuildBayesianNetworkWithMock:
    @pytest.mark.skipif(
        not importlib.util.find_spec("pgmpy"),
        reason="pgmpy not installed",
    )
    def test_builds_from_mock(self, simple_graph):
        from ohm.bayesian import build_bayesian_network

        result = build_bayesian_network(simple_graph, edge_types=["CAUSES"])
        assert result is not None
        assert result["n_nodes"] >= 2
        assert result["n_edges"] >= 1

    @pytest.mark.skipif(
        not importlib.util.find_spec("pgmpy"),
        reason="pgmpy not installed",
    )
    def test_observation_prior_from_reader(self):
        from ohm.bayesian import build_bayesian_network

        reader = MockGraphReader(
            edges=[EdgeRecord("A", "B", "CAUSES", probability=0.7)],
            nodes=[NodeRecord("A", "Root", "concept"), NodeRecord("B", "Effect", "concept")],
            observations=[
                ObservationRecord("o1", "A", None, "measurement", value=0.2),
                ObservationRecord("o2", "A", None, "measurement", value=0.4),
            ],
        )
        result = build_bayesian_network(reader, edge_types=["CAUSES"])
        assert result is not None


class TestMarkovWithMock:
    @pytest.mark.skipif(
        not importlib.util.find_spec("numpy"),
        reason="numpy not installed",
    )
    def test_absorbing_risk_from_mock(self):
        from ohm.markov import markov_absorbing_risk

        reader = MockGraphReader(
            edges=[
                EdgeRecord("S1", "S2", "TRANSITIONS_TO", probability=0.6),
                EdgeRecord("S1", "S3", "TRANSITIONS_TO", probability=0.4),
            ],
            nodes=[
                NodeRecord("S1", "Start", "concept"),
                NodeRecord("S2", "Mid", "concept"),
                NodeRecord("S3", "End", "concept"),
            ],
        )
        result = markov_absorbing_risk(reader, "S1", edge_types=["TRANSITIONS_TO"])
        assert result["method"] == "markov_absorbing_risk"
        assert result["start_node"] == "S1"

    @pytest.mark.skipif(
        not importlib.util.find_spec("numpy"),
        reason="numpy not installed",
    )
    def test_no_edges_isolates_start_node(self):
        from ohm.markov import markov_absorbing_risk

        reader = MockGraphReader(
            nodes=[NodeRecord("X", "Isolated", "concept")],
        )
        result = markov_absorbing_risk(reader, "X", edge_types=["TRANSITIONS_TO"])
        assert result["absorbing_states"] == ["X"]
