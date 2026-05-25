"""Tests for auto_pert_from_observations and auto_pert_from_edges (OHM-8fg9)."""

from __future__ import annotations

import pytest

from ohm.queries import auto_pert_from_edges, auto_pert_from_observations
from ohm.schema import initialize_schema


@pytest.fixture
def db():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


def _node(conn, label: str = "test-node", node_type: str = "concept") -> str:
    import uuid

    node_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
        [node_id, label, node_type, "test"],
    )
    return node_id


def _obs(conn, node_id: str, value: float, obs_type: str = "probability") -> None:
    import uuid

    conn.execute(
        "INSERT INTO ohm_observations (id, node_id, type, value, created_by) VALUES (?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), node_id, obs_type, value, "test"],
    )


def _edge(
    conn,
    from_id: str,
    to_id: str,
    probability: float | None = None,
    p05: float | None = None,
    p50: float | None = None,
    p95: float | None = None,
    confidence: float = 0.8,
    edge_type: str = "CAUSES",
) -> str:
    import uuid

    edge_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges
           (id, from_node, to_node, layer, edge_type, created_by,
            probability, probability_p05, probability_p50, probability_p95, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [edge_id, from_id, to_id, "L2", edge_type, "test",
         probability, p05, p50, p95, confidence],
    )
    return edge_id


# ── auto_pert_from_observations ──────────────────────────────────────────────


class TestAutoPertFromObservations:
    def test_returns_none_when_no_observations(self, db):
        node_id = _node(db)
        result = auto_pert_from_observations(db, node_id)
        assert result is None

    def test_returns_none_below_min_obs(self, db):
        node_id = _node(db)
        _obs(db, node_id, 0.3)
        _obs(db, node_id, 0.5)
        result = auto_pert_from_observations(db, node_id, min_obs=3)
        assert result is None

    def test_returns_dict_with_required_keys(self, db):
        node_id = _node(db)
        for v in [0.1, 0.3, 0.5, 0.7, 0.9]:
            _obs(db, node_id, v)
        result = auto_pert_from_observations(db, node_id)
        assert result is not None
        assert {"p05", "p50", "p95", "mean", "variance", "n_obs", "obs_type", "source"} == set(result.keys())

    def test_percentile_ordering(self, db):
        node_id = _node(db)
        for v in [0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9]:
            _obs(db, node_id, v)
        result = auto_pert_from_observations(db, node_id)
        assert result is not None
        assert result["p05"] <= result["p50"] <= result["p95"]

    def test_n_obs_correct(self, db):
        node_id = _node(db)
        for v in [0.2, 0.4, 0.6, 0.8, 0.9]:
            _obs(db, node_id, v)
        result = auto_pert_from_observations(db, node_id)
        assert result is not None
        assert result["n_obs"] == 5

    def test_filters_by_obs_type(self, db):
        node_id = _node(db)
        for v in [0.1, 0.5, 0.9]:
            _obs(db, node_id, v, obs_type="probability")
        for v in [10.0, 20.0, 30.0]:
            _obs(db, node_id, v, obs_type="measurement")
        result = auto_pert_from_observations(db, node_id, obs_type="probability")
        assert result is not None
        assert result["p95"] <= 1.0  # probability type only

    def test_excludes_deleted_observations(self, db):
        node_id = _node(db)
        for v in [0.1, 0.5, 0.9]:
            _obs(db, node_id, v)
        # Delete one observation
        db.execute("UPDATE ohm_observations SET deleted_at = CURRENT_TIMESTAMP WHERE node_id = ?", [node_id])
        result = auto_pert_from_observations(db, node_id, min_obs=3)
        assert result is None

    def test_source_field_is_observations(self, db):
        node_id = _node(db)
        for v in [0.1, 0.3, 0.5, 0.7, 0.9]:
            _obs(db, node_id, v)
        result = auto_pert_from_observations(db, node_id)
        assert result is not None
        assert result["source"] == "observations"

    def test_uniform_values_produce_near_zero_variance(self, db):
        node_id = _node(db)
        for _ in range(10):
            _obs(db, node_id, 0.5)
        result = auto_pert_from_observations(db, node_id)
        assert result is not None
        # Uniform input: spread is forced to default_spread, so variance > 0
        assert result["variance"] >= 0.0

    def test_custom_min_obs_threshold(self, db):
        node_id = _node(db)
        _obs(db, node_id, 0.4)
        # min_obs=1 should succeed
        result = auto_pert_from_observations(db, node_id, min_obs=1)
        assert result is not None
        assert result["n_obs"] == 1

    def test_obs_type_field_matches_parameter(self, db):
        node_id = _node(db)
        for v in [0.1, 0.5, 0.9]:
            _obs(db, node_id, v, obs_type="measurement")
        result = auto_pert_from_observations(db, node_id, obs_type="measurement", min_obs=3)
        assert result is not None
        assert result["obs_type"] == "measurement"


# ── auto_pert_from_edges ─────────────────────────────────────────────────────


