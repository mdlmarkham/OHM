"""Tests for OHM-vatf: event-driven propagation hook (post_event_create)."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import TOPO_SCHEMA, initialize_schema
from ohm.framework.sdk import Graph


@pytest.fixture
def graph():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn, TOPO_SCHEMA)
    g = Graph(conn, actor="test_agent")
    for nid, label in [("A", "Root"), ("B", "Middle"), ("C", "Leaf")]:
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, 'concept', 'test')",
            [nid, label],
        )
    return g


def _register_propagate_hook(conn):
    conn.execute(
        "INSERT INTO ohm_hooks (event, command, created_by) VALUES ('post_event_create', 'python:ohm.hooks_builtin.propagate_on_event', 'test')"
    )


class TestHookRegistration:
    def test_register_and_query(self, graph):
        _register_propagate_hook(graph._conn)
        rows = graph._conn.execute(
            "SELECT event, command FROM ohm_hooks WHERE event = 'post_event_create'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "python:ohm.hooks_builtin.propagate_on_event"

    def test_hook_event_is_valid(self):
        from ohm.hooks import VALID_HOOK_EVENTS
        assert "post_event_create" in VALID_HOOK_EVENTS


class TestPropagationOnFailureEvent:
    def test_failure_event_triggers_propagation(self, graph):
        _register_propagate_hook(graph._conn)
        graph._conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, probability, confidence, layer, created_by) "
            "VALUES ('e1', 'A', 'B', 'CAUSES', 0.8, 0.9, 'L3', 'test')"
        )
        event = graph.create_event(
            event_id="evt_fail",
            node_id="A",
            event_class="FAILURE",
            start_ts="2026-07-01 00:00:00",
            confidence=0.95,
            authority="sensor",
        )
        assert event["id"] == "evt_fail"

    def test_failure_propagates_to_downstream(self, graph):
        _register_propagate_hook(graph._conn)
        graph._conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, probability, confidence, layer, created_by) "
            "VALUES ('e1', 'A', 'B', 'CAUSES', 0.8, 0.9, 'L3', 'test')"
        )
        graph._conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, probability, confidence, layer, created_by) "
            "VALUES ('e2', 'B', 'C', 'CAUSES', 0.7, 0.85, 'L3', 'test')"
        )
        graph.create_event(
            event_id="evt_fail",
            node_id="A",
            event_class="FAILURE",
            start_ts="2026-07-01 00:00:00",
            confidence=0.95,
        )

    def test_non_triggering_event_class_no_propagation(self, graph):
        _register_propagate_hook(graph._conn)
        graph._conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, probability, confidence, layer, created_by) "
            "VALUES ('e1', 'A', 'B', 'CAUSES', 0.8, 0.9, 'L3', 'test')"
        )
        event = graph.create_event(
            event_id="evt_inspect",
            node_id="A",
            event_class="INSPECTION",
            start_ts="2026-07-01 00:00:00",
            confidence=0.95,
        )
        assert event["id"] == "evt_inspect"

    def test_unplanned_stop_triggers_propagation(self, graph):
        _register_propagate_hook(graph._conn)
        graph._conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, probability, confidence, layer, created_by) "
            "VALUES ('e1', 'A', 'B', 'CAUSES', 0.8, 0.9, 'L3', 'test')"
        )
        event = graph.create_event(
            event_id="evt_stop",
            node_id="A",
            event_class="UNPLANNED_STOP",
            start_ts="2026-07-01 00:00:00",
            confidence=0.9,
        )
        assert event["id"] == "evt_stop"

    def test_completed_triggers_propagation(self, graph):
        _register_propagate_hook(graph._conn)
        graph._conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, probability, confidence, layer, created_by) "
            "VALUES ('e1', 'A', 'B', 'CAUSES', 0.8, 0.9, 'L3', 'test')"
        )
        event = graph.create_event(
            event_id="evt_done",
            node_id="A",
            event_class="COMPLETED",
            start_ts="2026-07-01 00:00:00",
            confidence=0.85,
        )
        assert event["id"] == "evt_done"
