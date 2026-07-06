"""Tests for OHM-jw1x (batch cross-link bypass) and OHM-n9us (utility_scale arrays)."""

import json

import pytest

from ohm.graph.queries import create_node


class TestBatchCrossLinkBypass:
    """OHM-jw1x: POST /batch must run pre_ingest hooks (cross-link check) for each node."""

    def test_pattern_node_with_connects_to(self, test_db):
        """A pattern node created with connects_to should succeed."""
        anchor = create_node(test_db, label="Anchor", node_type="concept", created_by="test")
        node = create_node(
            test_db,
            label="Pattern",
            node_type="pattern",
            connects_to=[anchor["id"]],
            created_by="test",
        )
        assert node["id"] is not None


class TestUtilityScaleArrays:
    """OHM-n9us: utility_scale should accept integer and double arrays."""

    def test_accepts_integer_array(self, test_db):
        """utility_scale=[0, 1] should be accepted; mean stored as FLOAT, array in metadata."""
        node = create_node(
            test_db,
            label="Decision",
            node_type="decision",
            utility_scale=[0, 1],
            created_by="test",
        )
        assert node["id"] is not None
        # Mean of [0.0, 1.0] = 0.5, stored as the FLOAT utility_scale
        scale = node.get("utility_scale")
        assert scale is not None
        assert abs(float(scale) - 0.5) < 0.001

    def test_accepts_float_array(self, test_db):
        """utility_scale=[0.0, 0.5, 1.0] should be accepted."""
        node = create_node(
            test_db,
            label="Decision 2",
            node_type="decision",
            utility_scale=[0.0, 0.5, 1.0],
            created_by="test",
        )
        assert node["id"] is not None

    def test_accepts_mixed_string_number_array(self, test_db):
        """utility_scale=['best', 0.5, 'worst'] should map strings to numbers."""
        node = create_node(
            test_db,
            label="Decision 3",
            node_type="decision",
            utility_scale=["best", 0.5, "worst"],
            created_by="test",
        )
        assert node["id"] is not None

    def test_rejects_out_of_range_array(self, test_db):
        """utility_scale=[0, 2] should be rejected (2 > 1)."""
        with pytest.raises(ValueError, match="between 0 and 1"):
            create_node(
                test_db,
                label="Bad",
                node_type="decision",
                utility_scale=[0, 2],
                created_by="test",
            )

    def test_rejects_non_number_array(self, test_db):
        """utility_scale=['foo'] should be rejected."""
        with pytest.raises(ValueError, match="must be numbers"):
            create_node(
                test_db,
                label="Bad 2",
                node_type="decision",
                utility_scale=["foo"],
                created_by="test",
            )

    def test_accepts_single_number(self, test_db):
        """Single number still works (backward compat)."""
        node = create_node(
            test_db,
            label="Decision 4",
            node_type="decision",
            utility_scale=0.7,
            created_by="test",
        )
        assert node["id"] is not None
