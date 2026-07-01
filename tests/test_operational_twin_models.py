"""Tests for OHM-bf45 operational twin model selection, drift detection, validation, ensemble methods."""

from __future__ import annotations

import json
import uuid

import pytest

from ohm.exceptions import NodeNotFoundError, ValidationError
from ohm.graph.queries import (
    auto_retire_model,
    compute_decision_value,
    create_node,
    detect_drift,
    ensemble_predict,
    evaluate_model,
    promote_model,
    register_model_candidate,
    register_shadow_model,
    register_twin,
    run_walk_forward_validation,
)


def _make_twin(test_db, label: str = "Twin") -> str:
    target = create_node(test_db, label="Target", node_type="concept", created_by="tester")
    twin = register_twin(test_db, label=label, target_node_id=target["id"], created_by="tester")
    return twin["id"]


def _make_active_model(test_db, twin_id: str, label: str = "ActiveModel") -> dict:
    cand = register_model_candidate(test_db, label=label, twin_id=twin_id, created_by="tester")
    promote_model(test_db, model_candidate_id=cand["id"], created_by="tester")
    return cand


def _insert_observation(test_db, *, node_id, value, baseline, created_at=None):
    obs_id = str(uuid.uuid4())
    test_db.execute(
        """INSERT INTO ohm_observations (id, node_id, type, value, baseline, source, created_by, scale, created_at)
           VALUES (?, ?, 'measurement', ?, ?, 'analysis', 'tester', 'probability', ?)""",
        [obs_id, node_id, value, baseline, created_at],
    )
    return obs_id


class TestSchemaAdditions:
    def test_new_node_types_in_valid_set(self, test_db):
        from ohm.graph.schema import VALID_NODE_TYPES

        assert "drift_event" in VALID_NODE_TYPES
        assert "validation_run" in VALID_NODE_TYPES
        assert "ensemble_vote" in VALID_NODE_TYPES

    def test_new_edge_types_in_l3(self, test_db):
        from ohm.graph.schema import LAYER_EDGE_TYPES

        assert "SHADOWS" in LAYER_EDGE_TYPES["L3"]
        assert "DRIFT_SIGNAL" in LAYER_EDGE_TYPES["L3"]


