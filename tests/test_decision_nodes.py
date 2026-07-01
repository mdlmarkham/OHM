"""Tests for OHM decision nodes + hypothesis status integration.

Verifies that decision nodes can be created with action alternatives and a
categorical utility_scale, linked to hypotheses via DECISION_DEPENDS_ON edges,
and re-evaluated when hypothesis statuses change via verification outcome or
decay. Also tests the GET /decision/{id}/recommendation HTTP endpoint.
"""

from __future__ import annotations

import json

import pytest

from ohm.schema import LAYER_EDGE_TYPES, DEFAULT_SCHEMA, SCHEMA_VERSION
from ohm.queries import create_node, create_edge, query_record_outcome
from ohm.decision import evaluate_decision, recompute_linked_decisions
from ohm.graph.methods import apply_verification_decay
from ohm.validation import validate_confidence


class TestDecisionSchema:
    """Schema-level invariants for decision node primitives."""

    def test_decision_node_type_exists(self):
        assert "decision" in DEFAULT_SCHEMA.node_types

    def test_decision_edge_type_in_l3(self):
        assert "DECISION_DEPENDS_ON" in LAYER_EDGE_TYPES["L3"]

    def test_decision_edge_type_other_layers_rejected(self):
        for layer in ("L0", "L1", "L2", "L4"):
            assert "DECISION_DEPENDS_ON" not in LAYER_EDGE_TYPES.get(layer, frozenset())

    def test_schema_version_bumped(self):
        assert SCHEMA_VERSION == "0.40.0"

    def test_migration_0_38_0_present(self):
        from ohm.schema import MIGRATIONS
        versions = [v for v, _, _ in MIGRATIONS]
        assert "0.38.0" in versions

    def test_migration_0_38_0_description(self):
        from ohm.schema import MIGRATIONS
        match = [m for m in MIGRATIONS if m[0] == "0.38.0"]
        assert match and "gate" in match[0][1].lower()

    def test_migration_0_37_0_present(self):
        from ohm.schema import MIGRATIONS
        versions = [v for v, _, _ in MIGRATIONS]
        assert "0.37.0" in versions

    def test_migration_0_37_0_description(self):
        from ohm.schema import MIGRATIONS
        match = [m for m in MIGRATIONS if m[0] == "0.37.0"]
        assert match and "feedback" in match[0][1].lower()

    def test_migration_0_36_0_present(self):
        from ohm.schema import MIGRATIONS
        versions = [v for v, _, _ in MIGRATIONS]
        assert "0.36.0" in versions

    def test_migration_0_36_0_description(self):
        from ohm.schema import MIGRATIONS
        match = [m for m in MIGRATIONS if m[0] == "0.36.0"]
        assert match and "outcome" in match[0][1].lower()


class TestDecisionNodeCreation:
    """Creating decision nodes with new fields."""

    def test_create_decision_node_with_best_utility(self, test_db):
        node = create_node(
            test_db,
            label="Launch Decision",
            node_type="decision",
            content="Decide whether to launch a new feature.",
            created_by="test_agent",
            utility_scale="best",
            current_best_action="launch_now",
            action_alternatives=["launch_now", "run_experiment", "do_nothing"],
        )
        assert node["type"] == "decision"
        assert node["utility_scale"] == 1.0
        assert node["current_best_action"] == "launch_now"
        assert json.loads(node["action_alternatives"]) == ["launch_now", "run_experiment", "do_nothing"]

    def test_create_decision_node_with_neutral_utility(self, test_db):
        node = create_node(
            test_db,
            label="Neutral Decision",
            node_type="decision",
            content="A decision of neutral importance.",
            created_by="test_agent",
            utility_scale="neutral",
            action_alternatives=["option_a", "option_b"],
        )
        assert node["utility_scale"] == 0.5

    def test_invalid_utility_scale_rejected(self, test_db):
        with pytest.raises(ValueError):
            create_node(
                test_db,
                label="Bad Decision",
                node_type="decision",
                content="A decision with a bad utility scale.",
                created_by="test_agent",
                utility_scale="gigantic",
            )

    def test_numeric_utility_scale_accepted(self, test_db):
        node = create_node(
            test_db,
            label="Numeric Decision",
            node_type="decision",
            content="A decision with old numeric utility scale.",
            created_by="test_agent",
            utility_scale=0.75,
        )
        assert node["utility_scale"] == 0.75


