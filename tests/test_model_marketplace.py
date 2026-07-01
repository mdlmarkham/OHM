"""Tests for OHM-75tw model marketplace."""

from __future__ import annotations

import json

import pytest

from ohm.exceptions import NodeNotFoundError, ValidationError
from ohm.graph.queries import (
    compare_models,
    create_node,
    evaluate_model,
    promote_model,
    register_model_candidate,
    register_twin,
)


def _make_twin(test_db, label: str = "Twin") -> str:
    target = create_node(test_db, label="Target", node_type="concept", created_by="tester")
    twin = register_twin(test_db, label=label, target_node_id=target["id"], created_by="tester")
    return twin["id"]


class TestModelMarketplaceSchema:
    def test_node_types_in_valid_set(self, test_db):
        from ohm.graph.schema import VALID_NODE_TYPES, LAYER_EDGE_TYPES

        assert "model_candidate" in VALID_NODE_TYPES
        assert "model_evaluation" in VALID_NODE_TYPES
        assert "COMPETES_WITH" in LAYER_EDGE_TYPES["L3"]
        assert "EVALUATED_BY" in LAYER_EDGE_TYPES["L3"]


class TestRegisterModelCandidate:
    def test_creates_candidate_node(self, test_db):
        twin_id = _make_twin(test_db)
        cand = register_model_candidate(
            test_db,
            label="Linear Model",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"lr": 0.01},
        )
        assert cand["type"] == "model_candidate"
        assert cand["label"] == "Linear Model"

    def test_creates_evaluates_edge_to_twin(self, test_db):
        twin_id = _make_twin(test_db)
        cand = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [cand["id"]],
        ).fetchall()
        assert any(e[0] == twin_id for e in edges)

    def test_creates_competes_with_edges(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(test_db, label="M1", twin_id=twin_id, created_by="tester")
        c2 = register_model_candidate(test_db, label="M2", twin_id=twin_id, created_by="tester")
        edges = test_db.execute(
            """SELECT from_node, to_node FROM ohm_edges
               WHERE edge_type = 'COMPETES_WITH' AND deleted_at IS NULL
               AND (from_node = ? OR from_node = ? OR to_node = ? OR to_node = ?)""",
            [c1["id"], c2["id"], c1["id"], c2["id"]],
        ).fetchall()
        assert len(edges) >= 2

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            register_model_candidate(
                test_db,
                label="X",
                twin_id="nonexistent_twin_xyz",
                created_by="tester",
            )


class TestEvaluateModel:
    def test_creates_evaluation_node(self, test_db):
        twin_id = _make_twin(test_db)
        cand = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        ev = evaluate_model(
            test_db,
            model_candidate_id=cand["id"],
            created_by="tester",
            metrics={"mae": 0.12, "rmse": 0.18},
        )
        assert ev["type"] == "model_evaluation"

    def test_stores_metrics_in_metadata(self, test_db):
        twin_id = _make_twin(test_db)
        cand = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        ev = evaluate_model(
            test_db,
            model_candidate_id=cand["id"],
            created_by="tester",
            metrics={"mae": 0.1, "rmse": 0.2, "accuracy": 0.9},
        )
        meta = ev.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["metrics"]["mae"] == 0.1
        assert "composite_score" in meta

    def test_creates_evaluated_by_edge(self, test_db):
        twin_id = _make_twin(test_db)
        cand = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        ev = evaluate_model(
            test_db,
            model_candidate_id=cand["id"],
            created_by="tester",
            metrics={"mae": 0.1},
        )
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATED_BY' AND deleted_at IS NULL""",
            [cand["id"]],
        ).fetchall()
        assert any(e[0] == ev["id"] for e in edges)

    def test_empty_metrics_raises(self, test_db):
        twin_id = _make_twin(test_db)
        cand = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        with pytest.raises(ValidationError):
            evaluate_model(
                test_db,
                model_candidate_id=cand["id"],
                created_by="tester",
                metrics={},
            )

    def test_missing_candidate_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            evaluate_model(
                test_db,
                model_candidate_id="nonexistent_candidate_xyz",
                created_by="tester",
                metrics={"mae": 0.1},
            )


class TestCompareModels:
    def test_returns_ranked_candidates(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(test_db, label="M1", twin_id=twin_id, created_by="tester")
        c2 = register_model_candidate(test_db, label="M2", twin_id=twin_id, created_by="tester")
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"mae": 0.05, "rmse": 0.1},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"mae": 0.5, "rmse": 0.8},
        )
        result = compare_models(test_db, twin_id=twin_id)
        assert result["twin_id"] == twin_id
        assert len(result["candidates"]) == 2
        assert result["candidates"][0]["composite_score"] >= result["candidates"][1]["composite_score"]

    def test_includes_recommendation(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(test_db, label="Best", twin_id=twin_id, created_by="tester")
        c2 = register_model_candidate(test_db, label="Worse", twin_id=twin_id, created_by="tester")
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"mae": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"mae": 0.99},
        )
        result = compare_models(test_db, twin_id=twin_id)
        assert result["recommendation"]["label"] == "Best"

    def test_empty_when_no_candidates(self, test_db):
        twin_id = _make_twin(test_db)
        result = compare_models(test_db, twin_id=twin_id)
        assert result["candidates"] == []
        assert result["recommendation"] is None

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            compare_models(test_db, twin_id="nonexistent_twin_xyz")


class TestPromoteModel:
    def test_sets_active_status(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(test_db, label="C1", twin_id=twin_id, created_by="tester")
        promoted = promote_model(test_db, model_candidate_id=c1["id"], created_by="tester")
        meta = promoted.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["gate_status"] == "active"

    def test_archives_others(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(test_db, label="C1", twin_id=twin_id, created_by="tester")
        c2 = register_model_candidate(test_db, label="C2", twin_id=twin_id, created_by="tester")
        promote_model(test_db, model_candidate_id=c1["id"], created_by="tester")
        c2_row = test_db.execute("SELECT metadata FROM ohm_nodes WHERE id = ?", [c2["id"]]).fetchone()
        meta = json.loads(c2_row[0]) if c2_row[0] else {}
        assert meta.get("gate_status") == "archived"

    def test_missing_candidate_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            promote_model(test_db, model_candidate_id="nonexistent_candidate_xyz", created_by="tester")


class TestModelMarketplaceSDK:
    def test_sdk_register_and_promote(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            cand = g.register_model_candidate("SDK Model", twin_id=twin_id, model_parameters={"lr": 0.01})
            assert cand["type"] == "model_candidate"
            g.evaluate_model(cand["id"], metrics={"mae": 0.05, "accuracy": 0.95})
            comparison = g.compare_models(twin_id)
            assert len(comparison["candidates"]) == 1
            promoted = g.promote_model(cand["id"])
            assert promoted is not None
