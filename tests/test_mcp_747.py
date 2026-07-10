"""Tests for OHM-747: MCP batch tool, null-stripping, nudge dedup, onboarding surfaces."""

from __future__ import annotations

import pytest

from ohm.mcp.config import WRITE_TOOLS
from ohm.mcp.dispatch import build_request
from ohm.mcp.tools import all_tools


class TestOhmBatchTool:
    """Test ohm_batch MCP tool definition and dispatch."""

    def test_ohm_batch_in_tool_list(self):
        """ohm_batch appears in all_tools()."""
        tools = all_tools()
        names = [t.name for t in tools]
        assert "ohm_batch" in names

    def test_ohm_batch_in_write_tools(self):
        """ohm_batch is in WRITE_TOOLS so read-only profiles can't use it."""
        assert "ohm_batch" in WRITE_TOOLS

    def test_ohm_batch_schema_has_nodes_and_edges(self):
        """ohm_batch inputSchema has nodes and edges arrays."""
        tool = next(t for t in all_tools() if t.name == "ohm_batch")
        props = tool.inputSchema["properties"]
        assert "nodes" in props
        assert "edges" in props
        assert props["nodes"]["type"] == "array"
        assert props["edges"]["type"] == "array"

    def test_ohm_batch_dispatch_builds_request(self):
        """build_request returns POST /batch with nodes and edges."""
        method, path, body = build_request(
            "ohm_batch",
            {
                "nodes": [{"id": "n1", "label": "Node 1"}],
                "edges": [{"from": "n1", "to": "n1", "type": "CAUSES"}],
            },
            "test-agent",
        )
        assert method == "POST"
        assert path == "/batch"
        assert body is not None
        assert "nodes" in body
        assert "edges" in body
        assert len(body["nodes"]) == 1
        assert len(body["edges"]) == 1

    def test_ohm_batch_dispatch_empty_arrays(self):
        """build_request handles empty nodes/edges."""
        method, path, body = build_request("ohm_batch", {}, "test-agent")
        assert method == "POST"
        assert path == "/batch"
        assert body["nodes"] == []
        assert body["edges"] == []

    def test_ohm_batch_dispatch_rejects_over_50(self):
        """build_request raises when total items exceed 50."""
        nodes = [{"id": f"n{i}", "label": f"Node {i}"} for i in range(51)]
        with pytest.raises(ValueError, match="max 50"):
            build_request("ohm_batch", {"nodes": nodes}, "test-agent")


class TestNullStripping:
    """Test _strip_nulls in gateway.py."""

    def test_strip_nulls_removes_none_values(self):
        from ohm.mcp.gateway_helpers import _strip_nulls

        result = _strip_nulls({"a": 1, "b": None, "c": "hello"})
        assert result == {"a": 1, "c": "hello"}

    def test_strip_nulls_recursive_dicts(self):
        from ohm.mcp.gateway_helpers import _strip_nulls

        result = _strip_nulls({"a": {"x": 1, "y": None}, "b": None, "c": 3})
        assert result == {"a": {"x": 1}, "c": 3}

    def test_strip_nulls_lists(self):
        from ohm.mcp.gateway_helpers import _strip_nulls

        result = _strip_nulls([{"a": 1, "b": None}, {"c": None, "d": 2}])
        assert result == [{"a": 1}, {"d": 2}]

    def test_strip_nulls_nested_lists_in_dicts(self):
        from ohm.mcp.gateway_helpers import _strip_nulls

        result = _strip_nulls({"edges": [{"from": "a", "to": None}, {"from": "b"}]})
        assert result == {"edges": [{"from": "a"}, {"from": "b"}]}

    def test_strip_nulls_preserves_falsy_non_none(self):
        from ohm.mcp.gateway_helpers import _strip_nulls

        result = _strip_nulls({"a": 0, "b": False, "c": "", "d": None})
        assert result == {"a": 0, "b": False, "c": ""}

    def test_strip_nulls_passthrough_non_dict(self):
        from ohm.mcp.gateway_helpers import _strip_nulls

        assert _strip_nulls(42) == 42
        assert _strip_nulls("hello") == "hello"
        assert _strip_nulls(None) is None


