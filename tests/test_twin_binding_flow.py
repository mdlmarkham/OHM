"""Tests for OHM-f7tl P0-minimal binding flow."""

from __future__ import annotations

import pytest

from ohm.exceptions import NodeNotFoundError
from ohm.graph.queries import (
    add_twin_bindings,
    attach_twin_models,
    create_node,
    create_twin_template,
    get_twin_readiness,
    register_twin_with_bindings,
)
from ohm.graph.queries import register_model_candidate as register_model_candidate_q


def _make_target(test_db, label: str = "Target") -> str:
    return create_node(test_db, label=label, node_type="concept", created_by="tester")["id"]


def _make_decision(test_db, label: str = "Decision") -> str:
    return create_node(test_db, label=label, node_type="decision", created_by="tester")["id"]


def _make_feed(test_db, label: str = "Feed") -> str:
    return create_node(test_db, label=label, node_type="concept", created_by="tester")["id"]


def _make_model_candidate(test_db, twin_id: str, label: str = "Model") -> str:
    m = register_model_candidate_q(test_db, label=label, twin_id=twin_id, created_by="tester")
    return m["id"]


class TestRegisterTwinWithBindings:
    def test_creates_twin_with_target_only(self, test_db):
        target = _make_target(test_db)
        result = register_twin_with_bindings(test_db, label="Twin", target_node_id=target, created_by="tester")
        assert result["twin"]["type"] == "twin"
        assert result["target_node_id"] == target
        assert result["decision_bound"] is False
        assert result["feeds_bound"] == 0
        assert result["models_bound"] == 0

    def test_creates_twin_with_all_bindings(self, test_db):
        target = _make_target(test_db)
        decision = _make_decision(test_db)
        feed1 = _make_feed(test_db, "F1")
        feed2 = _make_feed(test_db, "F2")
        twin0 = create_node(test_db, label="Stub", node_type="twin", created_by="tester")["id"]
        model = _make_model_candidate(test_db, twin0, label="M1")

        result = register_twin_with_bindings(
            test_db,
            label="Twin",
            target_node_id=target,
            decision_node_id=decision,
            feed_node_ids=[feed1, feed2],
            model_candidate_ids=[model],
            created_by="tester",
        )
        assert result["decision_bound"] is True
        assert result["feeds_bound"] == 2
        assert result["models_bound"] == 1

    def test_creates_evaluates_edge_to_target(self, test_db):
        target = _make_target(test_db)
        result = register_twin_with_bindings(test_db, label="T", target_node_id=target, created_by="tester")
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [result["twin"]["id"]],
        ).fetchall()
        assert any(e[0] == target for e in edges)

    def test_creates_decision_depends_on_edge(self, test_db):
        target = _make_target(test_db)
        decision = _make_decision(test_db)
        result = register_twin_with_bindings(
            test_db,
            label="T",
            target_node_id=target,
            decision_node_id=decision,
            created_by="tester",
        )
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'DECISION_DEPENDS_ON' AND deleted_at IS NULL""",
            [result["twin"]["id"]],
        ).fetchall()
        assert any(e[0] == decision for e in edges)

    def test_creates_feeds_edges(self, test_db):
        target = _make_target(test_db)
        feed = _make_feed(test_db, "F1")
        result = register_twin_with_bindings(
            test_db,
            label="T",
            target_node_id=target,
            feed_node_ids=[feed],
            created_by="tester",
        )
        edges = test_db.execute(
            """SELECT from_node FROM ohm_edges
               WHERE to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NULL""",
            [result["twin"]["id"]],
        ).fetchall()
        assert any(e[0] == feed for e in edges)

    def test_creates_applies_to_edges(self, test_db):
        target = _make_target(test_db)
        twin0 = create_node(test_db, label="Stub", node_type="twin", created_by="tester")["id"]
        model = _make_model_candidate(test_db, twin0, "M1")
        result = register_twin_with_bindings(
            test_db,
            label="T",
            target_node_id=target,
            model_candidate_ids=[model],
            created_by="tester",
        )
        edges = test_db.execute(
            """SELECT from_node FROM ohm_edges
               WHERE to_node = ? AND edge_type = 'APPLIES_TO' AND deleted_at IS NULL""",
            [result["twin"]["id"]],
        ).fetchall()
        assert any(e[0] == model for e in edges)

    def test_missing_target_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            register_twin_with_bindings(
                test_db,
                label="T",
                target_node_id="nonexistent_target_xyz",
                created_by="tester",
            )

    def test_missing_decision_raises(self, test_db):
        target = _make_target(test_db)
        with pytest.raises(NodeNotFoundError):
            register_twin_with_bindings(
                test_db,
                label="T",
                target_node_id=target,
                decision_node_id="nonexistent_decision_xyz",
                created_by="tester",
            )

    def test_missing_feed_raises(self, test_db):
        target = _make_target(test_db)
        with pytest.raises(NodeNotFoundError):
            register_twin_with_bindings(
                test_db,
                label="T",
                target_node_id=target,
                feed_node_ids=["nonexistent_feed_xyz"],
                created_by="tester",
            )

    def test_missing_model_raises(self, test_db):
        target = _make_target(test_db)
        with pytest.raises(NodeNotFoundError):
            register_twin_with_bindings(
                test_db,
                label="T",
                target_node_id=target,
                model_candidate_ids=["nonexistent_model_xyz"],
                created_by="tester",
            )


class TestAddTwinBindings:
    def test_adds_feed(self, test_db):
        target = _make_target(test_db)
        result = register_twin_with_bindings(test_db, label="T", target_node_id=target, created_by="tester")
        feed = _make_feed(test_db, "F1")
        add_result = add_twin_bindings(test_db, twin_id=result["twin"]["id"], feed_node_ids=[feed], created_by="tester")
        assert feed in add_result["added"]

    def test_removes_feed(self, test_db):
        target = _make_target(test_db)
        feed = _make_feed(test_db, "F1")
        result = register_twin_with_bindings(
            test_db,
            label="T",
            target_node_id=target,
            feed_node_ids=[feed],
            created_by="tester",
        )
        rm_result = add_twin_bindings(
            test_db,
            twin_id=result["twin"]["id"],
            feed_node_ids_remove=[feed],
            created_by="tester",
        )
        assert feed in rm_result["removed"]
        edges = test_db.execute(
            """SELECT id FROM ohm_edges
               WHERE from_node = ? AND to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NOT NULL""",
            [feed, result["twin"]["id"]],
        ).fetchall()
        assert len(edges) >= 1

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            add_twin_bindings(
                test_db,
                twin_id="nonexistent_twin_xyz",
                feed_node_ids=["x"],
                created_by="tester",
            )


class TestAttachTwinModels:
    def test_attaches_model(self, test_db):
        target = _make_target(test_db)
        result = register_twin_with_bindings(test_db, label="T", target_node_id=target, created_by="tester")
        twin0 = create_node(test_db, label="Stub", node_type="twin", created_by="tester")["id"]
        model = _make_model_candidate(test_db, twin0, "M1")
        attach_result = attach_twin_models(test_db, twin_id=result["twin"]["id"], model_candidate_ids=[model], created_by="tester")
        assert model in attach_result["added"]

    def test_detaches_model(self, test_db):
        target = _make_target(test_db)
        twin0 = create_node(test_db, label="Stub", node_type="twin", created_by="tester")["id"]
        model = _make_model_candidate(test_db, twin0, "M1")
        result = register_twin_with_bindings(
            test_db,
            label="T",
            target_node_id=target,
            model_candidate_ids=[model],
            created_by="tester",
        )
        rm_result = attach_twin_models(
            test_db,
            twin_id=result["twin"]["id"],
            model_candidate_ids_remove=[model],
            created_by="tester",
        )
        assert model in rm_result["removed"]

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            attach_twin_models(
                test_db,
                twin_id="nonexistent_twin_xyz",
                model_candidate_ids=["x"],
                created_by="tester",
            )


class TestGetTwinReadiness:
    def test_all_gates_satisfied(self, test_db):
        target = _make_target(test_db)
        decision = _make_decision(test_db)
        feed = _make_feed(test_db, "F1")
        twin0 = create_node(test_db, label="Stub", node_type="twin", created_by="tester")["id"]
        model = _make_model_candidate(test_db, twin0, "M1")

        test_db.execute(
            "INSERT INTO ohm_observations (node_id, value, type, created_by, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [feed, 0.5, "test", "tester"],
        )

        result = register_twin_with_bindings(
            test_db,
            label="T",
            target_node_id=target,
            decision_node_id=decision,
            feed_node_ids=[feed],
            model_candidate_ids=[model],
            created_by="tester",
        )
        readiness = get_twin_readiness(test_db, twin_id=result["twin"]["id"])
        assert readiness["gates"]["target_bound"] is True
        assert readiness["gates"]["decision_bound"] is True
        assert readiness["gates"]["feeds_present"] is True
        assert readiness["gates"]["feeds_fresh"] is True
        assert readiness["gates"]["models_available"] is True

    def test_missing_decision(self, test_db):
        target = _make_target(test_db)
        result = register_twin_with_bindings(test_db, label="T", target_node_id=target, created_by="tester")
        readiness = get_twin_readiness(test_db, twin_id=result["twin"]["id"])
        assert readiness["gates"]["decision_bound"] is False
        assert "decision_bound" in readiness["missing"]

    def test_no_models(self, test_db):
        target = _make_target(test_db)
        result = register_twin_with_bindings(test_db, label="T", target_node_id=target, created_by="tester")
        readiness = get_twin_readiness(test_db, twin_id=result["twin"]["id"])
        assert readiness["gates"]["models_available"] is False

    def test_no_feeds(self, test_db):
        target = _make_target(test_db)
        result = register_twin_with_bindings(test_db, label="T", target_node_id=target, created_by="tester")
        readiness = get_twin_readiness(test_db, twin_id=result["twin"]["id"])
        assert readiness["gates"]["feeds_present"] is False

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            get_twin_readiness(test_db, twin_id="nonexistent_twin_xyz")


class TestTwinBindingFlowSDK:
    def test_sdk_register_with_bindings(self, test_db):
        from ohm.framework.sdk import Graph

        target = _make_target(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.register_twin_with_bindings("Twin", target_node_id=target)
            assert result["twin"]["type"] == "twin"
            readiness = g.get_twin_readiness(result["twin"]["id"])
            assert readiness["gates"]["target_bound"] is True
