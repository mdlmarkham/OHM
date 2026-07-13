"""Tests for OHM expectation node type and forecast observation type (OHM-841 / issue #841).

Verifies that expectation is a first-class node type for quantitative targets
attached to prospects, that forecast is a valid observation type, and that the
prospect -> expectation -> target node structure works end-to-end.
"""

from __future__ import annotations

import pytest

from ohm.schema import (
    DEFAULT_SCHEMA,
    SCHEMA_VERSION,
    VALID_NODE_TYPES,
    VALID_OBSERVATION_TYPES,
    MIGRATIONS,
)
from ohm.queries import create_node, create_edge


class TestExpectationSchema:
    """Schema-level invariants for the expectation node type."""

    def test_expectation_in_valid_node_types(self):
        assert "expectation" in VALID_NODE_TYPES

    def test_expectation_in_default_schema(self):
        assert "expectation" in DEFAULT_SCHEMA.node_types

    def test_forecast_in_valid_observation_types(self):
        assert "forecast" in VALID_OBSERVATION_TYPES

    def test_original_observation_types_preserved(self):
        for ot in ("anomaly", "measurement", "pattern", "challenge", "support",
                    "health_check", "experiment_result", "assessment"):
            assert ot in VALID_OBSERVATION_TYPES

    def test_schema_version_bumped(self):
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 52, 0)

    def test_migration_0_52_0_present(self):
        versions = [m[0] for m in MIGRATIONS]
        assert "0.52.0" in versions

    def test_migration_0_52_0_description(self):
        for ver, desc, _stmts in MIGRATIONS:
            if ver == "0.52.0":
                assert "expectation" in desc.lower()
                return
        pytest.fail("Migration 0.52.0 not found")

    def test_migration_0_52_0_is_noop(self):
        for ver, _desc, stmts in MIGRATIONS:
            if ver == "0.52.0":
                assert stmts == ["SELECT 1;"]
                return
        pytest.fail("Migration 0.52.0 not found")


class TestExpectationNodeCreation:
    """End-to-end: create expectation nodes in an in-memory DB."""

    def test_create_expectation_node(self, test_db):
        node = create_node(
            test_db,
            label="OEE Target",
            node_type="expectation",
            content="Kiln OEE should reach 85%",
            created_by="test_agent",
        )
        assert node["type"] == "expectation"

    def test_create_expectation_with_metadata(self, test_db):
        node = create_node(
            test_db,
            label="Throughput Target",
            node_type="expectation",
            content="Daily throughput target",
            created_by="test_agent",
            metadata={
                "expected_value": 5200.0,
                "unit": "ST/day",
                "expectation_type": "target",
                "criticality": "high",
                "p10": 4800.0,
                "p50": 5200.0,
                "p90": 5600.0,
            },
        )
        assert node["type"] == "expectation"
        assert node["metadata"] is not None


class TestProspectExpectationTargetStructure:
    """End-to-end: prospect -> CONTAINS -> expectation -> EXPECTS -> target node."""

    def test_full_structure(self, test_db):
        # Create a prospect
        prospect = create_node(
            test_db,
            label="Q3 Campaign",
            node_type="prospect",
            content="Third quarter production campaign",
            created_by="test_agent",
        )
        # Create a target node (concept that the expectation measures)
        target = create_node(
            test_db,
            label="Kiln OEE",
            node_type="concept",
            content="Overall equipment effectiveness of the kiln",
            created_by="test_agent",
        )
        # Create an expectation node
        expectation = create_node(
            test_db,
            label="OEE Target",
            node_type="expectation",
            content="Kiln OEE should reach 85%",
            created_by="test_agent",
            metadata={
                "expected_value": 0.85,
                "unit": "percent",
                "p10": 0.78,
                "p50": 0.85,
                "p90": 0.91,
                "criticality": "critical",
            },
        )
        # Link prospect -> expectation via CONTAINS (L1)
        contains_edge = create_edge(
            test_db,
            from_node=prospect["id"],
            to_node=expectation["id"],
            edge_type="CONTAINS",
            layer="L1",
            created_by="test_agent",
        )
        assert contains_edge["edge_type"] == "CONTAINS"
        assert contains_edge["layer"] == "L1"

        # Link expectation -> target via EXPECTS (L4)
        expects_edge = create_edge(
            test_db,
            from_node=expectation["id"],
            to_node=target["id"],
            edge_type="EXPECTS",
            layer="L4",
            created_by="test_agent",
        )
        assert expects_edge["edge_type"] == "EXPECTS"
        assert expects_edge["layer"] == "L4"

        # Verify the structure is queryable
        rows = test_db.execute(
            "SELECT e.type, ee.edge_type, ee.layer "
            "FROM ohm_edges ee "
            "JOIN ohm_nodes e ON ee.to_node = e.id "
            "WHERE ee.from_node = ? AND ee.edge_type = 'CONTAINS'",
            [prospect["id"]],
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "expectation"

    def test_expectation_connects_to_target(self, test_db):
        target = create_node(
            test_db,
            label="Revenue Target",
            node_type="concept",
            content="Quarterly revenue goal",
            created_by="test_agent",
        )
        expectation = create_node(
            test_db,
            label="Revenue Expectation",
            node_type="expectation",
            content="Revenue should exceed $10M",
            created_by="test_agent",
            metadata={
                "expected_value": 10_000_000.0,
                "unit": "USD",
                "expectation_type": "target",
                "p10": 8_000_000.0,
                "p50": 10_000_000.0,
                "p90": 12_000_000.0,
            },
        )
        edge = create_edge(
            test_db,
            from_node=expectation["id"],
            to_node=target["id"],
            edge_type="EXPECTS",
            layer="L4",
            created_by="test_agent",
        )
        assert edge["edge_type"] == "EXPECTS"
        assert edge["layer"] == "L4"


class TestAnalysisGuide:
    """The /schema endpoint exposes an ANALYSIS_GUIDE entry for expectation."""

    def test_expectation_has_analysis_guide(self):
        from ohm.graph.schema import ANALYSIS_GUIDE

        assert "expectation" in ANALYSIS_GUIDE

    def test_expectation_guide_use_for(self):
        from ohm.graph.schema import ANALYSIS_GUIDE

        guide = ANALYSIS_GUIDE["expectation"]
        use_for = list(guide["use_for"])  # type: ignore[arg-type]
        assert any("target" in u for u in use_for)

    def test_expectation_guide_provenance_rules(self):
        from ohm.graph.schema import ANALYSIS_GUIDE

        guide = ANALYSIS_GUIDE["expectation"]
        provenance = list(guide["provenance_rules"])  # type: ignore[arg-type]
        rules_text = " ".join(provenance)
        assert "measurement" in rules_text
        assert "EXPECTS" in rules_text
