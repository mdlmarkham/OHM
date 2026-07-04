"""Tests for OHM-ay5k: TOPO observation lifecycle domain DDL tables.

Verifies that the 4 TOPO observation tables (topo_observations,
topo_observation_assessments, topo_observation_annotations,
topo_observation_followups) are declared in SchemaConfig.topo() and
topo.json, created by initialize_schema(), and have the correct
columns, indexes, and ordering.
"""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import (
    DomainTable,
    SCHEMA_VERSION,
    SchemaConfig,
    TOPO_SCHEMA,
    initialize_schema,
)


EXPECTED_TABLES = {
    "topo_observations",
    "topo_observation_assessments",
    "topo_observation_annotations",
    "topo_observation_followups",
}

ALL_TOPO_TABLES = EXPECTED_TABLES | {"topo_prospects"}


class TestTOPOSchemaDomainTables:
    def test_topo_schema_has_5_domain_tables(self):
        names = {dt.name for dt in TOPO_SCHEMA.domain_tables}
        assert ALL_TOPO_TABLES <= names

    def test_topo_json_has_5_domain_tables(self):
        topo = SchemaConfig.from_json_file("topo.json")
        names = {dt.name for dt in topo.domain_tables}
        assert ALL_TOPO_TABLES <= names

    def test_python_factory_and_json_template_match(self):
        topo_py = TOPO_SCHEMA
        topo_json = SchemaConfig.from_json_file("topo.json")
        py_names = {dt.name for dt in topo_py.domain_tables}
        json_names = {dt.name for dt in topo_json.domain_tables}
        assert py_names == json_names

    def test_domain_tables_sorted_by_ordering(self):
        orderings = [dt.ordering for dt in TOPO_SCHEMA.domain_tables]
        assert orderings == sorted(orderings)

    def test_observations_table_ordering_before_children(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_observations"].ordering < by_name["topo_observation_assessments"].ordering
        assert by_name["topo_observations"].ordering < by_name["topo_observation_annotations"].ordering
        assert by_name["topo_observations"].ordering < by_name["topo_observation_followups"].ordering


class TestTopoObservationsTable:
    def test_columns(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        dt = by_name["topo_observations"]
        cols = {c[0] for c in dt.columns}
        assert {
            "id",
            "node_id",
            "obs_type",
            "obs_value",
            "obs_unit",
            "source",
            "observed_at",
            "created_by",
            "created_at",
            "updated_at",
            "metadata",
        } <= cols

    def test_primary_key(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_observations"].primary_key == "id"

    def test_indexes(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        idx_names = {i[0] for i in by_name["topo_observations"].indexes}
        assert {"idx_topo_obs_node", "idx_topo_obs_type", "idx_topo_obs_time"} <= idx_names


class TestTopoObservationAssessmentsTable:
    def test_columns(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        dt = by_name["topo_observation_assessments"]
        cols = {c[0] for c in dt.columns}
        assert {
            "id",
            "observation_id",
            "assessment_type",
            "assessment_value",
            "is_current",
            "assessed_by",
            "assessed_at",
            "notes",
            "metadata",
        } <= cols

    def test_primary_key(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_observation_assessments"].primary_key == "id"

    def test_has_is_current_column(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        cols = dict(by_name["topo_observation_assessments"].columns)
        assert "is_current" in cols
        assert "BOOLEAN" in cols["is_current"]

    def test_indexes(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        idx_names = {i[0] for i in by_name["topo_observation_assessments"].indexes}
        assert {"idx_topo_asmt_obs", "idx_topo_asmt_current"} <= idx_names


class TestTopoObservationAnnotationsTable:
    def test_columns(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        dt = by_name["topo_observation_annotations"]
        cols = {c[0] for c in dt.columns}
        assert {
            "id",
            "observation_id",
            "annotation_type",
            "annotation_value",
            "annotated_by",
            "annotated_at",
            "metadata",
        } <= cols

    def test_primary_key(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_observation_annotations"].primary_key == "id"

    def test_indexes(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        idx_names = {i[0] for i in by_name["topo_observation_annotations"].indexes}
        assert "idx_topo_anno_obs" in idx_names


class TestTopoObservationFollowupsTable:
    def test_columns(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        dt = by_name["topo_observation_followups"]
        cols = {c[0] for c in dt.columns}
        assert {
            "id",
            "observation_id",
            "followup_type",
            "status",
            "assigned_to",
            "due_date",
            "closed_at",
            "created_by",
            "created_at",
            "updated_at",
            "metadata",
        } <= cols

    def test_primary_key(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        assert by_name["topo_observation_followups"].primary_key == "id"

    def test_indexes(self):
        by_name = {dt.name: dt for dt in TOPO_SCHEMA.domain_tables}
        idx_names = {i[0] for i in by_name["topo_observation_followups"].indexes}
        assert {"idx_topo_fup_obs", "idx_topo_fup_status", "idx_topo_fup_assignee"} <= idx_names


class TestInitializeSchemaCreatesTOPOTables:
    def test_all_tables_created(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        for table_name in ALL_TOPO_TABLES:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
                [table_name],
            ).fetchall()
            assert rows, f"Table {table_name} not created"

    def test_observations_columns_in_db(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='topo_observations' ORDER BY ordinal_position").fetchall()
        col_names = {r[0] for r in cols}
        assert {"id", "node_id", "obs_type", "obs_value", "observed_at"} <= col_names

    def test_assessments_columns_in_db(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='topo_observation_assessments' ORDER BY ordinal_position").fetchall()
        col_names = {r[0] for r in cols}
        assert {"id", "observation_id", "is_current", "assessed_by"} <= col_names

    def test_followups_columns_in_db(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='topo_observation_followups' ORDER BY ordinal_position").fetchall()
        col_names = {r[0] for r in cols}
        assert {"id", "observation_id", "status", "assigned_to"} <= col_names

    def test_indexes_created(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        idx = conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='topo_observations'").fetchall()
        idx_names = {r[0] for r in idx}
        assert {"idx_topo_obs_node", "idx_topo_obs_type", "idx_topo_obs_time"} <= idx_names

    def test_idempotent_rerun(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        initialize_schema(conn, TOPO_SCHEMA)
        for table_name in ALL_TOPO_TABLES:
            rows = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
            assert rows[0] == 0

    def test_preserves_user_data_on_rerun(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        conn.execute("INSERT INTO topo_observations (id, node_id, obs_type, obs_value, created_by) VALUES ('obs1', 'node1', 'vibration', 0.5, 'test_agent')")
        initialize_schema(conn, TOPO_SCHEMA)
        rows = conn.execute("SELECT id, node_id FROM topo_observations").fetchall()
        assert rows == [("obs1", "node1")]

    def test_ohm_meta_records_ordering(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        for table_name in EXPECTED_TABLES:
            key = f"domain_tables:{table_name}:ordering"
            row = conn.execute("SELECT value FROM ohm_meta WHERE key = ?", [key]).fetchone()
            assert row is not None, f"ohm_meta missing key: {key}"

    def test_default_schema_does_not_create_topo_tables(self):
        from ohm.graph.schema import DEFAULT_SCHEMA

        conn = duckdb.connect(":memory:")
        initialize_schema(conn, DEFAULT_SCHEMA)
        rows = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name LIKE 'topo_%'").fetchone()
        assert rows[0] == 0

    def test_json_template_creates_all_tables(self):
        topo = SchemaConfig.from_json_file("topo.json")
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, topo)
        for table_name in ALL_TOPO_TABLES:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
                [table_name],
            ).fetchall()
            assert rows, f"Table {table_name} not created from topo.json"


class TestCRUdOperations:
    """Verify the tables support basic CRUD operations."""

    def test_insert_and_query_observation(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        conn.execute("INSERT INTO topo_observations (id, node_id, obs_type, obs_value, source, created_by) VALUES ('obs1', 'motor_01', 'vibration', 2.5, 'scada', 'agent_a')")
        rows = conn.execute("SELECT id, node_id, obs_type, obs_value FROM topo_observations").fetchall()
        assert rows == [("obs1", "motor_01", "vibration", 2.5)]

    def test_insert_assessment_with_is_current(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        conn.execute("INSERT INTO topo_observations (id, node_id, obs_type, created_by) VALUES ('obs1', 'motor_01', 'vibration', 'agent_a')")
        conn.execute("INSERT INTO topo_observation_assessments (id, observation_id, assessment_type, assessment_value, is_current, assessed_by) VALUES ('asmt1', 'obs1', 'review', 'confirmed', TRUE, 'analyst_b')")
        rows = conn.execute("SELECT observation_id, assessment_type, is_current FROM topo_observation_assessments").fetchall()
        assert rows == [("obs1", "review", True)]

    def test_insert_followup(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        conn.execute("INSERT INTO topo_observations (id, node_id, obs_type, created_by) VALUES ('obs1', 'motor_01', 'vibration', 'agent_a')")
        conn.execute("INSERT INTO topo_observation_followups (id, observation_id, followup_type, status, assigned_to, created_by) VALUES ('fup1', 'obs1', 'investigation', 'open', 'engineer_c', 'agent_a')")
        rows = conn.execute("SELECT observation_id, followup_type, status, assigned_to FROM topo_observation_followups").fetchall()
        assert rows == [("obs1", "investigation", "open", "engineer_c")]

    def test_append_only_assessment_history(self):
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, TOPO_SCHEMA)
        conn.execute("INSERT INTO topo_observations (id, node_id, obs_type, created_by) VALUES ('obs1', 'motor_01', 'vibration', 'agent_a')")
        conn.execute("INSERT INTO topo_observation_assessments (id, observation_id, assessment_type, assessment_value, is_current, assessed_by) VALUES ('asmt1', 'obs1', 'review', 'pending', TRUE, 'analyst_b')")
        conn.execute("UPDATE topo_observation_assessments SET is_current = FALSE WHERE id = 'asmt1'")
        conn.execute("INSERT INTO topo_observation_assessments (id, observation_id, assessment_type, assessment_value, is_current, assessed_by) VALUES ('asmt2', 'obs1', 'review', 'confirmed', TRUE, 'analyst_b')")
        current = conn.execute("SELECT assessment_value FROM topo_observation_assessments WHERE observation_id = 'obs1' AND is_current = TRUE").fetchall()
        assert current == [("confirmed",)]
        all_assessments = conn.execute("SELECT COUNT(*) FROM topo_observation_assessments WHERE observation_id = 'obs1'").fetchone()
        assert all_assessments[0] == 2
