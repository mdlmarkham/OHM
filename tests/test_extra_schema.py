"""Integration tests for --extra-schema and SchemaConfig.extend() (OHM-835).

Tests the end-to-end flow: load extra schema, merge, create DDL,
verify tables exist in the database. Also tests the reconnect guard
regression (extended tables must survive a restart).
"""

from __future__ import annotations

import json

import duckdb
import pytest

from ohm.graph.schema import (
    DEFAULT_SCHEMA,
    DomainTable,
    SchemaConfig,
    _create_domain_tables,
    initialize_schema,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_extra_schema(
    table_name: str = "ext_custom_table",
    columns: list[list[str]] | None = None,
    ordering: int = 300,
) -> SchemaConfig:
    """Build a minimal extra SchemaConfig with one domain table."""
    if columns is None:
        columns = [["id", "VARCHAR"], ["value", "DOUBLE"]]
    dt = DomainTable(
        name=table_name,
        columns=tuple((c, t) for c, t in columns),
        primary_key="id",
        ordering=ordering,
    )
    return SchemaConfig(
        name="topo",
        node_types=frozenset(),
        edge_types_by_layer={},
        layer_descriptions={},
        observation_types=frozenset(),
        observation_sources=frozenset(),
        visibilities=frozenset(),
        provenances=frozenset(),
        domain_tables=[dt],
    )


def _extra_schema_json(tmp_path, table_name="ext_from_file", extra=None):
    """Write a minimal extra-schema JSON file and return its path."""
    data = {
        "name": "topo",
        "node_types": [],
        "layer_edge_types": {},
        "layer_descriptions": {},
        "observation_types": [],
        "observation_sources": [],
        "visibilities": [],
        "provenances": [],
        "domain_tables": [
            {
                "name": table_name,
                "columns": [["id", "VARCHAR"], ["val", "FLOAT"]],
                "primary_key": "id",
                "ordering": 400,
            }
        ],
    }
    if extra:
        data.update(extra)
    p = tmp_path / "extra_schema.json"
    p.write_text(json.dumps(data))
    return str(p)


# ── extend() + DDL integration ──────────────────────────────────────────────


class TestExtraSchemaCreatesTables:
    def test_extend_and_create_ddl(self):
        """Extend topo schema with an extra table, verify DDL is created."""
        topo = SchemaConfig.from_json_file("topo.json")
        extra = _make_extra_schema("ext_test_table")
        merged = topo.extend(extra)
        assert len(merged.domain_tables) == len(topo.domain_tables) + 1

        conn = duckdb.connect(":memory:")
        initialize_schema(conn, merged)
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'ext_test_table'"
        ).fetchall()
        assert tables, "Extra table should exist after initialize_schema"

    def test_base_schema_without_extra_has_no_extra_table(self):
        """Verify that --schema topo without --extra-schema does NOT create extra tables."""
        topo = SchemaConfig.from_json_file("topo.json")
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, topo)
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'ext_test_table'"
        ).fetchall()
        assert not tables, "Extra table should NOT exist without --extra-schema"

    def test_extra_schema_from_file(self, tmp_path):
        """Load extra schema from a JSON file and verify DDL creation."""
        topo = SchemaConfig.from_json_file("topo.json")
        path = _extra_schema_json(tmp_path, "ext_file_table")
        extra = SchemaConfig.from_json_path(path)
        merged = topo.extend(extra)

        conn = duckdb.connect(":memory:")
        initialize_schema(conn, merged)
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'ext_file_table'"
        ).fetchall()
        assert tables

    def test_multiple_extra_schemas(self):
        """Chain two extra schemas together."""
        topo = SchemaConfig.from_json_file("topo.json")
        extra_a = _make_extra_schema("ext_alpha", ordering=300)
        extra_b = _make_extra_schema("ext_beta", ordering=310)
        merged = topo.extend(extra_a).extend(extra_b)

        conn = duckdb.connect(":memory:")
        initialize_schema(conn, merged)
        tables = {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'ext_%'"
        ).fetchall()}
        assert "ext_alpha" in tables
        assert "ext_beta" in tables


