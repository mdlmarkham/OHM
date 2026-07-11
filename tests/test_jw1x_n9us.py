"""Tests for OHM-jw1x (batch cross-link bypass) and OHM-n9us (utility_scale arrays)."""

import json

import pytest

from ohm.graph.queries import create_node
from ohm.hooks_builtin import cross_link_check
from tests.conftest import _request


class TestBatchCrossLinkBypass:
    """OHM-jw1x: POST /batch must run pre_ingest hooks (cross-link check) for each node.

    ADR-018 option 2: a node is accepted if an edge in the same batch references
    it (the edge anchors the claim to graph structure). This was documented but
    not implemented — the hook never saw the batch's edges.
    """

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

    def test_cross_link_accepts_batch_edge_to_existing(self, test_db):
        """Pattern node + batch edge to an existing node passes cross_link_check."""
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
            ["anchor_be1", "Anchor", "concept", "test"],
        )
        exit_code, _stdout, stderr = cross_link_check(
            {
                "body": {"type": "pattern", "id": "pat_be1"},
                "batch_edges": [{"from": "pat_be1", "to": "anchor_be1", "type": "CAUSES"}],
                "batch_node_ids": {"pat_be1"},
                "__conn": test_db,
            }
        )
        assert exit_code == 0, stderr

    def test_cross_link_accepts_batch_edge_to_batch_node(self, test_db):
        """Pattern node + batch edge to a node also in the batch passes."""
        exit_code, _stdout, stderr = cross_link_check(
            {
                "body": {"type": "pattern", "id": "pat_be2"},
                "batch_edges": [{"from": "pat_be2", "to": "concept_be2", "type": "CAUSES"}],
                "batch_node_ids": {"pat_be2", "concept_be2"},
                "__conn": test_db,
            }
        )
        assert exit_code == 0, stderr

    def test_cross_link_rejects_bare_pattern_in_batch(self, test_db):
        """Pattern node with no edges and no connects_to still rejected."""
        exit_code, _stdout, stderr = cross_link_check(
            {
                "body": {"type": "pattern", "id": "pat_be3"},
                "batch_edges": [],
                "batch_node_ids": {"pat_be3"},
                "__conn": test_db,
            }
        )
        assert exit_code == 1
        assert "cross_link_required" in stderr

    def test_cross_link_accepts_decision_with_batch_edge(self, test_db):
        """Decision node + batch edge to an existing node passes."""
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
            ["anchor_be4", "Anchor", "concept", "test"],
        )
        exit_code, _stdout, stderr = cross_link_check(
            {
                "body": {"type": "decision", "id": "dec_be4"},
                "batch_edges": [{"from": "dec_be4", "to": "anchor_be4", "type": "CAUSES"}],
                "batch_node_ids": {"dec_be4"},
                "__conn": test_db,
            }
        )
        assert exit_code == 0, stderr

    def test_cross_link_rejects_batch_edge_to_nonexistent(self, test_db):
        """Pattern node + batch edge to a nonexistent node not in batch rejected."""
        exit_code, _stdout, stderr = cross_link_check(
            {
                "body": {"type": "pattern", "id": "pat_be5"},
                "batch_edges": [{"from": "pat_be5", "to": "ghost", "type": "CAUSES"}],
                "batch_node_ids": {"pat_be5"},
                "__conn": test_db,
            }
        )
        assert exit_code == 1
        assert "cross_link_required" in stderr

    def test_cross_link_accepts_edge_where_node_is_target(self, test_db):
        """Pattern node is the 'to' end of a batch edge (not just 'from')."""
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
            ["source_be6", "Source", "concept", "test"],
        )
        exit_code, _stdout, stderr = cross_link_check(
            {
                "body": {"type": "pattern", "id": "pat_be6"},
                "batch_edges": [{"from": "source_be6", "to": "pat_be6", "type": "CAUSES"}],
                "batch_node_ids": {"pat_be6"},
                "__conn": test_db,
            }
        )
        assert exit_code == 0, stderr


@pytest.mark.xdist_group("server")
class TestBatchCrossLinkHttp:
    """HTTP-level tests: POST /batch with MUST_HAVE_EDGE nodes and edges."""

    def test_batch_pattern_with_edge_succeeds(self, test_server):
        """POST /batch with a pattern node + edge to an existing node succeeds."""
        port, _ = test_server
        status, _data = _request(
            "POST", port, "/node",
            body={"id": "anchor_http1", "label": "Anchor", "type": "concept"},
        )
        assert status in (200, 201)
        status, data = _request(
            "POST", port, "/batch",
            body={
                "nodes": [{"id": "pat_http1", "label": "Pattern", "type": "pattern"}],
                "edges": [{"from": "pat_http1", "to": "anchor_http1", "type": "CAUSES", "layer": "L3"}],
            },
        )
        assert status == 201, data
        assert data["nodes_created"] == 1
        assert data["edges_created"] == 1

    def test_batch_decision_with_edge_succeeds(self, test_server):
        """POST /batch with a decision node + edge to an existing node succeeds."""
        port, _ = test_server
        status, _data = _request(
            "POST", port, "/node",
            body={"id": "anchor_http2", "label": "Anchor", "type": "concept"},
        )
        assert status in (200, 201)
        status, data = _request(
            "POST", port, "/batch",
            body={
                "nodes": [{"id": "dec_http2", "label": "Decision", "type": "decision"}],
                "edges": [{"from": "dec_http2", "to": "anchor_http2", "type": "CAUSES", "layer": "L3"}],
            },
        )
        assert status == 201, data
        assert data["nodes_created"] == 1
        assert data["edges_created"] == 1

    def test_batch_bare_pattern_rejected(self, test_server):
        """POST /batch with a bare pattern node (no edges) fails."""
        port, _ = test_server
        status, data = _request(
            "POST", port, "/batch",
            body={
                "nodes": [{"id": "pat_http3", "label": "Bare", "type": "pattern"}],
                "edges": [],
            },
        )
        assert status == 400, data
        assert "cross_link_required" in data.get("message", "")

    def test_batch_two_nodes_with_edge_succeeds(self, test_server):
        """POST /batch with a concept + pattern node and an edge between them succeeds."""
        port, _ = test_server
        status, data = _request(
            "POST", port, "/batch",
            body={
                "nodes": [
                    {"id": "concept_http4", "label": "Concept", "type": "concept"},
                    {"id": "pat_http4", "label": "Pattern", "type": "pattern"},
                ],
                "edges": [{"from": "pat_http4", "to": "concept_http4", "type": "CAUSES", "layer": "L3"}],
            },
        )
        assert status == 201, data
        assert data["nodes_created"] == 2
        assert data["edges_created"] == 1


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
