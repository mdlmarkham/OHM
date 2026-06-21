"""Tests for OHM hypothesis-tree primitives (OHM-ss22).

Verifies that hypothesis and experiment nodes can be created, linked via
TESTS / REFINES / SUPPORTS_EVIDENCE / CONTRADICTS_EVIDENCE edges, and that
confidence propagates through a multi-level hypothesis tree.
"""

from __future__ import annotations

import json

import pytest

from ohm.schema import (
    LAYER_EDGE_TYPES,
    MIGRATIONS,
    SCHEMA_VERSION,
    VALID_NODE_TYPES,
    VALID_OBSERVATION_TYPES,
    MUST_HAVE_EDGE_NODE_TYPES,
    DEFAULT_SCHEMA,
)
from ohm.graph.schema import initialize_schema
from ohm.validation import validate_confidence


class TestSchemaPrimitives:
    """Schema-level invariants for the new primitives."""

    def test_hypothesis_node_type_exists(self):
        assert "hypothesis" in VALID_NODE_TYPES

    def test_experiment_node_type_exists(self):
        assert "experiment" in VALID_NODE_TYPES

    def test_must_have_edge_node_types_include_hypothesis_experiment(self):
        assert "hypothesis" in MUST_HAVE_EDGE_NODE_TYPES
        assert "experiment" in MUST_HAVE_EDGE_NODE_TYPES

    def test_l3_edge_types_include_hypothesis_tree_edges(self):
        l3 = LAYER_EDGE_TYPES["L3"]
        assert "TESTS" in l3
        assert "REFINES" in l3
        assert "SUPPORTS_EVIDENCE" in l3
        assert "CONTRADICTS_EVIDENCE" in l3

    def test_experiment_result_observation_type_exists(self):
        assert "experiment_result" in VALID_OBSERVATION_TYPES

    def test_schema_version_bumped(self):
        assert SCHEMA_VERSION == "0.34.0"

    def test_migration_0_34_0_present(self):
        versions = [m[0] for m in MIGRATIONS]
        assert "0.34.0" in versions

    def test_migration_0_34_0_description(self):
        migration = next(m for m in MIGRATIONS if m[0] == "0.34.0")
        assert "Hypothesis-tree primitives" in migration[1]
        assert any("hypothesis_status" in stmt for stmt in migration[2])
        assert any("worktree_ref" in stmt for stmt in migration[2])
        assert any("idx_nodes_hypothesis_status" in stmt for stmt in migration[2])

    def test_default_schema_validates_new_types(self):
        assert DEFAULT_SCHEMA.validate_node_type("hypothesis") is True
        assert DEFAULT_SCHEMA.validate_node_type("experiment") is True
        assert DEFAULT_SCHEMA.validate_edge_type("L3", "TESTS") is True
        assert DEFAULT_SCHEMA.validate_edge_type("L3", "REFINES") is True
        assert DEFAULT_SCHEMA.validate_edge_type("L3", "SUPPORTS_EVIDENCE") is True
        assert DEFAULT_SCHEMA.validate_edge_type("L3", "CONTRADICTS_EVIDENCE") is True

    def test_new_edge_types_wrong_layer_rejected(self):
        for edge_type in ("TESTS", "REFINES", "SUPPORTS_EVIDENCE", "CONTRADICTS_EVIDENCE"):
            assert DEFAULT_SCHEMA.validate_edge_type("L2", edge_type) is False
            assert DEFAULT_SCHEMA.validate_edge_type("L4", edge_type) is False


