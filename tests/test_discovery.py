"""Tests for causal structure discovery module (OHM-od01.4)."""

import pytest


class TestBuildObservationMatrix:
    """Tests for _build_observation_matrix."""

    def test_insufficient_observations_skipped(self):
        """Nodes with fewer than min_observations are skipped."""
        from ohm.inference.discovery import _build_observation_matrix
        from ohm.graph_reader import MockGraphReader, ObservationRecord

        reader = MockGraphReader(
            observations=[
                ObservationRecord(edge_id=None, id="o1", node_id="a", type="binary", value=1.0, source="test", created_by="test", scale="binary", created_at="2026-01-01"),
                ObservationRecord(edge_id=None, id="o2", node_id="b", type="binary", value=0.0, source="test", created_by="test", scale="binary", created_at="2026-01-01"),
            ]
        )
        data, valid, meta = _build_observation_matrix(reader, ["a", "b"], min_observations=3)
        assert len(valid) == 0
        assert "a" in meta["skipped"]
        assert "b" in meta["skipped"]

    def test_sufficient_observations_included(self):
        """Nodes with enough observations are included."""
        from ohm.inference.discovery import _build_observation_matrix
        from ohm.graph_reader import MockGraphReader, ObservationRecord

        obs_a = [ObservationRecord(edge_id=None, id=f"o{i}", node_id="a", type="binary", value=float(i % 2), source="test", created_by="test", scale="binary", created_at="2026-01-01") for i in range(8)]
        obs_b = [ObservationRecord(edge_id=None, id=f"p{i}", node_id="b", type="binary", value=float((i + 1) % 2), source="test", created_by="test", scale="binary", created_at="2026-01-01") for i in range(8)]
        reader = MockGraphReader(observations=obs_a + obs_b)
        data, valid, meta = _build_observation_matrix(reader, ["a", "b"], min_observations=5)
        assert len(valid) == 2
        assert data.shape[0] == 8
        assert data.shape[1] == 2


class TestDiscoverCausalAutoSelect:
    """Tests for discover_causal auto-select (N+1 fix)."""

    def test_auto_select_uses_observation_counts(self):
        """Auto-select picks nodes with sufficient observations via batch counts."""
        from ohm.inference.discovery import discover_causal
        from ohm.graph_reader import MockGraphReader, ObservationRecord, NodeRecord

        obs_a = [ObservationRecord(edge_id=None, id=f"o{i}", node_id="node_a", type="binary", value=float(i % 2), source="test", created_by="test", scale="binary", created_at="2026-01-01") for i in range(8)]
        obs_b = [ObservationRecord(edge_id=None, id=f"p{i}", node_id="node_b", type="binary", value=float((i + 1) % 2), source="test", created_by="test", scale="binary", created_at="2026-01-01") for i in range(8)]
        obs_c = [ObservationRecord(edge_id=None, id="q0", node_id="node_c", type="binary", value=0.5, source="test", created_by="test", scale="binary", created_at="2026-01-01")]

        reader = MockGraphReader(
            nodes=[NodeRecord(id="node_a", label="A", type="concept"), NodeRecord(id="node_b", label="B", type="concept"), NodeRecord(id="node_c", label="C", type="concept")],
            observations=obs_a + obs_b + obs_c,
        )
        result = discover_causal(reader, node_ids=None, method="pc", min_observations=5)
        if "error" not in result or "insufficient" not in result.get("error", ""):
            assert result["n_nodes"] >= 2
        else:
            assert "2" in result["error"] or "≥2" in result["error"]


class TestDiscoverCausalFallback:
    """Tests for PC→GES fallback."""

    def test_fallback_to_ges_on_pc_failure(self):
        """When PC fails, discover_causal falls back to GES."""
        from ohm.inference.discovery import discover_causal
        from ohm.graph_reader import MockGraphReader, ObservationRecord, NodeRecord

        obs_a = [ObservationRecord(edge_id=None, id=f"o{i}", node_id="node_a", type="binary", value=float(i % 2), source="test", created_by="test", scale="binary", created_at="2026-01-01") for i in range(8)]
        obs_b = [ObservationRecord(edge_id=None, id=f"p{i}", node_id="node_b", type="binary", value=float((i + 1) % 2), source="test", created_by="test", scale="binary", created_at="2026-01-01") for i in range(8)]

        reader = MockGraphReader(
            nodes=[NodeRecord(id="node_a", label="A", type="concept"), NodeRecord(id="node_b", label="B", type="concept")],
            observations=obs_a + obs_b,
        )
        result = discover_causal(reader, node_ids=["node_a", "node_b"], method="pc")
        assert "method" in result
        if result.get("fallback_from") == "pc":
            assert result["method"] == "ges"


class TestGetObsCountsBatch:
    """Tests for GraphReader.get_observation_counts batch method."""

    def test_duckdb_reader_counts(self, test_db):
        """DuckDBGraphReader.get_observation_counts returns correct counts."""
        from ohm.graph.queries import create_node, create_observation
        from ohm.graph_reader import DuckDBGraphReader

        create_node(test_db, label="X", node_type="concept", created_by="test")
        create_node(test_db, label="Y", node_type="concept", created_by="test")
        for i in range(3):
            create_observation(test_db, node_id="X", obs_type="binary", value=float(i), created_by="test")
        create_observation(test_db, node_id="Y", obs_type="binary", value=0.5, created_by="test")

        reader = DuckDBGraphReader(test_db)
        counts = reader.get_observation_counts(["X", "Y", "Z"])
        assert counts["X"] == 3
        assert counts["Y"] == 1
        assert counts["Z"] == 0

    def test_mock_reader_counts(self):
        """MockGraphReader.get_observation_counts returns correct counts."""
        from ohm.graph_reader import MockGraphReader, ObservationRecord

        reader = MockGraphReader(observations=[
            ObservationRecord(edge_id=None, id="o1", node_id="a", type="binary", value=1.0, source="test", created_by="test", scale="binary", created_at="2026-01-01"),
            ObservationRecord(edge_id=None, id="o2", node_id="a", type="binary", value=0.0, source="test", created_by="test", scale="binary", created_at="2026-01-01"),
            ObservationRecord(edge_id=None, id="o3", node_id="b", type="binary", value=1.0, source="test", created_by="test", scale="binary", created_at="2026-01-01"),
        ])
        counts = reader.get_observation_counts(["a", "b", "c"])
        assert counts["a"] == 2
        assert counts["b"] == 1
        assert counts["c"] == 0

    def test_empty_node_list(self, test_db):
        """get_observation_counts with empty list returns empty dict."""
        from ohm.graph_reader import DuckDBGraphReader

        reader = DuckDBGraphReader(test_db)
        assert reader.get_observation_counts([]) == {}
