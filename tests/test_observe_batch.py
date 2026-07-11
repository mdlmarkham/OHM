"""Tests for OHM-794: Batch + idempotent observations."""

from __future__ import annotations

import pytest

from ohm.graph.schema import initialize_schema, SCHEMA_VERSION
from ohm.graph.store import OhmStore


@pytest.fixture
def store(tmp_path):
    """Create an OhmStore with a fresh in-memory DB."""
    s = OhmStore(":memory:", agent_name="test-agent")
    initialize_schema(s.conn)
    # Create a test node
    s.write_node(id="test_node", label="Test Node", type="concept", agent_name="test-agent")
    return s


class TestIdempotencyKey:
    """Test idempotency_key on single observations."""

    def test_first_write_succeeds(self, store):
        result = store.write_observation(
            node_id="test_node",
            type="measurement",
            value=0.8,
            idempotency_key="run_1:check",
        )
        assert result is not None
        assert result["node_id"] == "test_node"

    def test_duplicate_key_returns_existing(self, store):
        # First write
        result1 = store.write_observation(
            node_id="test_node",
            type="measurement",
            value=0.8,
            idempotency_key="run_1:check",
        )
        # Second write with same key — should return existing, not insert
        result2 = store.write_observation(
            node_id="test_node",
            type="measurement",
            value=0.99,  # different value, but same key
            idempotency_key="run_1:check",
        )
        assert result2 is not None
        assert result2["id"] == result1["id"]

    def test_different_keys_create_separate(self, store):
        store.write_observation(
            node_id="test_node",
            type="measurement",
            value=0.8,
            idempotency_key="run_1:check",
        )
        store.write_observation(
            node_id="test_node",
            type="measurement",
            value=0.9,
            idempotency_key="run_2:check",
        )
        # Should have 2 observations
        rows = store.conn.execute("SELECT COUNT(*) FROM ohm_observations WHERE node_id = 'test_node' AND deleted_at IS NULL").fetchone()
        assert rows[0] == 2

    def test_no_key_always_inserts(self, store):
        store.write_observation(node_id="test_node", type="measurement", value=0.8)
        store.write_observation(node_id="test_node", type="measurement", value=0.9)
        rows = store.conn.execute("SELECT COUNT(*) FROM ohm_observations WHERE node_id = 'test_node' AND deleted_at IS NULL").fetchone()
        assert rows[0] == 2

    def test_none_key_always_inserts(self, store):
        store.write_observation(node_id="test_node", type="measurement", value=0.8, idempotency_key=None)
        store.write_observation(node_id="test_node", type="measurement", value=0.9, idempotency_key=None)
        rows = store.conn.execute("SELECT COUNT(*) FROM ohm_observations WHERE node_id = 'test_node' AND deleted_at IS NULL").fetchone()
        assert rows[0] == 2


class TestSchemaVersion:
    def test_schema_version_bumped(self):
        assert SCHEMA_VERSION == "0.50.0"


class TestMcpToolDispatch:
    """Test that ohm_observe_batch dispatch works."""

    def test_dispatch_ohm_observe_batch(self):
        from ohm.mcp.dispatch import build_request

        method, path, body = build_request(
            "ohm_observe_batch",
            {"observations": [{"node_id": "n1", "obs_type": "measurement", "value": 0.5}]},
            "test-agent",
        )
        assert method == "POST"
        assert path == "/observations"
        assert body is not None
        assert "observations" in body
        assert len(body["observations"]) == 1

    def test_dispatch_injects_default_source(self):
        from ohm.mcp.dispatch import build_request

        _, _, body = build_request(
            "ohm_observe_batch",
            {"observations": [{"node_id": "n1", "obs_type": "measurement", "value": 0.5}]},
            "test-agent",
        )
        assert body["observations"][0]["source"] == "test-agent"

    def test_dispatch_preserves_explicit_source(self):
        from ohm.mcp.dispatch import build_request

        _, _, body = build_request(
            "ohm_observe_batch",
            {"observations": [{"node_id": "n1", "obs_type": "measurement", "value": 0.5, "source": "ci-runner"}]},
            "test-agent",
        )
        assert body["observations"][0]["source"] == "ci-runner"

    def test_dispatch_rejects_too_many(self):
        from ohm.mcp.dispatch import build_request

        with pytest.raises(ValueError, match="max 1000"):
            build_request(
                "ohm_observe_batch",
                {"observations": [{"node_id": "n1", "obs_type": "m", "value": 0}] * 1001},
                "test-agent",
            )

    def test_dispatch_ohm_observe_passes_idempotency_key(self):
        from ohm.mcp.dispatch import build_request

        method, path, body = build_request(
            "ohm_observe",
            {"node_id": "n1", "obs_type": "measurement", "value": 0.5, "idempotency_key": "run_42:check"},
            "test-agent",
        )
        assert body["idempotency_key"] == "run_42:check"

    def test_dispatch_ohm_observe_omits_missing_idempotency_key(self):
        from ohm.mcp.dispatch import build_request

        _, _, body = build_request(
            "ohm_observe",
            {"node_id": "n1", "obs_type": "measurement", "value": 0.5},
            "test-agent",
        )
        assert "idempotency_key" not in body


class TestWriteToolsConfig:
    def test_ohm_observe_batch_is_write_tool(self):
        from ohm.mcp.config import WRITE_TOOLS

        assert "ohm_observe_batch" in WRITE_TOOLS

    def test_read_only_blocks_ohm_observe_batch(self):
        from ohm.mcp.config import is_tool_allowed

        assert is_tool_allowed("ohm_observe_batch", {"read_only": True, "allowed_tools": ["*"]}) is False