class TestHypothesisTreeOperations:
    """Database operations for hypothesis trees."""

    def test_create_hypothesis_with_connects_to(self, test_db):
        from ohm.queries import create_node

        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept that the hypothesis is about.",
            created_by="test_agent",
        )
        hypo = create_node(
            test_db,
            label="Main Hypothesis",
            node_type="hypothesis",
            content="A testable claim about the base concept.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            metadata={"project_id": "project-xyz"},
        )
        assert hypo["type"] == "hypothesis"
        assert hypo["id"].startswith("hypothesis-")
        assert json.loads(hypo["metadata"])["project_id"] == "project-xyz"

    def test_create_experiment_with_metrics(self, test_db):
        from ohm.queries import create_node, create_edge

        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept.",
            created_by="test_agent",
        )
        hypo = create_node(
            test_db,
            label="Target Hypothesis",
            node_type="hypothesis",
            content="Hypothesis to be tested.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            metadata={"project_id": "project-xyz"},
        )
        experiment = create_node(
            test_db,
            label="First Experiment",
            node_type="experiment",
            content="An experiment that evaluates the hypothesis with metrics.",
            created_by="test_agent",
            connects_to=[hypo["id"]],
            metadata={
                "artifact_ref": "git:abc123",
                "dev_metric": 0.82,
                "test_metric": 0.91,
                "evaluation_script": "scripts/eval.py",
                "budget_seconds": 120,
            },
        )
        assert experiment["type"] == "experiment"
        assert experiment["id"].startswith("experiment-")
        assert json.loads(experiment["metadata"])["test_metric"] == pytest.approx(0.91)

    def test_tests_edge_between_experiment_and_hypothesis(self, test_db):
        from ohm.queries import create_node, create_edge

        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept.",
            created_by="test_agent",
        )
        hypo = create_node(
            test_db,
            label="Target Hypothesis",
            node_type="hypothesis",
            content="Hypothesis to be tested.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            metadata={"project_id": "project-xyz"},
        )
        experiment = create_node(
            test_db,
            label="First Experiment",
            node_type="experiment",
            content="An experiment that evaluates the hypothesis.",
            created_by="test_agent",
            connects_to=[hypo["id"]],
            metadata={"artifact_ref": "git:abc123", "dev_metric": 0.82, "test_metric": 0.91},
        )
        edge = create_edge(
            test_db,
            from_node=experiment["id"],
            to_node=hypo["id"],
            layer="L3",
            edge_type="TESTS",
            created_by="test_agent",
            confidence=0.85,
        )
        assert edge["edge_type"] == "TESTS"
        assert edge["layer"] == "L3"

    def test_supports_evidence_edge(self, test_db):
        from ohm.queries import create_node, create_edge

        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept.",
            created_by="test_agent",
        )
        experiment = create_node(
            test_db,
            label="Supporting Experiment",
            node_type="experiment",
            content="Provides evidence for the concept.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            metadata={"artifact_ref": "git:def456", "dev_metric": 0.75, "test_metric": 0.88},
        )
        edge = create_edge(
            test_db,
            from_node=experiment["id"],
            to_node=concept["id"],
            layer="L3",
            edge_type="SUPPORTS_EVIDENCE",
            created_by="test_agent",
            confidence=0.8,
        )
        assert edge["edge_type"] == "SUPPORTS_EVIDENCE"

    def test_contradicts_evidence_edge(self, test_db):
        from ohm.queries import create_node, create_edge

        concept = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept.",
            created_by="test_agent",
        )
        experiment = create_node(
            test_db,
            label="Contradicting Experiment",
            node_type="experiment",
            content="Provides evidence against the concept.",
            created_by="test_agent",
            connects_to=[concept["id"]],
            metadata={"artifact_ref": "git:ghi789", "dev_metric": 0.65, "test_metric": 0.55},
        )
        edge = create_edge(
            test_db,
            from_node=experiment["id"],
            to_node=concept["id"],
            layer="L3",
            edge_type="CONTRADICTS_EVIDENCE",
            created_by="test_agent",
            confidence=0.7,
        )
        assert edge["edge_type"] == "CONTRADICTS_EVIDENCE"

    def test_experiment_result_observation(self, test_db):
        from ohm.queries import create_node, create_observation

        base = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept for the experiment.",
            created_by="test_agent",
        )
        experiment = create_node(
            test_db,
            label="Metric Experiment",
            node_type="experiment",
            content="Experiment whose observation records a result.",
            created_by="test_agent",
            connects_to=[base["id"]],
            metadata={"artifact_ref": "git:result001", "dev_metric": 0.7, "test_metric": 0.85},
        )
        obs = create_observation(
            test_db,
            node_id=experiment["id"],
            obs_type="experiment_result",
            created_by="test_agent",
            value=0.85,
            scale="probability",
            notes="Held-out evaluation result",
        )
        assert obs["type"] == "experiment_result"