class TestRegisterShadowModel:
    def test_creates_shadow_node_and_edges(self, test_db):
        twin_id = _make_twin(test_db)
        active = _make_active_model(test_db, twin_id)
        shadow = register_shadow_model(
            test_db,
            twin_id=twin_id,
            label="Shadow Model",
            source_model_id=active["id"],
            created_by="tester",
        )
        assert shadow["type"] == "model_candidate"

        meta = shadow.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["gate_status"] == "shadow"

        shadows_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'SHADOWS' AND deleted_at IS NULL""",
            [shadow["id"]],
        ).fetchall()
        assert any(e[0] == active["id"] for e in shadows_edges)

        evaluates_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [shadow["id"]],
        ).fetchall()
        assert any(e[0] == twin_id for e in evaluates_edges)

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            register_shadow_model(
                test_db,
                twin_id="nonexistent_twin_xyz",
                label="Shadow",
                source_model_id="some_model",
                created_by="tester",
            )

    def test_missing_source_model_raises(self, test_db):
        twin_id = _make_twin(test_db)
        with pytest.raises(NodeNotFoundError):
            register_shadow_model(
                test_db,
                twin_id=twin_id,
                label="Shadow",
                source_model_id="nonexistent_model_xyz",
                created_by="tester",
            )


class TestDetectDrift:
    def test_returns_drift_score_and_type(self, test_db):
        twin_id = _make_twin(test_db)
        _make_active_model(test_db, twin_id)
        result = detect_drift(
            test_db,
            twin_id=twin_id,
            created_by="tester",
        )
        assert "drift_score" in result
        assert "drift_type" in result
        assert "twin_id" in result
        assert result["twin_id"] == twin_id

    def test_creates_event_when_threshold_exceeded(self, test_db):
        twin_id = _make_twin(test_db)
        _make_active_model(test_db, twin_id)

        for i in range(20):
            _insert_observation(
                test_db,
                node_id=twin_id,
                value=0.9,
                baseline=0.1,
                created_at=f"2025-01-{(i % 28) + 1:02d}T00:00:00Z",
            )

        result = detect_drift(
            test_db,
            twin_id=twin_id,
            residual_threshold=0.05,
            created_by="tester",
        )
        assert result["drift_score"] > 0.0
        assert result["drift_type"] == "residual"
        assert "drift_event_id" in result

        event = test_db.execute(
            "SELECT id, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [result["drift_event_id"]],
        ).fetchone()
        assert event is not None
        assert event[1] == "drift_event"

    def test_no_drift_event_when_below_threshold(self, test_db):
        twin_id = _make_twin(test_db)
        _make_active_model(test_db, twin_id)

        result = detect_drift(
            test_db,
            twin_id=twin_id,
            residual_threshold=10.0,
            created_by="tester",
        )
        assert result["drift_score"] == 0.0
        assert "drift_event_id" not in result

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            detect_drift(
                test_db,
                twin_id="nonexistent_twin_xyz",
                created_by="tester",
            )


class TestWalkForwardValidation:
    def test_creates_validation_run(self, test_db):
        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")

        for i in range(100):
            _insert_observation(
                test_db,
                node_id=twin_id,
                value=0.5 + i * 0.001,
                baseline=0.5,
                created_at=f"2025-01-{(i % 28) + 1:02d}T00:00:00Z",
            )

        result = run_walk_forward_validation(
            test_db,
            model_id=model["id"],
            n_splits=5,
            min_train_size=50,
            created_by="tester",
        )
        assert "validation_id" in result
        assert result["model_id"] == model["id"]
        assert len(result["per_split_metrics"]) > 0

        vrun = test_db.execute(
            "SELECT id, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [result["validation_id"]],
        ).fetchone()
        assert vrun is not None
        assert vrun[1] == "validation_run"

    def test_detects_overfitting(self, test_db):
        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")

        for i in range(200):
            baseline = 0.5
            value = baseline if i < 100 else baseline + 0.5
            _insert_observation(
                test_db,
                node_id=twin_id,
                value=value,
                baseline=baseline,
                created_at=f"2025-01-{(i % 28) + 1:02d}T00:00:00Z",
            )

        result = run_walk_forward_validation(
            test_db,
            model_id=model["id"],
            n_splits=5,
            min_train_size=50,
            created_by="tester",
        )
        assert "overfitting_detected" in result

    def test_missing_model_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            run_walk_forward_validation(
                test_db,
                model_id="nonexistent_model_xyz",
                n_splits=5,
                min_train_size=50,
                created_by="tester",
            )


class TestEnsemblePredict:
    def test_returns_weighted_votes(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(test_db, label="M1", twin_id=twin_id, created_by="tester")
        c2 = register_model_candidate(test_db, label="M2", twin_id=twin_id, created_by="tester")
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.9, "mae": 0.05},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"accuracy": 0.7, "mae": 0.15},
        )

        result = ensemble_predict(test_db, twin_id=twin_id)
        assert "weighted_prediction" in result
        assert "votes" in result
        assert "disagreement" in result
        assert len(result["votes"]) == 2
        assert result["candidate_count"] == 2

    def test_no_candidates_returns_empty(self, test_db):
        twin_id = _make_twin(test_db)
        result = ensemble_predict(test_db, twin_id=twin_id)
        assert result["votes"] == []
        assert result["candidate_count"] == 0
        assert result["weighted_prediction"] == 0.0

    def test_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            ensemble_predict(test_db, twin_id="nonexistent_twin_xyz")


class TestComputeDecisionValue:
    def test_computes_score(self, test_db):
        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        evaluate_model(
            test_db,
            model_candidate_id=model["id"],
            created_by="tester",
            metrics={"accuracy": 0.9, "mae": 0.05},
        )
        decision = create_node(test_db, label="Decision", node_type="decision", created_by="tester")

        result = compute_decision_value(
            test_db,
            model_id=model["id"],
            decision_node_id=decision["id"],
            utility_scale=1.0,
        )
        assert "decision_value_score" in result
        assert 0.0 <= result["decision_value_score"] <= 1.0
        assert result["model_id"] == model["id"]
        assert result["decision_node_id"] == decision["id"]

    def test_missing_model_raises(self, test_db):
        decision = create_node(test_db, label="D", node_type="decision", created_by="tester")
        with pytest.raises(NodeNotFoundError):
            compute_decision_value(
                test_db,
                model_id="nonexistent_model_xyz",
                decision_node_id=decision["id"],
                utility_scale=1.0,
            )

    def test_missing_decision_raises(self, test_db):
        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        with pytest.raises(NodeNotFoundError):
            compute_decision_value(
                test_db,
                model_id=model["id"],
                decision_node_id="nonexistent_decision_xyz",
                utility_scale=1.0,
            )


class TestAutoRetireModel:
    def test_sets_retired_status(self, test_db):
        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        result = auto_retire_model(
            test_db,
            model_id=model["id"],
            reason="excessive drift",
            created_by="tester",
        )
        meta = result.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["gate_status"] == "retired"
        assert meta["retirement_reason"] == "excessive drift"

    def test_missing_model_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            auto_retire_model(
                test_db,
                model_id="nonexistent_model_xyz",
                reason="test",
                created_by="tester",
            )


class TestOperationalTwinModelsSDK:
    def test_sdk_register_shadow(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        active = _make_active_model(test_db, twin_id)
        with Graph(test_db, actor="sdk-tester") as g:
            shadow = g.register_shadow_model(
                twin_id=twin_id,
                label="SDK Shadow",
                source_model_id=active["id"],
            )
            assert shadow["type"] == "model_candidate"

    def test_sdk_detect_drift(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        _make_active_model(test_db, twin_id)
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.detect_drift(twin_id)
            assert "drift_score" in result
            assert "drift_type" in result

    def test_sdk_walk_forward_validation(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.run_walk_forward_validation(model["id"])
            assert "validation_id" in result

    def test_sdk_ensemble_predict(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.ensemble_predict(twin_id)
            assert "votes" in result

    def test_sdk_compute_decision_value(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        decision = create_node(test_db, label="D", node_type="decision", created_by="tester")
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.compute_decision_value(model["id"], decision["id"], utility_scale=1.0)
            assert "decision_value_score" in result

    def test_sdk_auto_retire(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        model = register_model_candidate(test_db, label="M", twin_id=twin_id, created_by="tester")
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.auto_retire_model(model["id"], reason="drift exceeded")
            meta = result.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            assert meta["gate_status"] == "retired"
