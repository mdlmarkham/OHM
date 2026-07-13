"""Tests for OHM prospect node type and SUPERSEDES edge type (OHM-840 / issue #840).

Verifies that prospect is a first-class node type, SUPERSEDES is an L4 edge type,
and prospect lifecycle statuses (proposed, committed, active, completed, failed,
partial, superseded) are accepted by the schema and validation layers.
"""

from __future__ import annotations

import pytest

from ohm.schema import (
    DEFAULT_SCHEMA,
    LAYER_EDGE_TYPES,
    SCHEMA_VERSION,
    VALID_NODE_TYPES,
    VALID_TASK_STATUSES,
    MIGRATIONS,
)
from ohm.queries import create_node, create_edge


class TestProspectSchema:
    """Schema-level invariants for the prospect node type."""

    def test_prospect_in_valid_node_types(self):
        assert "prospect" in VALID_NODE_TYPES

    def test_prospect_in_default_schema(self):
        assert "prospect" in DEFAULT_SCHEMA.node_types

    def test_supersedes_in_l4_edge_types(self):
        assert "SUPERSEDES" in LAYER_EDGE_TYPES["L4"]

    def test_supersedes_not_in_other_layers(self):
        for layer in ("L0", "L1", "L2", "L3"):
            assert "SUPERSEDES" not in LAYER_EDGE_TYPES.get(layer, frozenset())

    def test_schema_version_bumped(self):
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 51, 0)

    def test_migration_0_51_0_present(self):
        versions = [m[0] for m in MIGRATIONS]
        assert "0.51.0" in versions

    def test_migration_0_51_0_description(self):
        for ver, desc, _stmts in MIGRATIONS:
            if ver == "0.51.0":
                assert "prospect" in desc.lower()
                return
        pytest.fail("Migration 0.51.0 not found")

    def test_migration_0_51_0_is_noop(self):
        for ver, _desc, stmts in MIGRATIONS:
            if ver == "0.51.0":
                assert stmts == ["SELECT 1;"]
                return
        pytest.fail("Migration 0.51.0 not found")


class TestProspectLifecycleStatuses:
    """The extended task_status values for prospect lifecycle."""

    def test_original_statuses_preserved(self):
        for s in ("open", "in_progress", "blocked", "review", "done", "cancelled"):
            assert s in VALID_TASK_STATUSES

    def test_proposed_status(self):
        assert "proposed" in VALID_TASK_STATUSES

    def test_committed_status(self):
        assert "committed" in VALID_TASK_STATUSES

    def test_active_status(self):
        assert "active" in VALID_TASK_STATUSES

    def test_completed_status(self):
        assert "completed" in VALID_TASK_STATUSES

    def test_failed_status(self):
        assert "failed" in VALID_TASK_STATUSES

    def test_partial_status(self):
        assert "partial" in VALID_TASK_STATUSES

    def test_superseded_status(self):
        assert "superseded" in VALID_TASK_STATUSES

    def test_validate_task_status_accepts_new_values(self):
        from ohm.framework.validation import validate_task_status

        for s in ("proposed", "committed", "active", "completed", "failed", "partial", "superseded"):
            assert validate_task_status(s) == s

    def test_validate_task_status_rejects_unknown(self):
        from ohm.framework.validation import validate_task_status

        with pytest.raises(ValueError, match="Invalid task_status"):
            validate_task_status("definitely_not_real")


class TestProspectNodeCreation:
    """End-to-end: create prospect nodes in an in-memory DB."""

    def test_create_prospect_node(self, test_db):
        node = create_node(
            test_db,
            label="Q3-2026 Campaign",
            node_type="prospect",
            content="Cement campaign at RCC plant",
            created_by="test_agent",
        )
        assert node["type"] == "prospect"

    def test_create_prospect_with_lifecycle_status(self, test_db):
        node = create_node(
            test_db,
            label="Proposed Campaign",
            node_type="prospect",
            content="Awaiting approval",
            created_by="test_agent",
        )
        test_db.execute(
            "UPDATE ohm_nodes SET task_status = 'proposed' WHERE id = ?",
            [node["id"]],
        )
        row = test_db.execute(
            "SELECT task_status FROM ohm_nodes WHERE id = ?", [node["id"]]
        ).fetchone()
        assert row[0] == "proposed"

    def test_create_prospect_with_committed_status(self, test_db):
        node = create_node(
            test_db,
            label="Committed Campaign",
            node_type="prospect",
            content="Approved for execution",
            created_by="test_agent",
        )
        test_db.execute(
            "UPDATE ohm_nodes SET task_status = 'committed' WHERE id = ?",
            [node["id"]],
        )
        row = test_db.execute(
            "SELECT task_status FROM ohm_nodes WHERE id = ?", [node["id"]]
        ).fetchone()
        assert row[0] == "committed"


class TestSupersedesEdge:
    """End-to-end: create SUPERSEDES edges between prospect nodes."""

    def test_create_supersedes_edge(self, test_db):
        old = create_node(
            test_db,
            label="Old Campaign",
            node_type="prospect",
            content="Previous plan",
            created_by="test_agent",
        )
        test_db.execute(
            "UPDATE ohm_nodes SET task_status = 'superseded' WHERE id = ?",
            [old["id"]],
        )
        new = create_node(
            test_db,
            label="New Campaign",
            node_type="prospect",
            content="Revised plan",
            created_by="test_agent",
        )
        test_db.execute(
            "UPDATE ohm_nodes SET task_status = 'committed' WHERE id = ?",
            [new["id"]],
        )
        edge = create_edge(
            test_db,
            from_node=new["id"],
            to_node=old["id"],
            edge_type="SUPERSEDES",
            layer="L4",
            created_by="test_agent",
            confidence=0.9,
        )
        assert edge["edge_type"] == "SUPERSEDES"
        assert edge["layer"] == "L4"

    def test_supersedes_wrong_layer_rejected(self, test_db):
        old = create_node(
            test_db,
            label="Old Plan",
            node_type="prospect",
            content="Old",
            created_by="test_agent",
        )
        new = create_node(
            test_db,
            label="New Plan",
            node_type="prospect",
            content="New",
            created_by="test_agent",
        )
        with pytest.raises(Exception):
            create_edge(
                test_db,
                from_node=new["id"],
                to_node=old["id"],
                edge_type="SUPERSEDES",
                layer="L3",
                created_by="test_agent",
            )


class TestAnalysisGuide:
    """The /schema endpoint exposes an ANALYSIS_GUIDE entry for prospect."""

    def test_prospect_has_analysis_guide(self):
        from ohm.graph.schema import ANALYSIS_GUIDE

        assert "prospect" in ANALYSIS_GUIDE

    def test_prospect_guide_use_for(self):
        from ohm.graph.schema import ANALYSIS_GUIDE

        guide = ANALYSIS_GUIDE["prospect"]
        use_for = list(guide["use_for"])  # type: ignore[arg-type]
        assert any("campaign" in u for u in use_for)

    def test_prospect_guide_provenance_rules(self):
        from ohm.graph.schema import ANALYSIS_GUIDE

        guide = ANALYSIS_GUIDE["prospect"]
        assert "provenance_rules" in guide
        # Raw dict value is typed as object; runtime is list[str]
        provenance = list(guide["provenance_rules"])  # type: ignore[arg-type]
        rules_text = " ".join(provenance)
        assert "SUPERSEDES" in rules_text
        assert "assessment" in rules_text
