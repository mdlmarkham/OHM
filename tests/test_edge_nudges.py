"""Tests for OHM-tsxk and OHM-bm5r: creation-time nudges for edge typing.

OHM-tsxk: When a pattern/concept node is linked to a decision/task/event
with a CAUSES edge, warn that REFINES or EXPLAINS is more appropriate.

OHM-bm5r: When a CAUSES/INFLUENCES/DEPENDS_ON edge is created without a
condition (mediating mechanism), warn the agent to provide one.
"""

from __future__ import annotations

import pytest

from ohm.server.nudges import generate_nudges


@pytest.fixture
def store_with_nodes(tmp_path):
    from ohm.graph.store import OhmStore

    store = OhmStore(db_path=str(tmp_path / "nudge_test.duckdb"), agent_name="test")
    store.write_node("pattern_1", "AND→OR Pattern", "pattern", agent_name="test")
    store.write_node("decision_1", "Switch to OR", "decision", agent_name="test")
    store.write_node("concept_1", "Governance", "concept", agent_name="test")
    store.write_node("event_1", "Election", "event", agent_name="test")
    store.write_node("task_1", "Refactor", "task", agent_name="test")
    store.write_node("source_1", "Paper", "source", agent_name="test")
    yield store
    store.close()


class TestPatternToCausalNudge:
    """OHM-tsxk: Warn when pattern→case uses CAUSES."""

    def test_pattern_to_decision_causes_triggers_warning(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="pattern_1",
            to_node_id="decision_1",
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        assert len(pattern_nudges) == 1
        assert "REFINES" in pattern_nudges[0]["message"]
        assert pattern_nudges[0]["severity"] == "warning"
        assert pattern_nudges[0]["data"]["from_type"] == "pattern"
        assert pattern_nudges[0]["data"]["to_type"] == "decision"

    def test_pattern_to_task_causes_triggers_warning(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="pattern_1",
            to_node_id="task_1",
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        assert len(pattern_nudges) == 1

    def test_pattern_to_event_causes_triggers_warning(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="pattern_1",
            to_node_id="event_1",
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        assert len(pattern_nudges) == 1

    def test_pattern_to_concept_causes_triggers_warning(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="pattern_1",
            to_node_id="concept_1",
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        assert len(pattern_nudges) == 1
        assert "EXPLAINS" in pattern_nudges[0]["message"]

    def test_concept_to_decision_causes_triggers_warning(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="concept_1",
            to_node_id="decision_1",
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        assert len(pattern_nudges) == 1

    def test_non_pattern_to_decision_no_warning(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="source_1",
            to_node_id="decision_1",
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        assert len(pattern_nudges) == 0

    def test_no_store_no_crash(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="pattern_1",
            to_node_id="decision_1",
            store=None,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        assert len(pattern_nudges) == 0


class TestMechanismGateNudge:
    """OHM-bm5r: Warn when causal edge has no mechanism."""

    def test_causes_without_condition_triggers_warning(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            condition=None,
        )
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(mech_nudges) == 1
        assert "mechanism" in mech_nudges[0]["message"].lower()
        assert mech_nudges[0]["severity"] == "warning"
        assert mech_nudges[0]["data"]["has_condition"] is False

    def test_influences_without_condition_triggers_warning(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="INFLUENCES",
            condition=None,
        )
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(mech_nudges) == 1

    def test_depends_on_without_condition_triggers_warning(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="DEPENDS_ON",
            condition=None,
        )
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(mech_nudges) == 1

    def test_causes_with_condition_no_warning(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            condition="via increased insulin resistance",
        )
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(mech_nudges) == 0

    def test_causes_with_mechanism_metadata_no_warning(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            condition=None,
            metadata={"mechanism": "price elasticity of demand"},
        )
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(mech_nudges) == 0

    def test_supports_without_condition_no_warning(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="SUPPORTS",
            condition=None,
        )
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(mech_nudges) == 0

    def test_refines_without_condition_no_warning(self):
        nudges = generate_nudges(
            action="edge",
            edge_type="REFINES",
            condition=None,
        )
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(mech_nudges) == 0


class TestNudgesCoexist:
    """Both nudges can fire simultaneously without conflict."""

    def test_pattern_to_causes_fires_both_nudges(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="pattern_1",
            to_node_id="decision_1",
            condition=None,
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(pattern_nudges) == 1
        assert len(mech_nudges) == 1

    def test_pattern_to_causes_with_condition_skips_mech(self, store_with_nodes):
        nudges = generate_nudges(
            action="edge",
            edge_type="CAUSES",
            from_node_id="pattern_1",
            to_node_id="decision_1",
            condition="via institutional pressure",
            store=store_with_nodes,
        )
        pattern_nudges = [n for n in nudges if n["type"] == "pattern_to_causal_warning"]
        mech_nudges = [n for n in nudges if n["type"] == "mechanism_gate"]
        assert len(pattern_nudges) == 1
        assert len(mech_nudges) == 0
