"""Tests for OHM-dh9l.3: TOPO temporal DomainTable SDK methods."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import TOPO_SCHEMA, initialize_schema
from ohm.framework.sdk import Graph


@pytest.fixture
def graph():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn, TOPO_SCHEMA)
    return Graph(conn, actor="test_agent")


@pytest.fixture
def sample_plan(graph):
    return graph.create_plan(
        plan_id="plan_001",
        node_id="compressor_A",
        plan_type="maintenance_window",
        label="Compressor A Annual Maintenance",
        start_ts="2026-07-01 00:00:00",
        end_ts="2026-07-05 00:00:00",
        horizon="PLANNED",
        status="active",
    )


@pytest.fixture
def sample_event(graph, sample_plan):
    return graph.create_event(
        event_id="evt_shutdown",
        plan_id=sample_plan["id"],
        node_id="compressor_A",
        event_class="shutdown",
        title="Planned compressor shutdown",
        start_ts="2026-07-01 08:00:00",
        end_ts="2026-07-01 12:00:00",
        operating_state="stopped",
        description="Planned shutdown for annual maintenance",
        confidence=0.95,
        authority="engineer",
    )


class TestCreateAndGetPlan:
    def test_create_plan(self, graph):
        result = graph.create_plan(
            plan_id="p1",
            node_id="n1",
            plan_type="annual_outage",
            label="2026 outage",
            start_ts="2026-12-01",
            end_ts="2026-12-14",
            horizon="PLANNED",
            status="approved",
        )
        assert result["id"] == "p1"
        assert result["node_id"] == "n1"
        assert result["plan_type"] == "annual_outage"
        assert result["status"] == "approved"

    def test_create_plan_minimal(self, graph):
        result = graph.create_plan(plan_id="p2", plan_type="campaign")
        assert result["id"] == "p2"
        assert result["plan_type"] == "campaign"
        assert result["status"] == "active"

    def test_get_plan(self, graph, sample_plan):
        result = graph.get_plan("plan_001")
        assert result is not None
        assert result["id"] == "plan_001"
        assert result["plan_type"] == "maintenance_window"

    def test_get_plan_nonexistent(self, graph):
        result = graph.get_plan("nonexistent")
        assert result is None


class TestListPlans:
    def test_list_plans_empty(self, graph):
        plans = graph.list_plans()
        assert plans == []

    def test_list_plans_by_type(self, graph, sample_plan):
        result = graph.create_plan(plan_id="p2", plan_type="campaign")
        plans = graph.list_plans(plan_type="maintenance_window")
        assert len(plans) == 1
        assert plans[0]["id"] == "plan_001"

    def test_list_plans_by_status(self, graph, sample_plan):
        result = graph.create_plan(plan_id="p2", plan_type="campaign", status="completed")
        plans = graph.list_plans(status="active")
        assert len(plans) == 1
        assert plans[0]["id"] == "plan_001"

    def test_list_plans_by_horizon(self, graph, sample_plan):
        result = graph.create_plan(plan_id="p2", plan_type="campaign", horizon="FORECAST")
        plans = graph.list_plans(horizon="PLANNED")
        assert len(plans) == 1
        assert plans[0]["id"] == "plan_001"

    def test_list_plans_sorts_by_start_ts(self, graph):
        graph.create_plan(plan_id="p1", plan_type="type_a", start_ts="2026-02-01")
        graph.create_plan(plan_id="p2", plan_type="type_b", start_ts="2026-01-01")
        graph.create_plan(plan_id="p3", plan_type="type_c", start_ts="2026-03-01")
        plans = graph.list_plans()
        assert [p["id"] for p in plans] == ["p2", "p1", "p3"]


class TestCreateAndGetEvent:
    def test_create_event(self, graph):
        result = graph.create_event(
            event_id="e1",
            node_id="n1",
            event_class="failure",
            start_ts="2026-06-01 10:00:00",
            end_ts="2026-06-01 12:00:00",
            title="Pump failure",
            operating_state="unplanned_stop",
        )
        assert result["id"] == "e1"
        assert result["event_class"] == "failure"
        assert result["node_id"] == "n1"

    def test_create_event_minimal(self, graph):
        result = graph.create_event(
            event_id="e2",
            node_id="n1",
            event_class="inspection",
            start_ts="2026-06-01 10:00:00",
        )
        assert result["id"] == "e2"
        assert result["event_class"] == "inspection"

    def test_create_event_with_extra_json_fields(self, graph):
        result = graph.create_event(
            event_id="e3",
            node_id="n1",
            event_class="failure",
            start_ts="2026-06-01 10:00:00",
            source_refs={"ref": "log_001"},
            l3_context={"cause": "overpressure"},
            flow_impact=[{"flow": "coolant", "severity": "high"}],
        )
        assert result["id"] == "e3"
        assert result["event_class"] == "failure"

    def test_get_event(self, graph, sample_event):
        result = graph.get_event("evt_shutdown")
        assert result is not None
        assert result["id"] == "evt_shutdown"
        assert result["event_class"] == "shutdown"

    def test_get_event_nonexistent(self, graph):
        result = graph.get_event("nonexistent")
        assert result is None


class TestGetEventsForNode:
    def test_events_for_node(self, graph, sample_event):
        events = graph.get_events_for_node("compressor_A")
        assert len(events) >= 1
        assert events[0]["id"] == "evt_shutdown"

    def test_events_for_node_empty(self, graph):
        events = graph.get_events_for_node("nonexistent_node")
        assert events == []

    def test_events_for_node_by_horizon(self, graph):
        graph.create_event(
            event_id="evt_planned",
            node_id="compressor_A",
            event_class="shutdown",
            start_ts="2026-07-01",
            horizon="PLANNED",
        )
        graph.create_event(
            event_id="evt_hist",
            node_id="compressor_A",
            event_class="inspection",
            start_ts="2025-01-01",
            horizon="HISTORICAL",
        )
        events = graph.get_events_for_node("compressor_A", horizon="PLANNED")
        assert len(events) == 1
        assert events[0]["id"] == "evt_planned"

    def test_events_for_node_by_class(self, graph, sample_event):
        events = graph.get_events_for_node("compressor_A", event_class="shutdown")
        assert len(events) == 1
        events = graph.get_events_for_node("compressor_A", event_class="restart")
        assert events == []

    def test_events_for_node_time_range(self, graph, sample_event):
        events = graph.get_events_for_node("compressor_A", start_after="2026-07-02")
        assert events == []


class TestGetEventsForPlan:
    def test_events_for_plan(self, graph, sample_plan, sample_event):
        graph.create_event(
            event_id="evt_restart",
            plan_id="plan_001",
            node_id="compressor_A",
            event_class="restart",
            start_ts="2026-07-04 14:00:00",
            end_ts="2026-07-04 18:00:00",
        )
        events = graph.get_events_for_plan("plan_001")
        assert len(events) == 2
        assert events[0]["id"] == "evt_shutdown"

    def test_events_for_plan_empty(self, graph):
        events = graph.get_events_for_plan("nonexistent")
        assert events == []

    def test_events_for_plan_sorted(self, graph):
        graph.create_plan(plan_id="plan_sorted", plan_type="test")
        graph.create_event(event_id="e_late", plan_id="plan_sorted", node_id="n1", event_class="a", start_ts="2026-03-01")
        graph.create_event(event_id="e_early", plan_id="plan_sorted", node_id="n1", event_class="b", start_ts="2026-01-01")
        events = graph.get_events_for_plan("plan_sorted")
        assert events[0]["id"] == "e_early"
        assert events[1]["id"] == "e_late"


class TestCreateAndGetEventLink:
    def test_create_event_link(self, graph, sample_event):
        graph.create_event(
            event_id="evt_restart",
            node_id="compressor_A",
            event_class="restart",
            start_ts="2026-07-04 14:00:00",
        )
        link = graph.create_event_link(
            link_id="link_001",
            from_event_id="evt_shutdown",
            to_event_id="evt_restart",
            edge_type="followed_by",
        )
        assert link["id"] == "link_001"
        assert link["edge_type"] == "followed_by"
        assert link["layer"] == "L1"
        assert link["confidence"] == 1.0

    def test_get_event_links_by_event(self, graph, sample_event):
        graph.create_event(event_id="evt_restart", node_id="compressor_A", event_class="restart", start_ts="2026-07-04")
        graph.create_event_link(link_id="l1", from_event_id="evt_shutdown", to_event_id="evt_restart", edge_type="followed_by")
        graph.create_event_link(link_id="l2", from_event_id="evt_shutdown", to_event_id="evt_restart", edge_type="overlaps")
        links = graph.get_event_links(event_id="evt_shutdown")
        assert len(links) == 2

    def test_get_event_links_by_type(self, graph, sample_event):
        graph.create_event(event_id="evt_restart", node_id="compressor_A", event_class="restart", start_ts="2026-07-04")
        graph.create_event(event_id="evt_inspect", node_id="compressor_A", event_class="inspection", start_ts="2026-07-02")
        graph.create_event_link(link_id="l1", from_event_id="evt_shutdown", to_event_id="evt_restart", edge_type="followed_by")
        graph.create_event_link(link_id="l2", from_event_id="evt_shutdown", to_event_id="evt_inspect", edge_type="caused_by")
        links = graph.get_event_links(edge_type="followed_by")
        assert len(links) == 1

    def test_get_event_links_empty(self, graph):
        links = graph.get_event_links()
        assert links == []


class TestIntegration4DayMaintenanceWindow:
    def test_full_scenario(self, graph):
        plan = graph.create_plan(
            plan_id="plan_mw",
            node_id="pump_P101",
            plan_type="maintenance_window",
            label="Pump P101 July Maintenance",
            start_ts="2026-07-01 00:00:00",
            end_ts="2026-07-05 00:00:00",
            horizon="PLANNED",
        )
        evt_shutdown = graph.create_event(
            event_id="evt_shutdown",
            plan_id="plan_mw",
            node_id="pump_P101",
            event_class="shutdown",
            title="Planned shutdown",
            start_ts="2026-07-01 08:00:00",
            end_ts="2026-07-01 12:00:00",
            operating_state="stopped",
            horizon="PLANNED",
        )
        evt_inspect = graph.create_event(
            event_id="evt_inspect",
            plan_id="plan_mw",
            node_id="pump_P101",
            event_class="inspection",
            title="Rotor inspection",
            start_ts="2026-07-02 08:00:00",
            end_ts="2026-07-02 16:00:00",
            operating_state="stopped",
            horizon="PLANNED",
        )
        evt_restart = graph.create_event(
            event_id="evt_restart",
            plan_id="plan_mw",
            node_id="pump_P101",
            event_class="restart",
            title="Post-maintenance restart",
            start_ts="2026-07-04 14:00:00",
            end_ts="2026-07-04 18:00:00",
            operating_state="running",
            horizon="PLANNED",
        )
        graph.create_event_link(link_id="link_01", from_event_id="evt_shutdown", to_event_id="evt_inspect", edge_type="followed_by")
        graph.create_event_link(link_id="link_02", from_event_id="evt_inspect", to_event_id="evt_restart", edge_type="followed_by")
        plan_r = graph.get_plan("plan_mw")
        assert plan_r is not None
        assert plan_r["plan_type"] == "maintenance_window"
        events = graph.get_events_for_plan("plan_mw")
        assert len(events) == 3
        assert events[0]["id"] == "evt_shutdown"
        assert events[1]["id"] == "evt_inspect"
        assert events[2]["id"] == "evt_restart"
        links = graph.get_event_links(event_id="evt_shutdown")
        assert len(links) == 1
        assert links[0]["edge_type"] == "followed_by"
        node_events = graph.get_events_for_node("pump_P101")
        assert len(node_events) == 3


class TestTimelineRollup:
    """OHM-xggk: timeline rollup with horizon, date range, and ancestor grouping."""

    @pytest.fixture
    def hierarchy(self, graph):
        """Build a 3-level L1 CONTAINS hierarchy with events at each leaf.

        plant
        ├── unit_a (CONTAINS)
        │   ├── pump_P101 (CONTAINS)
        │   └── pump_P102 (CONTAINS)
        └── unit_b (CONTAINS)
            └── compressor_C1 (CONTAINS)
        """
        conn = graph._conn
        for nid, lbl in (
            ("plant", "Plant Alpha"),
            ("unit_a", "Unit A"),
            ("unit_b", "Unit B"),
            ("pump_P101", "Pump P101"),
            ("pump_P102", "Pump P102"),
            ("compressor_C1", "Compressor C1"),
        ):
            conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, 'concept', 'test_agent')",
                [nid, lbl],
            )
        for src, dst in (
            ("plant", "unit_a"),
            ("plant", "unit_b"),
            ("unit_a", "pump_P101"),
            ("unit_a", "pump_P102"),
            ("unit_b", "compressor_C1"),
        ):
            conn.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by) VALUES (?, ?, ?, 'CONTAINS', 'L1', 1.0, 'test_agent')",
                [f"e_{src}_{dst}", src, dst],
            )

        graph.create_plan(
            plan_id="plan_outage",
            node_id="plant",
            plan_type="annual_outage",
            label="Annual Outage 2026",
            start_ts="2026-06-01 00:00:00",
            end_ts="2026-06-10 00:00:00",
            horizon="PLANNED",
            status="active",
        )
        graph.create_plan(
            plan_id="plan_pm",
            node_id="unit_a",
            plan_type="pm_schedule",
            label="Unit A PM Schedule",
            start_ts="2026-07-01 00:00:00",
            end_ts="2026-07-31 00:00:00",
            horizon="PLANNED",
        )

        graph.create_event(
            event_id="evt_p101_shutdown",
            plan_id="plan_pm",
            node_id="pump_P101",
            event_class="shutdown",
            title="P101 shutdown",
            start_ts="2026-07-02 08:00:00",
            end_ts="2026-07-02 12:00:00",
            operating_state="stopped",
            horizon="PLANNED",
        )
        graph.create_event(
            event_id="evt_p102_inspect",
            plan_id="plan_pm",
            node_id="pump_P102",
            event_class="inspection",
            title="P102 inspection",
            start_ts="2026-07-03 08:00:00",
            end_ts="2026-07-03 16:00:00",
            operating_state="stopped",
            horizon="PLANNED",
        )
        graph.create_event(
            event_id="evt_c1_failure",
            plan_id="plan_outage",
            node_id="compressor_C1",
            event_class="failure",
            title="C1 unplanned failure",
            start_ts="2026-06-03 02:00:00",
            end_ts="2026-06-03 06:00:00",
            operating_state="failed",
            horizon="HISTORICAL",
        )
        return {
            "plant": "plant",
            "unit_a": "unit_a",
            "pump_P101": "pump_P101",
            "pump_P102": "pump_P102",
            "compressor_C1": "compressor_C1",
        }

    def test_rollup_from_plant_returns_all_descendant_events(self, graph, hierarchy):
        result = graph.timeline_rollup("plant")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_p101_shutdown", "evt_p102_inspect", "evt_c1_failure"}
        assert result["ancestor"] == "plant"

    def test_rollup_from_unit_a_returns_only_unit_a_subtree(self, graph, hierarchy):
        result = graph.timeline_rollup("unit_a")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_p101_shutdown", "evt_p102_inspect"}

    def test_rollup_from_leaf_returns_only_leaf_events(self, graph, hierarchy):
        result = graph.timeline_rollup("pump_P101")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_p101_shutdown"}

    def test_rollup_horizon_filter(self, graph, hierarchy):
        result = graph.timeline_rollup("plant", horizon="PLANNED")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_p101_shutdown", "evt_p102_inspect"}

    def test_rollup_historical_horizon_filter(self, graph, hierarchy):
        result = graph.timeline_rollup("plant", horizon="HISTORICAL")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_c1_failure"}

    def test_rollup_date_range_filter(self, graph, hierarchy):
        result = graph.timeline_rollup("plant", start_after="2026-07-01 00:00:00", end_before="2026-07-31 23:59:59")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_p101_shutdown", "evt_p102_inspect"}

    def test_rollup_event_class_filter(self, graph, hierarchy):
        result = graph.timeline_rollup("plant", event_class="shutdown")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_p101_shutdown"}

    def test_rollup_plan_filter(self, graph, hierarchy):
        result = graph.timeline_rollup("plant", plan_id="plan_outage")
        ids = {e["id"] for e in result["events"]}
        assert ids == {"evt_c1_failure"}

    def test_rollup_includes_plans_by_default(self, graph, hierarchy):
        result = graph.timeline_rollup("plant")
        plan_ids = {p["id"] for p in result["plans"]}
        assert plan_ids == {"plan_pm", "plan_outage"}

    def test_rollup_can_exclude_plans(self, graph, hierarchy):
        result = graph.timeline_rollup("plant", include_plans=False)
        assert "plans" not in result or result["plans"] == []

    def test_rollup_events_ordered_by_start_ts(self, graph, hierarchy):
        result = graph.timeline_rollup("plant")
        start_ts = [e["start_ts"] for e in result["events"]]
        assert start_ts == sorted(start_ts)

    def test_rollup_empty_subtree(self, graph, hierarchy):
        conn = graph._conn
        conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES ('empty_unit', 'Empty Unit', 'concept', 'test_agent')")
        conn.execute("INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by) VALUES ('e_plant_empty', 'plant', 'empty_unit', 'CONTAINS', 'L1', 1.0, 'test_agent')")
        result = graph.timeline_rollup("empty_unit")
        assert result["events"] == []
        assert result["plans"] == []

    def test_rollup_no_descendants(self, graph):
        conn = graph._conn
        conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES ('orphan', 'Orphan', 'concept', 'test_agent')")
        result = graph.timeline_rollup("orphan")
        assert result["events"] == []
        assert result["plans"] == []
        assert result["ancestor"] == "orphan"

    def test_rollup_respects_max_depth(self, graph, hierarchy):
        result = graph.timeline_rollup("plant", max_depth=1)
        ids = {e["id"] for e in result["events"]}
        assert ids == set()
