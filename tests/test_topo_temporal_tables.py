"""Tests for OHM-dm2b: TOPO temporal event domain tables (topo_plans, topo_events, topo_event_links)."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import (
    SchemaConfig,
    TOPO_SCHEMA,
    initialize_schema,
)

ALL_TOPO_TABLES = {
    "topo_prospects",
    "topo_observations",
    "topo_observation_assessments",
    "topo_observation_annotations",
    "topo_observation_followups",
    "topo_plans",
    "topo_events",
    "topo_event_links",
}

NEW_TABLES = {"topo_plans", "topo_events", "topo_event_links"}


class TestTOPOSchemaHasNewTables:
    def test_topo_schema_has_8_domain_tables(self):
        names = {dt.name for dt in TOPO_SCHEMA.domain_tables}
        assert ALL_TOPO_TABLES <= names

    def test_topo_json_has_8_domain_tables(self):
        topo = SchemaConfig.from_json_file("topo.json")
        names = {dt.name for dt in topo.domain_tables}
        assert ALL_TOPO_TABLES <= names

    def test_python_and_json_match(self):
        py_names = {dt.name for dt in TOPO_SCHEMA.domain_tables}
        json_names = {dt.name for dt in SchemaConfig.from_json_file("topo.json").domain_tables}
        assert py_names == json_names


class TestTopoPlansTable:
    def test_columns(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        dt = by_name["topo_plans"]
        cols = {c[0] for c in dt.columns}
        assert {"id", "node_id", "plan_type", "horizon_start", "horizon_end", "status"} <= cols

    def test_primary_key(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_plans"].primary_key == "id"

    def test_indexes(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        idx_names = {i[0] for i in by_name["topo_plans"].indexes}
        assert {"idx_topo_plans_node", "idx_topo_plans_type", "idx_topo_plans_horizon", "idx_topo_plans_status"} <= idx_names

    def test_ordering(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_plans"].ordering == 150


class TestTopoEventsTable:
    def test_columns(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        dt = by_name["topo_events"]
        cols = {c[0] for c in dt.columns}
        assert {"id", "plan_id", "node_id", "event_type", "start_time", "end_time", "severity"} <= cols

    def test_primary_key(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_events"].primary_key == "id"

    def test_indexes(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        idx_names = {i[0] for i in by_name["topo_events"].indexes}
        assert {"idx_topo_events_plan", "idx_topo_events_node", "idx_topo_events_type", "idx_topo_events_time"} <= idx_names

    def test_ordering(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_events"].ordering == 160


class TestTopoEventLinksTable:
    def test_columns(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        dt = by_name["topo_event_links"]
        cols = {c[0] for c in dt.columns}
        assert {"id", "from_event_id", "to_event_id", "link_type"} <= cols

    def test_primary_key(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_event_links"].primary_key == "id"

    def test_indexes(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        idx_names = {i[0] for i in by_name["topo_event_links"].indexes}
        assert {"idx_topo_elinks_from", "idx_topo_elinks_to", "idx_topo_elinks_type"} <= idx_names

    def test_ordering(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_event_links"].ordering == 170


class TestOrdering:
    def test_plans_before_events(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_plans"].ordering < by_name["topo_events"].ordering

    def test_events_before_links(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_events"].ordering < by_name["topo_event_links"].ordering


class TestInitializeSchema:
    def test_all_tables_created(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        for table_name in NEW_TABLES:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
                [table_name],
            ).fetchall()
            assert rows, f"Table {table_name} not created"

    def test_plans_columns_in_db(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='topo_plans' ORDER BY ordinal_position"
        ).fetchall()
        col_names = {r[0] for r in cols}
        assert {"id", "node_id", "plan_type", "horizon_start", "horizon_end"} <= col_names

    def test_events_columns_in_db(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='topo_events' ORDER BY ordinal_position"
        ).fetchall()
        col_names = {r[0] for r in cols}
        assert {"id", "plan_id", "event_type", "start_time", "end_time"} <= col_names

    def test_event_links_columns_in_db(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='topo_event_links' ORDER BY ordinal_position"
        ).fetchall()
        col_names = {r[0] for r in cols}
        assert {"id", "from_event_id", "to_event_id", "link_type"} <= col_names

    def test_indexes_created(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        idx = conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='topo_plans'").fetchall()
        idx_names = {r[0] for r in idx}
        assert {"idx_topo_plans_node", "idx_topo_plans_type"} <= idx_names

    def test_idempotent_rerun(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        initialize_schema(conn, TOPO_SCHEMA)
        for table_name in NEW_TABLES:
            rows = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
            assert rows[0] == 0

    def test_ohm_meta_records_ordering(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        for table_name in NEW_TABLES:
            key = f"domain_tables:{table_name}:ordering"
            row = conn.execute("SELECT value FROM ohm_meta WHERE key = ?", [key]).fetchone()
            assert row is not None, f"ohm_meta missing key: {key}"


class TestCRUDOperations:
    """Integration test: 4-day maintenance window example."""

    def test_maintenance_window_example(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)

        # Create a plan: 4-day maintenance window
        conn.execute(
            "INSERT INTO topo_plans (id, node_id, plan_type, horizon_start, horizon_end, status, created_by) "
            "VALUES ('plan_001', 'compressor_A', 'maintenance_window', '2026-07-01 00:00:00', '2026-07-05 00:00:00', 'active', 'topo_agent')"
        )

        # Create events within the plan
        conn.execute(
            "INSERT INTO topo_events (id, plan_id, node_id, event_type, start_time, end_time, severity, description, created_by) "
            "VALUES ('evt_shutdown', 'plan_001', 'compressor_A', 'shutdown', '2026-07-01 08:00:00', '2026-07-01 12:00:00', 'medium', 'Planned shutdown', 'topo_agent')"
        )
        conn.execute(
            "INSERT INTO topo_events (id, plan_id, node_id, event_type, start_time, end_time, severity, description, created_by) "
            "VALUES ('evt_restart', 'plan_001', 'compressor_A', 'restart', '2026-07-04 14:00:00', '2026-07-04 18:00:00', 'low', 'Restart after maintenance', 'topo_agent')"
        )

        # Link events: shutdown caused_by → restart follows
        conn.execute(
            "INSERT INTO topo_event_links (id, from_event_id, to_event_id, link_type, created_by) "
            "VALUES ('link_001', 'evt_shutdown', 'evt_restart', 'followed_by', 'topo_agent')"
        )

        # Verify
        plans = conn.execute("SELECT id, plan_type, status FROM topo_plans").fetchall()
        assert len(plans) == 1
        assert plans[0][1] == "maintenance_window"
        assert plans[0][2] == "active"

        events = conn.execute("SELECT id, event_type FROM topo_events WHERE plan_id = 'plan_001' ORDER BY start_time").fetchall()
        assert len(events) == 2
        assert events[0][0] == "evt_shutdown"
        assert events[1][0] == "evt_restart"

        links = conn.execute("SELECT from_event_id, to_event_id, link_type FROM topo_event_links").fetchall()
        assert len(links) == 1
        assert links[0] == ("evt_shutdown", "evt_restart", "followed_by")

    def test_annual_outage_example(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)

        conn.execute(
            "INSERT INTO topo_plans (id, node_id, plan_type, horizon_start, horizon_end, status, created_by) "
            "VALUES ('plan_002', 'site_A', 'annual_outage', '2026-12-01 00:00:00', '2026-12-14 00:00:00', 'planned', 'topo_agent')"
        )

        conn.execute(
            "INSERT INTO topo_events (id, plan_id, node_id, event_type, start_time, end_time, severity, description, created_by) "
            "VALUES ('evt_outage', 'plan_002', 'site_A', 'outage', '2026-12-02 00:00:00', '2026-12-10 00:00:00', 'high', 'Annual plant outage', 'topo_agent')"
        )

        plans = conn.execute("SELECT plan_type FROM topo_plans WHERE id = 'plan_002'").fetchone()
        assert plans[0] == "annual_outage"

    def test_preserves_user_data_on_rerun(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        conn.execute(
            "INSERT INTO topo_plans (id, node_id, plan_type, created_by) "
            "VALUES ('p1', 'n1', 'test', 'agent')"
        )
        initialize_schema(conn, TOPO_SCHEMA)
        rows = conn.execute("SELECT id FROM topo_plans").fetchall()
        assert rows == [("p1",)]
