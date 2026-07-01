"""Tests for OHM-f7tl twin construction engine."""

from __future__ import annotations

import pytest

from ohm.exceptions import NodeNotFoundError
from ohm.graph.queries import (
    assemble_twin_for_decision,
    create_node,
    create_twin_template,
)


def _make_decision(test_db, label: str = "Decision") -> str:
    n = create_node(test_db, label=label, node_type="decision", created_by="tester")
    return n["id"]


def _make_template(test_db, *, label: str, description: str, required_edges=None, target_label: str = "T") -> str:
    target = create_node(test_db, label=target_label, node_type="concept", created_by="tester")
    t = create_twin_template(
        test_db,
        label=label,
        target_node_id=target["id"],
        created_by="tester",
        description=description,
        required_edges=required_edges or [],
    )
    return t["id"]


class TestConstructionEngineSchema:
    def test_decision_node_type_exists(self, test_db):
        from ohm.graph.schema import VALID_NODE_TYPES

        assert "decision" in VALID_NODE_TYPES


class TestAssembleTwinForDecision:
    def test_creates_ad_hoc_when_no_templates(self, test_db):
        decision_id = _make_decision(test_db, label="Choose HVAC supplier")
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision_id,
            goal="minimize HVAC energy consumption",
            created_by="tester",
        )
        assert result["twin"]["type"] == "twin"
        assert result["template"] is None
        assert "reasoning" in result
        assert "ad-hoc" in result["reasoning"].lower()

    def test_picks_highest_relevance_template(self, test_db):
        decision_id = _make_decision(test_db, label="Reduce energy")
        _make_template(test_db, label="Generic", description="A general template")
        hvac_id = _make_template(
            test_db,
            label="HVAC Energy",
            description="HVAC energy optimization template",
            required_edges=["CAUSES"],
        )
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision_id,
            goal="minimize HVAC energy consumption",
            created_by="tester",
        )
        assert result["template"]["id"] == hvac_id

    def test_respects_preferred_template(self, test_db):
        decision_id = _make_decision(test_db, label="Reduce energy")
        _make_template(test_db, label="HVAC", description="hvac template")
        generic_id = _make_template(test_db, label="Generic", description="generic template")
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision_id,
            goal="anything",
            preferred_template_id=generic_id,
            created_by="tester",
        )
        assert result["template"]["id"] == generic_id

    def test_missing_decision_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            assemble_twin_for_decision(
                test_db,
                decision_node_id="nonexistent_decision_xyz",
                goal="anything",
                created_by="tester",
            )

    def test_missing_preferred_template_raises(self, test_db):
        decision_id = _make_decision(test_db)
        with pytest.raises(NodeNotFoundError):
            assemble_twin_for_decision(
                test_db,
                decision_node_id=decision_id,
                goal="x",
                preferred_template_id="nonexistent_template_xyz",
                created_by="tester",
            )

    def test_reasoning_is_readable(self, test_db):
        decision_id = _make_decision(test_db, label="Test Decision")
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision_id,
            goal="test goal",
            created_by="tester",
        )
        assert isinstance(result["reasoning"], str)
        assert len(result["reasoning"]) > 0

    def test_returns_ranking_dict(self, test_db):
        decision_id = _make_decision(test_db, label="D")
        _make_template(test_db, label="T1", description="d1")
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision_id,
            goal="test",
            created_by="tester",
        )
        assert "ranking" in result
        assert "template_candidates" in result["ranking"]
        assert "model_candidates" in result["ranking"]

    def test_twin_links_to_decision(self, test_db):
        decision_id = _make_decision(test_db, label="Linked Decision")
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision_id,
            goal="test",
            created_by="tester",
        )
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type IN ('EVALUATES', 'DECISION_DEPENDS_ON')
               AND deleted_at IS NULL""",
            [result["twin"]["id"]],
        ).fetchall()
        assert any(e[0] == decision_id for e in edges)


class TestConstructionEngineSDK:
    def test_sdk_assembles_twin(self, test_db):
        from ohm.framework.sdk import Graph

        decision_id = _make_decision(test_db, label="SDK Decision")
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.assemble_twin_for_decision(decision_id, "test goal")
            assert result["twin"]["type"] == "twin"
            assert "reasoning" in result