class TestDecisionDependsOnEdge:
    """DECISION_DEPENDS_ON edges connect decisions to hypotheses."""

    def test_create_decision_depends_on_edge(self, test_db):
        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept.",
            created_by="test_agent",
        )
        hypo = create_node(
            test_db,
            label="Customer Will Pay",
            node_type="hypothesis",
            content="Customers will pay for the premium tier.",
            created_by="test_agent",
            connects_to=[concept["id"]],
        )
        decision = create_node(
            test_db,
            label="Premium Tier Decision",
            node_type="decision",
            content="Decide whether to build the premium tier.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            current_best_action="build_now",
            action_alternatives=["build_now", "abandon"],
            utility_scale="best",
        )
        edge = create_edge(
            test_db,
            from_node=decision["id"],
            to_node=hypo["id"],
            layer="L3",
            edge_type="DECISION_DEPENDS_ON",
            created_by="test_agent",
            confidence=0.85,
        )
        assert edge["edge_type"] == "DECISION_DEPENDS_ON"
        assert edge["layer"] == "L3"


class TestDecisionRecommendation:
    """Recommendation logic for decision nodes."""

    @pytest.fixture
    def setup(self, test_db):
        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept for the decision.",
            created_by="test_agent",
        )
        hypo = create_node(
            test_db,
            label="Hypothesis A",
            node_type="hypothesis",
            content="A hypothesis the decision depends on.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            confidence=0.6,
        )
        test_db.execute(
            "UPDATE ohm_nodes SET hypothesis_status = 'tested' WHERE id = ?",
            [hypo["id"]],
        )
        decision = create_node(
            test_db,
            label="Decision A",
            node_type="decision",
            content="A decision linked to the hypothesis.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            current_best_action="status_quo",
            action_alternatives=["status_quo", "pivot_to_a"],
            utility_scale="neutral",
            confidence=0.8,
        )
        create_edge(
            test_db,
            from_node=decision["id"],
            to_node=hypo["id"],
            layer="L3",
            edge_type="DECISION_DEPENDS_ON",
            created_by="test_agent",
            confidence=0.85,
        )
        return {"concept": concept, "hypo": hypo, "decision": decision}

    def test_recommendation_returns_fields(self, test_db, setup):
        rec = evaluate_decision(test_db, setup["decision"]["id"])
        assert rec["decision_id"] == setup["decision"]["id"]
        assert rec["current_best_action"] == "status_quo"
        assert rec["action_alternatives"] == ["status_quo", "pivot_to_a"]
        assert 0.0 <= rec["confidence"] <= 1.0
        assert len(rec["key_assumptions"]) == 1
        assert rec["key_assumptions"][0]["id"] == setup["hypo"]["id"]
        assert rec["utility_scale"] == "neutral"

    def test_recommendation_confidence_drops_for_pruned(self, test_db, setup):
        test_db.execute(
            "UPDATE ohm_nodes SET hypothesis_status = 'pruned' WHERE id = ?",
            [setup["hypo"]["id"]],
        )
        rec = evaluate_decision(test_db, setup["decision"]["id"])
        assert rec["confidence"] == pytest.approx(0.0, abs=1e-4)

    def test_recommendation_confidence_rises_for_verified(self, test_db, setup):
        test_db.execute(
            "UPDATE ohm_nodes SET hypothesis_status = 'verified' WHERE id = ?",
            [setup["hypo"]["id"]],
        )
        rec = evaluate_decision(test_db, setup["decision"]["id"])
        assert rec["confidence"] == pytest.approx(0.8, abs=1e-4)