# ── Reconnect guard regression (OHM-835 critical finding) ───────────────────


class TestReconnectGuardExtendedTables:
    def test_extended_tables_survive_reconnect(self):
        """Simulate: extend schema, persist to DB, re-read — extended tables must persist.

        This mirrors the exact scenario from the 3rd comment on #835:
        apply .extend() once with an extra table, then simulate a
        restart (re-read from DB), confirm the extra table is still there.
        """
        topo = SchemaConfig.from_json_file("topo.json")
        extra = _make_extra_schema("ext_persist_test")
        merged = topo.extend(extra)

        conn = duckdb.connect(":memory:")
        initialize_schema(conn, merged)
        merged.to_db(conn)

        # Simulate restart: read schema back from DB
        restored = SchemaConfig.from_db(conn)
        assert restored is not None
        table_names = {dt.name for dt in restored.domain_tables}
        assert "ext_persist_test" in table_names
        # Also verify the base topo tables are still there
        assert "topo_rul_assessments" in table_names

    def test_extend_merges_persisted_and_new(self):
        """Simulate: persist extended schema, restart with additional --extra-schema.

        The reconnect guard should merge the persisted schema (which has
        the first extension) with the new invocation's schema (which has
        a second extension).
        """
        topo = SchemaConfig.from_json_file("topo.json")
        extra_a = _make_extra_schema("ext_first", ordering=300)
        merged_a = topo.extend(extra_a)

        conn = duckdb.connect(":memory:")
        initialize_schema(conn, merged_a)
        merged_a.to_db(conn)

        # Simulate restart with a NEW extra schema
        extra_b = _make_extra_schema("ext_second", ordering=310)
        # The reconnect guard logic: db_schema.extend(schema_config)
        db_schema = SchemaConfig.from_db(conn)
        assert db_schema is not None
        final = db_schema.extend(extra_b) if extra_b else db_schema

        # Verify both extensions are present
        table_names = {dt.name for dt in final.domain_tables}
        assert "ext_first" in table_names, "First extension must survive restart"
        assert "ext_second" in table_names, "Second extension must be merged"
        assert "topo_rul_assessments" in table_names, "Base tables must persist"


# ── Collision detection integration ──────────────────────────────────────────


class TestExtraSchemaCollisionDetection:
    def test_table_name_collision_raises(self):
        """Extending with a table name that already exists raises ValueError."""
        topo = SchemaConfig.from_json_file("topo.json")
        # Try to add a table with the same name as an existing one
        extra = _make_extra_schema("topo_rul_assessments")
        with pytest.raises(ValueError, match="Domain table name collision"):
            topo.extend(extra)

    def test_index_name_collision_raises(self):
        """Extending with an index name that already exists raises ValueError."""
        topo = SchemaConfig.from_json_file("topo.json")
        # Find an existing index name
        existing_idx = topo.domain_tables[0].indexes[0][0] if topo.domain_tables[0].indexes else None
        if existing_idx:
            dt = DomainTable(
                name="ext_new_table",
                columns=(("id", "VARCHAR"), ("x", "FLOAT")),
                indexes=((existing_idx, ("x",)),),
            )
            extra = SchemaConfig(
                name="topo",
                node_types=frozenset(),
                edge_types_by_layer={},
                layer_descriptions={},
                observation_types=frozenset(),
                observation_sources=frozenset(),
                visibilities=frozenset(),
                provenances=frozenset(),
                domain_tables=[dt],
            )
            with pytest.raises(ValueError, match="Index name collision"):
                topo.extend(extra)


# ── DuckLake compatibility ───────────────────────────────────────────────────


class TestExtraSchemaDuckLake:
    def test_extended_tables_get_ducklake_entries(self):
        """Extended tables should auto-derive DuckLakeTable entries."""
        from ohm.graph.schema import DuckLakeTable

        topo = SchemaConfig.from_json_file("topo.json")
        extra = _make_extra_schema("ext_ducklake_test")
        merged = topo.extend(extra)

        dlt_names = {d.name for d in merged.ducklake_tables}
        assert "ext_ducklake_test" in dlt_names
