"""Tests for feedback-graph node and edge types (OHM-iuoz)."""

from __future__ import annotations

import pytest

from ohm.schema import (
    VALID_NODE_TYPES,
    LAYER_EDGE_TYPES,
    ALL_EDGE_TYPES,
    MUST_HAVE_EDGE_NODE_TYPES,
    SCHEMA_VERSION,
    MIGRATIONS,
    VALID_GATE_TYPES,
    VALID_GATE_STATUSES,
)


class TestFeedbackGraphNodeTypes:
    """Verify feedback-graph node types are registered."""

    def test_scenario_node_type_exists(self):
        assert "scenario" in VALID_NODE_TYPES

    def test_action_node_type_exists(self):
        assert "action" in VALID_NODE_TYPES

    def test_intervention_node_type_exists(self):
        assert "intervention" in VALID_NODE_TYPES


class TestFeedbackGraphEdgeTypes:
    """Verify feedback-graph edge types are registered in the right layers."""

    def test_counterfactual_of_in_l3(self):
        assert "COUNTERFACTUAL_OF" in LAYER_EDGE_TYPES["L3"]

    def test_proposes_action_in_l3(self):
        assert "PROPOSES_ACTION" in LAYER_EDGE_TYPES["L3"]

    def test_evaluates_in_l3(self):
        assert "EVALUATES" in LAYER_EDGE_TYPES["L3"]

    def test_proposed_by_in_l4(self):
        assert "PROPOSED_BY" in LAYER_EDGE_TYPES["L4"]

    def test_executed_by_in_l4(self):
        assert "EXECUTED_BY" in LAYER_EDGE_TYPES["L4"]

    def test_feedback_to_in_l4(self):
        assert "FEEDBACK_TO" in LAYER_EDGE_TYPES["L4"]

    def test_intervenes_on_in_l4(self):
        assert "INTERVENES_ON" in LAYER_EDGE_TYPES["L4"]

    def test_all_new_edges_in_all_edge_types(self):
        for et in ("COUNTERFACTUAL_OF", "PROPOSES_ACTION", "EVALUATES",
                   "PROPOSED_BY", "EXECUTED_BY", "FEEDBACK_TO", "INTERVENES_ON"):
            assert et in ALL_EDGE_TYPES


class TestCrossLinkRequirement:
    """Verify feedback-graph types require cross-links (ADR-018)."""

    def test_scenario_requires_cross_link(self):
        assert "scenario" in MUST_HAVE_EDGE_NODE_TYPES

    def test_action_requires_cross_link(self):
        assert "action" in MUST_HAVE_EDGE_NODE_TYPES

    def test_intervention_requires_cross_link(self):
        assert "intervention" in MUST_HAVE_EDGE_NODE_TYPES


class TestSchemaVersion:
    """Verify schema version bumped for the migration."""

    def test_version_is_0380(self):
        assert SCHEMA_VERSION == "0.38.0"

    def test_migration_0370_exists(self):
        versions = [m[0] for m in MIGRATIONS]
        assert "0.37.0" in versions

    def test_migration_0370_description(self):
        migration = next(m for m in MIGRATIONS if m[0] == "0.37.0")
        assert "feedback" in migration[1].lower()


