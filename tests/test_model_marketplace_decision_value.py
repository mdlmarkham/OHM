"""Tests for OHM-75tw decision-value promotion policy."""

from __future__ import annotations

import json

import pytest

from ohm.exceptions import NodeNotFoundError, ValidationError
from ohm.graph.queries import (
    auto_promote_best_model,
    compute_decision_value,
    create_node,
    evaluate_model,
    promote_model,
    register_model_candidate,
    register_twin,
    set_promotion_policy,
)


def _make_twin(test_db, label: str = "Twin") -> str:
    target = create_node(test_db, label="Target", node_type="concept", created_by="tester")
    twin = register_twin(test_db, label=label, target_node_id=target["id"], created_by="tester")
    return twin["id"]


def _make_decision_node(test_db, label: str = "Decision") -> str:
    node = create_node(test_db, label=label, node_type="decision", created_by="tester")
    return node["id"]


class TestPromoteWithDecisionValuePolicy:
    def test_promote_with_decision_value_uses_compute_decision_value(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="M1",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.1, "cost": 0.2},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.9},
        )
        promoted = promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        assert "promotion_decision_value" in promoted
        assert promoted["promotion_policy"] == "decision_value"

    def test_promote_decision_value_requires_decision_id(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="M1",
            twin_id=twin_id,
            created_by="tester",
        )
        with pytest.raises(ValidationError, match="decision_node_id is required"):
            promote_model(
                test_db,
                model_candidate_id=c1["id"],
                created_by="tester",
                policy="decision_value",
            )

    def test_promote_decision_value_below_active_raises(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Active",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.95},
        )
        promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        c2 = register_model_candidate(
            test_db,
            label="Worse",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.5, "cost": 0.5},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"accuracy": 0.3},
        )
        with pytest.raises(ValidationError, match="does not exceed"):
            promote_model(
                test_db,
                model_candidate_id=c2["id"],
                created_by="tester",
                policy="decision_value",
                decision_node_id=decision_id,
                min_improvement=0.0,
            )

    def test_promote_decision_value_above_active_succeeds(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Active",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.5, "cost": 0.5},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.3},
        )
        promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        c2 = register_model_candidate(
            test_db,
            label="Better",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"accuracy": 0.95},
        )
        promoted = promote_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        meta = promoted.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["gate_status"] == "active"
        assert promoted["previous_active_id"] == c1["id"]

    def test_promote_default_policy_remains_accuracy(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="C1",
            twin_id=twin_id,
            created_by="tester",
        )
        promoted = promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
        )
        assert promoted["promotion_policy"] == "accuracy"

    def test_promote_invalid_policy_raises(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="C1",
            twin_id=twin_id,
            created_by="tester",
        )
        with pytest.raises(ValidationError, match="Invalid policy"):
            promote_model(
                test_db,
                model_candidate_id=c1["id"],
                created_by="tester",
                policy="invalid_policy",
            )

    def test_promote_decision_value_no_active_promotes_freely(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="First",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.1, "cost": 0.1},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.5},
        )
        promoted = promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        meta = promoted.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["gate_status"] == "active"
        assert "previous_active_id" not in promoted or promoted.get("previous_active_id") is None

    def test_promotion_policy_persisted_across_promotion(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="C1",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.9},
        )
        promoted = promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        meta = promoted.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["promotion_policy"] == "decision_value"
        assert "promotion_decision_value" in meta


class TestSetPromotionPolicy:
    def test_set_promotion_policy_stores_in_metadata(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="C1",
            twin_id=twin_id,
            created_by="tester",
        )
        decision_id = _make_decision_node(test_db)
        result = set_promotion_policy(
            test_db,
            model_candidate_id=c1["id"],
            policy="decision_value",
            decision_node_id=decision_id,
            min_improvement=0.05,
            created_by="tester",
        )
        meta = result.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["promotion_policy"] == "decision_value"
        assert meta["decision_node_id"] == decision_id
        assert meta["min_improvement"] == 0.05

    def test_set_promotion_policy_accuracy(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="C1",
            twin_id=twin_id,
            created_by="tester",
        )
        result = set_promotion_policy(
            test_db,
            model_candidate_id=c1["id"],
            policy="accuracy",
            created_by="tester",
        )
        meta = result.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["promotion_policy"] == "accuracy"

    def test_set_promotion_policy_invalid_raises(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="C1",
            twin_id=twin_id,
            created_by="tester",
        )
        with pytest.raises(ValidationError, match="Invalid policy"):
            set_promotion_policy(
                test_db,
                model_candidate_id=c1["id"],
                policy="bogus",
                created_by="tester",
            )

    def test_set_promotion_policy_decision_value_without_node_raises(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="C1",
            twin_id=twin_id,
            created_by="tester",
        )
        with pytest.raises(ValidationError, match="decision_node_id is required"):
            set_promotion_policy(
                test_db,
                model_candidate_id=c1["id"],
                policy="decision_value",
                created_by="tester",
            )

    def test_set_promotion_policy_missing_candidate_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            set_promotion_policy(
                test_db,
                model_candidate_id="nonexistent_xyz",
                policy="accuracy",
                created_by="tester",
            )