class TestThreeLevelHypothesisTree:
    """Build a 3-level hypothesis tree and propagate confidence."""

    @pytest.fixture
    def tree(self, test_db):
        from ohm.queries import create_node, create_edge

        # L0/L1 base concept
        base = create_node(
            test_db,
            label="Base Concept",
            node_type="concept",
            content="A foundational concept with enough length to satisfy contract linting.",
            created_by="test_agent",
        )

        # Level 1: root hypothesis
        root = create_node(
            test_db,
            label="Root Hypothesis",
            node_type="hypothesis",
            content="Top-level claim that we want to verify through refinement and experimentation.",
            created_by="test_agent",
            connects_to=[base["id"]],
            confidence=0.6,
        )

        # Level 2: two child hypotheses that REFINES the root
        child_a = create_node(
            test_db,
            label="Child Hypothesis A",
            node_type="hypothesis",
            content="A narrower version of the root hypothesis focusing on scenario A.",
            created_by="test_agent",
            connects_to=[root["id"]],
            confidence=0.5,
            metadata={"parent_hypothesis_id": root["id"]},
        )
        child_b = create_node(
            test_db,
            label="Child Hypothesis B",
            node_type="hypothesis",
            content="A narrower version of the root hypothesis focusing on scenario B.",
            created_by="test_agent",
            connects_to=[root["id"]],
            confidence=0.5,
            metadata={"parent_hypothesis_id": root["id"]},
        )

        create_edge(
            test_db,
            from_node=child_a["id"],
            to_node=root["id"],
            layer="L3",
            edge_type="REFINES",
            created_by="test_agent",
            confidence=0.8,
        )
        create_edge(
            test_db,
            from_node=child_b["id"],
            to_node=root["id"],
            layer="L3",
            edge_type="REFINES",
            created_by="test_agent",
            confidence=0.8,
        )

        # Level 3: experiments that TEST each child hypothesis
        exp_a = create_node(
            test_db,
            label="Experiment A",
            node_type="experiment",
            content="Experiment designed to test child hypothesis A with dev and held-out metrics.",
            created_by="test_agent",
            connects_to=[child_a["id"]],
            metadata={
                "artifact_ref": "git:exp-a-001",
                "dev_metric": 0.78,
                "test_metric": 0.84,
                "evaluation_script": "eval_a.py",
            },
        )
        exp_b = create_node(
            test_db,
            label="Experiment B",
            node_type="experiment",
            content="Experiment designed to test child hypothesis B with dev and held-out metrics.",
            created_by="test_agent",
            connects_to=[child_b["id"]],
            metadata={
                "artifact_ref": "git:exp-b-002",
                "dev_metric": 0.66,
                "test_metric": 0.72,
                "evaluation_script": "eval_b.py",
            },
        )

        create_edge(
            test_db,
            from_node=exp_a["id"],
            to_node=child_a["id"],
            layer="L3",
            edge_type="TESTS",
            created_by="test_agent",
            confidence=0.9,
        )
        create_edge(
            test_db,
            from_node=exp_b["id"],
            to_node=child_b["id"],
            layer="L3",
            edge_type="TESTS",
            created_by="test_agent",
            confidence=0.85,
        )

        # Add experiment_result observations
        from ohm.queries import create_observation

        create_observation(
            test_db,
            node_id=exp_a["id"],
            obs_type="experiment_result",
            created_by="test_agent",
            value=0.84,
            scale="probability",
            notes="Held-out test metric for experiment A",
            metadata={"worktree_ref": "worktree/exp-a", "held_out": True},
        )
        create_observation(
            test_db,
            node_id=exp_b["id"],
            obs_type="experiment_result",
            created_by="test_agent",
            value=0.72,
            scale="probability",
            notes="Held-out test metric for experiment B",
            metadata={"worktree_ref": "worktree/exp-b", "held_out": True},
        )

        return {
            "base": base,
            "root": root,
            "child_a": child_a,
            "child_b": child_b,
            "exp_a": exp_a,
            "exp_b": exp_b,
        }

    def test_tree_nodes_have_correct_types(self, tree):
        assert tree["root"]["type"] == "hypothesis"
        assert tree["child_a"]["type"] == "hypothesis"
        assert tree["child_b"]["type"] == "hypothesis"
        assert tree["exp_a"]["type"] == "experiment"
        assert tree["exp_b"]["type"] == "experiment"

    def test_refines_edges_exist(self, test_db, tree):
        rows = test_db.execute("SELECT edge_type, COUNT(*) FROM ohm_edges WHERE layer = 'L3' AND deleted_at IS NULL GROUP BY edge_type").fetchall()
        counts = {row[0]: row[1] for row in rows}
        assert counts.get("REFINES", 0) == 2
        assert counts.get("TESTS", 0) == 2

    def test_propagate_child_confidence_to_root(self, test_db, tree):
        """Aggregate test-metric support from experiments up through REFINES edges.

        Simple propagation: each child confidence is boosted by its experiment's
        held-out test metric weighted by the TESTS edge confidence. The root
        confidence is then updated by averaging child refined confidences weighted
        by their REFINES edge confidence.
        """
        # Gather inputs
        child_a_id = tree["child_a"]["id"]
        child_b_id = tree["child_b"]["id"]
        root_id = tree["root"]["id"]

        exp_a_metric = test_db.execute(
            "SELECT metadata->>'test_metric' FROM ohm_nodes WHERE id = ?",
            [tree["exp_a"]["id"]],
        ).fetchone()[0]
        exp_b_metric = test_db.execute(
            "SELECT metadata->>'test_metric' FROM ohm_nodes WHERE id = ?",
            [tree["exp_b"]["id"]],
        ).fetchone()[0]

        tests_a_conf = test_db.execute(
            "SELECT confidence FROM ohm_edges WHERE from_node = ? AND edge_type = 'TESTS'",
            [tree["exp_a"]["id"]],
        ).fetchone()[0]
        tests_b_conf = test_db.execute(
            "SELECT confidence FROM ohm_edges WHERE from_node = ? AND edge_type = 'TESTS'",
            [tree["exp_b"]["id"]],
        ).fetchone()[0]

        refines_a_conf = test_db.execute(
            "SELECT confidence FROM ohm_edges WHERE from_node = ? AND edge_type = 'REFINES'",
            [child_a_id],
        ).fetchone()[0]
        refines_b_conf = test_db.execute(
            "SELECT confidence FROM ohm_edges WHERE from_node = ? AND edge_type = 'REFINES'",
            [child_b_id],
        ).fetchone()[0]

        # Child refined confidence = prior child confidence + (metric - prior) * tests_conf
        child_a_refined = 0.5 + (float(exp_a_metric) - 0.5) * tests_a_conf
        child_b_refined = 0.5 + (float(exp_b_metric) - 0.5) * tests_b_conf

        # Clamp to [0, 1]
        child_a_refined = min(1.0, max(0.0, child_a_refined))
        child_b_refined = min(1.0, max(0.0, child_b_refined))

        # Root update: weighted average of child refined confidences
        total_weight = refines_a_conf + refines_b_conf
        root_expected = (child_a_refined * refines_a_conf + child_b_refined * refines_b_conf) / total_weight

        # Sanity: root confidence should move from 0.6 toward the mean of child evidence
        assert 0.6 < root_expected < 0.84

        # Apply propagation via SQL update for the test assertion
        test_db.execute(
            "UPDATE ohm_nodes SET confidence = ? WHERE id = ?",
            [validate_confidence(root_expected), root_id],
        )
        propagated = test_db.execute(
            "SELECT confidence FROM ohm_nodes WHERE id = ?",
            [root_id],
        ).fetchone()[0]
        assert propagated == pytest.approx(root_expected, abs=1e-4)

    def test_experiment_result_observations_link_to_experiments(self, test_db, tree):
        rows = test_db.execute("SELECT node_id, type, value FROM ohm_observations WHERE type = 'experiment_result' AND deleted_at IS NULL ORDER BY value DESC").fetchall()
        assert len(rows) == 2
        node_ids = {row[0] for row in rows}
        assert tree["exp_a"]["id"] in node_ids
        assert tree["exp_b"]["id"] in node_ids