class TestFeedbackGraphIntegration:
    """Integration tests — create nodes/edges of the new types."""

    def test_create_scenario_node(self, test_db):
        from ohm.queries import create_node, create_edge

        target = create_node(test_db, label="Target Concept", node_type="concept", created_by="metis")
        scenario = create_node(
            test_db, label="What if reliability drops?", node_type="scenario",
            created_by="metis", connects_to=[target["id"]],
        )
        assert scenario["type"] == "scenario"
        assert scenario["id"]

    def test_create_action_node(self, test_db):
        from ohm.queries import create_node

        scenario = create_node(test_db, label="Test Scenario", node_type="scenario", created_by="metis")
        action = create_node(
            test_db, label="Increase buffer stock", node_type="action",
            created_by="metis", connects_to=[scenario["id"]],
        )
        assert action["type"] == "action"

    def test_create_intervention_node(self, test_db):
        from ohm.queries import create_node

        target = create_node(test_db, label="Supplier", node_type="concept", created_by="metis")
        intervention = create_node(
            test_db, label="Force supplier to 0.9", node_type="intervention",
            created_by="metis", connects_to=[target["id"]],
        )
        assert intervention["type"] == "intervention"

    def test_create_counterfactual_of_edge(self, test_db):
        from ohm.queries import create_node, create_edge

        original = create_node(test_db, label="Original", node_type="concept", created_by="metis")
        scenario = create_node(test_db, label="CF Scenario", node_type="scenario", created_by="metis")
        edge = create_edge(test_db, from_node=scenario["id"], to_node=original["id"],
                           edge_type="COUNTERFACTUAL_OF", layer="L3", created_by="metis")
        assert edge["edge_type"] == "COUNTERFACTUAL_OF"

    def test_create_proposed_by_edge(self, test_db):
        from ohm.queries import create_node, create_edge

        scenario = create_node(test_db, label="Scenario", node_type="scenario", created_by="metis")
        action = create_node(test_db, label="Action", node_type="action", created_by="metis")
        edge = create_edge(test_db, from_node=action["id"], to_node=scenario["id"],
                           edge_type="PROPOSED_BY", layer="L4", created_by="metis")
        assert edge["edge_type"] == "PROPOSED_BY"

    def test_create_intervenes_on_edge(self, test_db):
        from ohm.queries import create_node, create_edge

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        intervention = create_node(test_db, label="Force State", node_type="intervention", created_by="metis")
        edge = create_edge(test_db, from_node=intervention["id"], to_node=target["id"],
                          edge_type="INTERVENES_ON", layer="L4", created_by="metis")
        assert edge["edge_type"] == "INTERVENES_ON"


# ── AND-gate governance (OHM-as17) ──────────────────────────────────────────


class TestGateGovernance:
    """Tests for gate_type, gate_status, and constraint_expr schema (OHM-as17)."""

    def test_valid_gate_types(self):
        assert "AND" in VALID_GATE_TYPES
        assert "OR" in VALID_GATE_TYPES

    def test_valid_gate_statuses(self):
        assert "intact" in VALID_GATE_STATUSES
        assert "converted" in VALID_GATE_STATUSES
        assert "compromised" in VALID_GATE_STATUSES
        assert "failed" in VALID_GATE_STATUSES

    def test_schema_version_0380(self):
        assert SCHEMA_VERSION == "0.38.0"

    def test_migration_0380_exists(self):
        versions = [m[0] for m in MIGRATIONS]
        assert "0.38.0" in versions

    def test_migration_0380_adds_columns(self):
        migration = next(m for m in MIGRATIONS if m[0] == "0.38.0")
        stmts = " ".join(migration[2])
        assert "gate_type" in stmts
        assert "gate_status" in stmts
        assert "constraint_expr" in stmts

    def test_gate_type_column_exists(self, test_db):
        cols = test_db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_nodes' AND column_name = 'gate_type'"
        ).fetchall()
        assert len(cols) == 1

    def test_gate_status_column_exists(self, test_db):
        cols = test_db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_nodes' AND column_name = 'gate_status'"
        ).fetchall()
        assert len(cols) == 1

    def test_constraint_expr_column_exists(self, test_db):
        cols = test_db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_edges' AND column_name = 'constraint_expr'"
        ).fetchall()
        assert len(cols) == 1

    def test_node_with_gate_type(self, test_db):
        from ohm.queries import create_node
        n = create_node(test_db, label="AND-gate Node", node_type="concept", created_by="metis")
        test_db.execute("UPDATE ohm_nodes SET gate_type = ?, gate_status = ? WHERE id = ?",
                        ["AND", "intact", n["id"]])
        row = test_db.execute("SELECT gate_type, gate_status FROM ohm_nodes WHERE id = ?", [n["id"]]).fetchone()
        assert row[0] == "AND"
        assert row[1] == "intact"

    def test_edge_with_constraint_expr(self, test_db):
        from ohm.queries import create_node, create_edge
        a = create_node(test_db, label="A", node_type="concept", created_by="metis")
        b = create_node(test_db, label="B", node_type="concept", created_by="metis")
        e = create_edge(test_db, from_node=a["id"], to_node=b["id"],
                        edge_type="CAUSES", layer="L3", created_by="metis")
        test_db.execute("UPDATE ohm_edges SET constraint_expr = ? WHERE id = ?",
                        ["A AND B AND C", e["id"]])
        row = test_db.execute("SELECT constraint_expr FROM ohm_edges WHERE id = ?", [e["id"]]).fetchone()
        assert row[0] == "A AND B AND C"