class TestNudgeDedup:
    """Test _deduplicate_nudges in gateway_helpers.py."""

    def test_first_nudge_passes_through(self):
        from ohm.mcp.gateway_helpers import _deduplicate_nudges, _SESSION_NUDGES_SEEN

        _SESSION_NUDGES_SEEN.pop("session1", None)
        payload = {"nudges": [{"type": "batch_suggestion", "msg": "first"}]}
        result = _deduplicate_nudges("session1", payload)
        assert len(result["nudges"]) == 1

    def test_duplicate_nudge_stripped(self):
        from ohm.mcp.gateway_helpers import _deduplicate_nudges, _SESSION_NUDGES_SEEN

        _SESSION_NUDGES_SEEN.pop("session2", None)
        payload = {"nudges": [{"type": "batch_suggestion", "msg": "first"}]}
        _deduplicate_nudges("session2", payload)
        payload2 = {"nudges": [{"type": "batch_suggestion", "msg": "second"}]}
        result = _deduplicate_nudges("session2", payload2)
        assert len(result["nudges"]) == 0

    def test_different_types_not_deduped(self):
        from ohm.mcp.gateway_helpers import _deduplicate_nudges, _SESSION_NUDGES_SEEN

        _SESSION_NUDGES_SEEN.pop("session3", None)
        payload = {"nudges": [{"type": "batch_suggestion"}, {"type": "stale_edge"}]}
        result = _deduplicate_nudges("session3", payload)
        assert len(result["nudges"]) == 2

    def test_per_session_isolation(self):
        from ohm.mcp.gateway_helpers import _deduplicate_nudges, _SESSION_NUDGES_SEEN

        _SESSION_NUDGES_SEEN.pop("sess_a", None)
        _SESSION_NUDGES_SEEN.pop("sess_b", None)
        payload = {"nudges": [{"type": "batch_suggestion"}]}
        _deduplicate_nudges("sess_a", payload)
        result = _deduplicate_nudges("sess_b", payload)
        assert len(result["nudges"]) == 1

    def test_no_nudges_passthrough(self):
        from ohm.mcp.gateway_helpers import _deduplicate_nudges

        result = _deduplicate_nudges("sess", {"data": "other"})
        assert result == {"data": "other"}


class TestOnboardingNodeId:
    """Test SchemaConfig.onboarding_node_id field."""

    def test_default_onboarding_node_id_is_none(self):
        from ohm.graph.schema import SchemaConfig

        sc = SchemaConfig()
        assert sc.onboarding_node_id is None

    def test_onboarding_node_id_set(self):
        from ohm.graph.schema import SchemaConfig

        sc = SchemaConfig(onboarding_node_id="welcome_123")
        assert sc.onboarding_node_id == "welcome_123"

    def test_to_dict_includes_onboarding_node_id(self):
        from ohm.graph.schema import SchemaConfig

        sc = SchemaConfig(onboarding_node_id="onboard_1")
        d = sc.to_dict()
        assert d["onboarding_node_id"] == "onboard_1"

    def test_to_dict_omits_when_none(self):
        from ohm.graph.schema import SchemaConfig

        sc = SchemaConfig()
        d = sc.to_dict()
        assert "onboarding_node_id" not in d

    def test_from_dict_roundtrips(self):
        from ohm.graph.schema import SchemaConfig

        sc = SchemaConfig(onboarding_node_id="test_node")
        d = sc.to_dict()
        sc2 = SchemaConfig.from_dict(d)
        assert sc2.onboarding_node_id == "test_node"

    def test_from_dict_omits_when_absent(self):
        from ohm.graph.schema import SchemaConfig

        d = {
            "name": "ohm",
            "node_types": ["concept"],
            "layer_descriptions": {"L1": "test"},
            "observation_types": ["measurement"],
            "observation_sources": ["test"],
            "provenances": ["test"],
        }
        sc = SchemaConfig.from_dict(d)
        assert sc.onboarding_node_id is None