class TestHypothesisStatusUpdatesDecision:
    """Recording outcomes and decaying edges updates linked decisions."""

    @pytest.fixture
    def setup(self, test_db):
        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept for the hypothesis tree.",
            created_by="test_agent",
        )
        hypo = create_node(
            test_db,
            label="Feature Demand High",
            node_type="hypothesis",
            content="Demand for the new feature is high.",
            created_by="test_agent",
            connects_to=[concept["id"]],
        )
        test_db.execute(
            "UPDATE ohm_nodes SET hypothesis_status = 'tested' WHERE id = ?",
            [hypo["id"]],
        )
        experiment = create_node(
            test_db,
            label="Demand Survey",
            node_type="experiment",
            content="Survey to validate demand.",
            created_by="test_agent",
            connects_to=[hypo["id"]],
        )
        create_edge(
            test_db,
            from_node=experiment["id"],
            to_node=hypo["id"],
            layer="L3",
            edge_type="TESTS",
            created_by="test_agent",
            confidence=0.9,
        )
        decision = create_node(
            test_db,
            label="Build Feature",
            node_type="decision",
            content="Decide whether to build the feature.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            current_best_action="wait",
            action_alternatives=["wait", "build_feature", "kill_feature"],
            utility_scale="best",
        )
        create_edge(
            test_db,
            from_node=decision["id"],
            to_node=hypo["id"],
            layer="L3",
            edge_type="DECISION_DEPENDS_ON",
            created_by="test_agent",
            confidence=0.9,
        )
        return {
            "concept": concept,
            "hypo": hypo,
            "experiment": experiment,
            "decision": decision,
        }

    def test_positive_outcome_verifies_hypothesis_and_updates_decision(self, test_db, setup):
        result = query_record_outcome(
            test_db,
            source_agent="survey_agent",
            claim_node=setup["experiment"]["id"],
            outcome=True,
            recorded_by="test_agent",
        )
        statuses = {u["hypothesis_id"]: u["new_status"] for u in result.get("hypothesis_status_updates", [])}
        assert statuses[setup["hypo"]["id"]] == "verified"

        updated = test_db.execute(
            "SELECT current_best_action FROM ohm_nodes WHERE id = ?",
            [setup["decision"]["id"]],
        ).fetchone()[0]
        assert updated == "build_feature"

    def test_negative_outcome_prunes_hypothesis_and_updates_decision(self, test_db, setup):
        result = query_record_outcome(
            test_db,
            source_agent="survey_agent",
            claim_node=setup["experiment"]["id"],
            outcome=False,
            recorded_by="test_agent",
        )
        statuses = {u["hypothesis_id"]: u["new_status"] for u in result.get("hypothesis_status_updates", [])}
        assert statuses[setup["hypo"]["id"]] == "pruned"

        updated = test_db.execute(
            "SELECT current_best_action FROM ohm_nodes WHERE id = ?",
            [setup["decision"]["id"]],
        ).fetchone()[0]
        assert updated == "kill_feature"

    def test_decay_prunes_tested_hypothesis_and_updates_decision(self, test_db, setup):
        # Force the TESTS edge to be old and its confidence below the pruning threshold
        test_db.execute(
            "UPDATE ohm_edges SET confidence = 0.15, created_at = CURRENT_TIMESTAMP - INTERVAL '60 day' WHERE from_node = ?",
            [setup["experiment"]["id"]],
        )
        result = apply_verification_decay(test_db, dry_run=False)
        assert result.get("hypotheses_pruned", 0) >= 1

        updated = test_db.execute(
            "SELECT current_best_action FROM ohm_nodes WHERE id = ?",
            [setup["decision"]["id"]],
        ).fetchone()[0]
        assert updated == "kill_feature"


class TestHTTPDecisionRecommendation:
    """GET /decision/{id}/recommendation endpoint via test server."""

    def test_get_decision_recommendation(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        # Create a concept, hypothesis, and decision in the server DB
        concept = store.write_node(
            id="concept-demand",
            label="Demand Concept",
            type="concept",
            content="Concept for demand hypothesis.",
            agent_name="test_agent",
        )
        hypo = store.write_node(
            id="hypo-demand-high",
            label="Demand High",
            type="hypothesis",
            content="Demand is high.",
            agent_name="test_agent",
        )
        decision = store.write_node(
            id="decision-build",
            label="Build Feature",
            type="decision",
            content="Build the feature decision.",
            current_best_action="wait",
            action_alternatives=["wait", "build_feature"],
            utility_scale="best",
            agent_name="test_agent",
        )
        store.write_edge(
            from_node=hypo["id"],
            to_node=concept["id"],
            edge_type="REFINES",
            layer="L3",
            agent_name="test_agent",
        )
        store.write_edge(
            from_node=decision["id"],
            to_node=hypo["id"],
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            agent_name="test_agent",
        )

        status, data = _request("GET", port, f"/decision/{decision['id']}/recommendation")
        assert status == 200
        assert data["current_best_action"] == "wait"
        assert data["action_alternatives"] == ["wait", "build_feature"]
        assert "confidence" in data
        assert len(data["key_assumptions"]) == 1
        assert data["key_assumptions"][0]["id"] == hypo["id"]

    def test_get_decision_recommendation_not_found(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/decision/does-not-exist/recommendation")
        assert status == 404