class TestAutoPromoteBestModel:
    def test_auto_promote_picks_highest_decision_value(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Low",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.5, "cost": 0.5},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.3},
        )
        c2 = register_model_candidate(
            test_db,
            label="High",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"accuracy": 0.95},
        )
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            decision_node_id=decision_id,
            policy="decision_value",
            created_by="tester",
        )
        assert result["promoted"] is not None
        meta = result["promoted"].get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["gate_status"] == "active"
        assert result["promoted"]["id"] == c2["id"]

    def test_auto_promote_respects_min_improvement(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Active",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.9},
        )
        promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        c2 = register_model_candidate(
            test_db,
            label="SlightlyBetter",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"accuracy": 0.91},
        )
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            decision_node_id=decision_id,
            policy="decision_value",
            min_improvement=1.0,
            created_by="tester",
        )
        assert result["promoted"] is None

    def test_auto_promote_no_candidates_returns_empty(self, test_db):
        twin_id = _make_twin(test_db)
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            policy="decision_value",
            created_by="tester",
        )
        assert result["promoted"] is None
        assert result["ranking"] == []

    def test_auto_promote_missing_twin_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            auto_promote_best_model(
                test_db,
                twin_id="nonexistent_twin_xyz",
                policy="decision_value",
                created_by="tester",
            )

    def test_auto_promote_accuracy_policy(self, test_db):
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Low",
            twin_id=twin_id,
            created_by="tester",
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"mae": 0.5, "rmse": 0.8},
        )
        c2 = register_model_candidate(
            test_db,
            label="High",
            twin_id=twin_id,
            created_by="tester",
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"mae": 0.01, "rmse": 0.02, "accuracy": 0.99},
        )
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            policy="accuracy",
            created_by="tester",
        )
        assert result["promoted"] is not None
        assert result["promoted"]["id"] == c2["id"]


class TestPromoteModelSDK:
    def test_sdk_promote_with_decision_value(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            c1 = g.register_model_candidate("SDK Model", twin_id=twin_id)
            g.evaluate_model(c1["id"], metrics={"accuracy": 0.9})
            promoted = g.promote_model(
                c1["id"],
                policy="decision_value",
                decision_node_id=decision_id,
            )
            assert promoted["promotion_policy"] == "decision_value"

    def test_sdk_set_promotion_policy(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            c1 = g.register_model_candidate("SDK Model", twin_id=twin_id)
            result = g.set_promotion_policy(
                c1["id"],
                policy="decision_value",
                decision_node_id=decision_id,
                min_improvement=0.1,
            )
            meta = result.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            assert meta["promotion_policy"] == "decision_value"

    def test_sdk_auto_promote(self, test_db):
        from ohm.framework.sdk import Graph

        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            c1 = g.register_model_candidate("Low", twin_id=twin_id)
            g.evaluate_model(c1["id"], metrics={"accuracy": 0.3})
            c2 = g.register_model_candidate("High", twin_id=twin_id)
            g.evaluate_model(c2["id"], metrics={"accuracy": 0.95})
            result = g.auto_promote_best_model(
                twin_id,
                decision_node_id=decision_id,
                policy="decision_value",
            )
            assert result["promoted"] is not None


class TestAutoPromoteReasonField:
    """OHM-341t: auto_promote_best_model surfaces a reason field for every
    outcome. Previously, a failed min_improvement was indistinguishable from
    no-candidates — both returned `{"promoted": None, "ranking": []}`.
    Now each branch returns a structured `reason` and `detail`."""

    def test_no_candidates_reason(self, test_db):
        twin_id = _make_twin(test_db)
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            policy="decision_value",
            created_by="tester",
        )
        assert result["promoted"] is None
        assert result["reason"] == "no_candidates"
        assert "no model candidates" in result["detail"].lower()
        assert result["ranking"] == []

    def test_below_min_improvement_reason(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Active",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.9},
        )
        promote_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            policy="decision_value",
            decision_node_id=decision_id,
        )
        c2 = register_model_candidate(
            test_db,
            label="SlightlyBetter",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c2["id"],
            created_by="tester",
            metrics={"accuracy": 0.91},
        )
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            decision_node_id=decision_id,
            policy="decision_value",
            min_improvement=1.0,
            created_by="tester",
        )
        assert result["promoted"] is None
        assert result["reason"] == "below_min_improvement"
        # Detail carries the original promote_model ValidationError message
        assert "min_improvement" in result["detail"]
        # Ranking should still include both candidates for caller inspection
        assert len(result["ranking"]) == 2
        assert result["best_candidate"]["label"] == "SlightlyBetter"

    def test_promoted_reason(self, test_db):
        twin_id = _make_twin(test_db)
        decision_id = _make_decision_node(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Winner",
            twin_id=twin_id,
            created_by="tester",
            model_parameters={"latency": 0.01, "cost": 0.01},
        )
        evaluate_model(
            test_db,
            model_candidate_id=c1["id"],
            created_by="tester",
            metrics={"accuracy": 0.9},
        )
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            decision_node_id=decision_id,
            policy="decision_value",
            created_by="tester",
        )
        assert result["promoted"] is not None
        assert result["reason"] == "promoted"
        assert "winner" in result["detail"].lower()
        assert "0.9" in result["detail"]
        assert result["best_candidate"]["label"] == "Winner"

    def test_no_score_reason_when_no_evaluation(self, test_db):
        """If the only candidate has no evaluation, reason is no_score."""
        twin_id = _make_twin(test_db)
        c1 = register_model_candidate(
            test_db,
            label="Unscored",
            twin_id=twin_id,
            created_by="tester",
        )
        # No evaluate_model call — candidate has no composite_score
        result = auto_promote_best_model(
            test_db,
            twin_id=twin_id,
            policy="decision_value",
            created_by="tester",
        )
        assert result["promoted"] is None
        assert result["reason"] == "no_score"
        assert result["best_candidate"]["label"] == "Unscored"
        # Detail explains why we couldn't score
        assert "no scorable evaluation" in result["detail"].lower() or "no evaluation" in result["detail"].lower()