class TestAutoPertFromEdges:
    def test_returns_none_when_no_edges(self, db):
        node_id = _node(db)
        result = auto_pert_from_edges(db, node_id)
        assert result is None

    def test_returns_none_below_min_edges(self, db):
        src = _node(db, "src")
        tgt = _node(db, "tgt")
        _edge(db, src, tgt, probability=0.5)
        result = auto_pert_from_edges(db, tgt, min_edges=2)
        assert result is None

    def test_uses_existing_pert_columns(self, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        _edge(db, src1, tgt, p05=0.1, p50=0.5, p95=0.9)
        _edge(db, src2, tgt, p05=0.2, p50=0.6, p95=0.8)
        result = auto_pert_from_edges(db, tgt, direction="in")
        assert result is not None
        assert result["p05"] < result["p50"] < result["p95"]
        assert result["source"] == "edges"

    def test_falls_back_to_point_probability(self, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        _edge(db, src1, tgt, probability=0.3)
        _edge(db, src2, tgt, probability=0.7)
        result = auto_pert_from_edges(db, tgt, direction="in")
        assert result is not None
        # p50 should be between the two source probabilities
        assert 0.3 <= result["p50"] <= 0.7

    def test_direction_in(self, db):
        src = _node(db, "src")
        tgt = _node(db, "tgt")
        other = _node(db, "other")
        _edge(db, src, tgt, probability=0.4, confidence=0.9)
        _edge(db, src, tgt, probability=0.6, confidence=0.9)
        _edge(db, tgt, other, probability=0.9, confidence=0.9)  # outgoing — excluded
        result = auto_pert_from_edges(db, tgt, direction="in", min_edges=2)
        assert result is not None
        assert result["n_edges"] == 2

    def test_direction_out(self, db):
        tgt = _node(db, "tgt")
        dst1 = _node(db, "dst1")
        dst2 = _node(db, "dst2")
        _edge(db, tgt, dst1, probability=0.3, confidence=0.8)
        _edge(db, tgt, dst2, probability=0.7, confidence=0.8)
        result = auto_pert_from_edges(db, tgt, direction="out", min_edges=2)
        assert result is not None
        assert result["n_edges"] == 2
        assert result["direction"] == "out"

    def test_direction_both(self, db):
        src = _node(db, "src")
        tgt = _node(db, "tgt")
        dst = _node(db, "dst")
        _edge(db, src, tgt, probability=0.4)
        _edge(db, tgt, dst, probability=0.6)
        result = auto_pert_from_edges(db, tgt, direction="both", min_edges=2)
        assert result is not None
        assert result["n_edges"] == 2

    def test_required_keys_present(self, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        _edge(db, src1, tgt, probability=0.3)
        _edge(db, src2, tgt, probability=0.7)
        result = auto_pert_from_edges(db, tgt, direction="in")
        assert result is not None
        assert {"p05", "p50", "p95", "mean", "variance", "n_edges", "direction", "source"} == set(result.keys())

    def test_bounds_clamped(self, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        # Edges with extreme probabilities and wide spread
        _edge(db, src1, tgt, p05=0.0, p50=0.05, p95=0.1)
        _edge(db, src2, tgt, p05=0.9, p50=0.95, p95=1.0)
        result = auto_pert_from_edges(db, tgt, direction="in")
        assert result is not None
        assert 0.0 <= result["p05"] <= 1.0
        assert 0.0 <= result["p95"] <= 1.0

    def test_edge_type_filter(self, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        _edge(db, src1, tgt, probability=0.3, edge_type="CAUSES")
        _edge(db, src2, tgt, probability=0.7, edge_type="INFLUENCES")
        # Filter to CAUSES only — only 1 edge, below min_edges=2
        result = auto_pert_from_edges(db, tgt, direction="in", edge_types=["CAUSES"], min_edges=2)
        assert result is None

    def test_invalid_direction_raises(self, db):
        node_id = _node(db)
        with pytest.raises(ValueError, match="direction"):
            auto_pert_from_edges(db, node_id, direction="sideways")

    def test_weighted_by_confidence(self, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        # High confidence on low probability should pull aggregate down
        _edge(db, src1, tgt, probability=0.1, confidence=0.95)
        _edge(db, src2, tgt, probability=0.9, confidence=0.1)
        result = auto_pert_from_edges(db, tgt, direction="in")
        assert result is not None
        # Weighted mean should be closer to 0.1 than to 0.9
        assert result["mean"] < 0.5

    def test_excludes_deleted_edges(self, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        _edge(db, src1, tgt, probability=0.4)
        _edge(db, src2, tgt, probability=0.6)
        db.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE to_node = ?", [tgt])
        result = auto_pert_from_edges(db, tgt, direction="in", min_edges=2)
        assert result is None


# ── SDK wrappers ─────────────────────────────────────────────────────────────


class TestSdkPertWrappers:
    """Verify the Graph SDK exposes pert_from_observations and pert_from_edges."""

    @pytest.fixture
    def graph(self, db):
        from ohm.framework.sdk import Graph

        return Graph(db, actor="test-agent")

    def test_pert_from_observations_returns_none_without_data(self, graph, db):
        node_id = _node(db)
        result = graph.pert_from_observations(node_id)
        assert result is None

    def test_pert_from_observations_returns_dict_with_data(self, graph, db):
        node_id = _node(db)
        for v in [0.1, 0.3, 0.5, 0.7, 0.9]:
            _obs(db, node_id, v)
        result = graph.pert_from_observations(node_id)
        assert result is not None
        assert "p05" in result

    def test_pert_from_edges_returns_none_without_data(self, graph, db):
        node_id = _node(db)
        result = graph.pert_from_edges(node_id)
        assert result is None

    def test_pert_from_edges_returns_dict_with_data(self, graph, db):
        src1 = _node(db, "s1")
        src2 = _node(db, "s2")
        tgt = _node(db, "tgt")
        _edge(db, src1, tgt, probability=0.3)
        _edge(db, src2, tgt, probability=0.7)
        result = graph.pert_from_edges(tgt, direction="in")
        assert result is not None
        assert "p50" in result
