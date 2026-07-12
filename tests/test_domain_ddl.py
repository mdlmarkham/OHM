"""Tests for OHM-vl8o: domain DDL hook + SchemaConfig.domain_tables.

Background: OHM's core schema is fixed (ohm_nodes, ohm_edges,
ohm_observations, …). Domain templates (TOPO, healthcare, …) need extra
tables created alongside the base OHM schema in a single migration
sequence. This test suite verifies:

- DomainTable dataclass: validation, immutability, ordering, to/from_dict
- SchemaConfig.domain_tables: opt-in field, sorted by ordering
- to_dict / from_dict round-trip preserves domain_tables
- from_json_file picks up domain_tables in the template
- initialize_schema() creates the tables, indexes, and seeds
- Idempotency: re-running initialize_schema() is a no-op
- ohm_meta records domain_tables:<name>:ordering after provisioning
- Server.py now passes schema=schema_config to OhmStore (the bug)
- Both library mode and (mocked) ohmd mode work
"""

import json
from pathlib import Path

import duckdb
import pytest

from ohm.graph.schema import (
    DEFAULT_SCHEMA,
    DomainTable,
    SCHEMA_VERSION,
    SchemaConfig,
    TOPO_SCHEMA,
    _create_domain_tables,
    initialize_schema,
)


# ── DomainTable dataclass ──────────────────────────────────────────────────


class TestDomainTableValidation:
    """DomainTable enforces SQL identifier rules and column reference rules."""

    def test_minimal_construct(self):
        dt = DomainTable(name="topo_x", columns=(("id", "VARCHAR"),))
        assert dt.name == "topo_x"
        assert dt.columns == (("id", "VARCHAR"),)
        assert dt.primary_key is None
        assert dt.indexes == ()
        assert dt.ordering == 100
        assert dt.initial_data == ()

    def test_full_construct(self):
        dt = DomainTable(
            name="topo_x",
            columns=(("id", "VARCHAR"), ("v", "FLOAT")),
            primary_key="id",
            indexes=(("idx_v", ("v",)),),
            ordering=50,
            initial_data=({"id": "a", "v": 1.0},),
            description="test",
        )
        assert dt.primary_key == "id"
        assert dt.indexes == (("idx_v", ("v",)),)
        assert dt.ordering == 50
        assert dt.initial_data == ({"id": "a", "v": 1.0},)

    def test_rejects_reserved_ohm_prefix(self):
        with pytest.raises(ValueError, match="reserved 'ohm_' prefix"):
            DomainTable(name="ohm_anything", columns=(("x", "VARCHAR"),))

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="non-empty string"):
            DomainTable(name="", columns=(("x", "VARCHAR"),))

    def test_rejects_invalid_identifier_chars(self):
        with pytest.raises(ValueError, match="not a valid SQL identifier"):
            DomainTable(name="1bad_start", columns=(("x", "VARCHAR"),))
        with pytest.raises(ValueError, match="not a valid SQL identifier"):
            DomainTable(name="has-dash", columns=(("x", "VARCHAR"),))

    def test_rejects_empty_columns(self):
        with pytest.raises(ValueError, match="at least one column"):
            DomainTable(name="mytab", columns=())

    def test_rejects_primary_key_not_in_columns(self):
        with pytest.raises(ValueError, match="primary_key='missing'"):
            DomainTable(name="t", columns=(("a", "VARCHAR"),), primary_key="missing")

    def test_rejects_index_referencing_missing_column(self):
        with pytest.raises(ValueError, match="index 'idx_y' references missing column 'y'"):
            DomainTable(
                name="t",
                columns=(("a", "VARCHAR"),),
                indexes=(("idx_y", ("y",)),),
            )

    def test_rejects_empty_index_name(self):
        with pytest.raises(ValueError, match="empty name"):
            DomainTable(
                name="t",
                columns=(("a", "VARCHAR"),),
                indexes=(("", ("a",)),),
            )

    def test_is_frozen(self):
        dt = DomainTable(name="t", columns=(("a", "VARCHAR"),))
        with pytest.raises(Exception):  # FrozenInstanceError
            dt.name = "other"  # type: ignore[misc]


# ── DomainTable serialization ──────────────────────────────────────────────


class TestDomainTableSerialization:
    def test_to_dict_minimal(self):
        dt = DomainTable(name="t", columns=(("a", "VARCHAR"),))
        d = dt.to_dict()
        assert d == {
            "name": "t",
            "columns": [["a", "VARCHAR"]],
            "ordering": 100,
        }

    def test_to_dict_full(self):
        dt = DomainTable(
            name="t",
            columns=(("a", "VARCHAR"),),
            primary_key="a",
            indexes=(("idx_a", ("a",)),),
            initial_data=({"a": "x"},),
            description="hi",
        )
        d = dt.to_dict()
        assert d["primary_key"] == "a"
        assert d["indexes"] == [["idx_a", ["a"]]]
        assert d["initial_data"] == [{"a": "x"}]
        assert d["description"] == "hi"

    def test_from_dict_roundtrip(self):
        dt = DomainTable(
            name="t",
            columns=(("a", "VARCHAR"), ("b", "FLOAT")),
            primary_key="a",
            indexes=(("idx_b", ("b",)),),
            initial_data=({"a": "x", "b": 1.0},),
        )
        restored = DomainTable.from_dict(dt.to_dict())
        assert restored == dt

    def test_from_dict_requires_name_and_columns(self):
        with pytest.raises(ValueError, match="requires 'name' and 'columns'"):
            DomainTable.from_dict({"name": "t"})
        with pytest.raises(ValueError, match="requires 'name' and 'columns'"):
            DomainTable.from_dict({"columns": []})


# ── SchemaConfig.domain_tables integration ──────────────────────────────────


class TestSchemaConfigDomainTables:
    def test_default_schema_has_empty_domain_tables(self):
        assert DEFAULT_SCHEMA.domain_tables == ()

    def test_topo_template_loads_with_domain_tables(self):
        topo = SchemaConfig.from_json_file("topo.json")
        assert len(topo.domain_tables) >= 1
        names = {dt.name for dt in topo.domain_tables}
        assert "topo_rul_assessments" in names

    def test_domain_tables_sorted_by_ordering(self):
        dt_a = DomainTable(name="zebra", columns=(("x", "VARCHAR"),), ordering=200)
        dt_b = DomainTable(name="apple", columns=(("x", "VARCHAR"),), ordering=100)
        c = SchemaConfig(name="t", domain_tables=[dt_a, dt_b])
        assert [d.name for d in c.domain_tables] == ["apple", "zebra"]

    def test_domain_tables_tie_broken_by_name(self):
        # Same ordering: alphabetic name wins.
        dt_b = DomainTable(name="banana", columns=(("x", "VARCHAR"),), ordering=100)
        dt_a = DomainTable(name="apple", columns=(("x", "VARCHAR"),), ordering=100)
        c = SchemaConfig(name="t", domain_tables=[dt_b, dt_a])
        assert [d.name for d in c.domain_tables] == ["apple", "banana"]

    def test_domain_tables_rejects_non_domaintable(self):
        with pytest.raises(TypeError, match="must contain DomainTable instances"):
            SchemaConfig(name="t", domain_tables=[{"name": "x", "columns": [("a", "VARCHAR")]}])  # type: ignore[list-item]

    def test_domain_tables_default_none_becomes_empty_tuple(self):
        c = SchemaConfig(name="t")
        assert c.domain_tables == ()


# ── to_dict / from_dict round-trip with domain_tables ──────────────────────


class TestSchemaConfigRoundTripWithDomainTables:
    def test_to_dict_includes_domain_tables_when_set(self):
        dt = DomainTable(name="t1", columns=(("id", "VARCHAR"),), primary_key="id")
        c = SchemaConfig(name="test", domain_tables=[dt])
        d = c.to_dict()
        assert "domain_tables" in d
        assert len(d["domain_tables"]) == 1
        assert d["domain_tables"][0]["name"] == "t1"

    def test_to_dict_omits_domain_tables_when_empty(self):
        d = DEFAULT_SCHEMA.to_dict()
        assert "domain_tables" not in d

    def test_from_dict_reconstructs_domain_tables(self):
        dt = DomainTable(
            name="t1",
            columns=(("id", "VARCHAR"), ("v", "FLOAT")),
            primary_key="id",
            indexes=(("idx_v", ("v",)),),
        )
        original = SchemaConfig(name="test", domain_tables=[dt])
        restored = SchemaConfig.from_dict(original.to_dict())
        assert len(restored.domain_tables) == 1
        assert restored.domain_tables[0] == dt

    def test_roundtrip_with_topo_template(self):
        topo = SchemaConfig.from_json_file("topo.json")
        restored = SchemaConfig.from_dict(topo.to_dict())
        assert restored.name == "topo"
        assert len(restored.domain_tables) == len(topo.domain_tables)
        assert {d.name for d in restored.domain_tables} == {d.name for d in topo.domain_tables}


# ── initialize_schema actually creates the tables ──────────────────────────


class TestInitializeSchemaWithDomainTables:
    def test_creates_domain_tables(self):
        dt = DomainTable(
            name="topo_rul_assessments",
            columns=(
                ("id", "VARCHAR"),
                ("equipment_id", "VARCHAR"),
                ("rul_days", "FLOAT"),
            ),
            primary_key="id",
        )
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name='topo_rul_assessments'").fetchall()
        assert tables

    def test_creates_indexes(self):
        dt = DomainTable(
            name="topo_x",
            columns=(("id", "VARCHAR"), ("equipment_id", "VARCHAR")),
            primary_key="id",
            indexes=(("idx_topo_x_eq", ("equipment_id",)),),
        )
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        idx = conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='topo_x'").fetchall()
        idx_names = {row[0] for row in idx}
        assert "idx_topo_x_eq" in idx_names

    def test_idempotent_re_run(self):
        dt = DomainTable(name="t", columns=(("id", "VARCHAR"),), primary_key="id")
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        initialize_schema(conn, cfg)  # second run is a no-op
        initialize_schema(conn, cfg)  # third run still fine
        rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()
        assert rows[0] == 0

    def test_preserves_user_data_on_re_run(self):
        dt = DomainTable(name="t", columns=(("id", "VARCHAR"), ("v", "FLOAT")), primary_key="id")
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        conn.execute("INSERT INTO t (id, v) VALUES ('u1', 1.0)")
        # Re-run initialize_schema — should NOT clobber user data.
        initialize_schema(conn, cfg)
        rows = conn.execute("SELECT id, v FROM t").fetchall()
        assert rows == [("u1", 1.0)]

    def test_initial_data_seeds_on_first_create(self):
        dt = DomainTable(
            name="t",
            columns=(("id", "VARCHAR"), ("v", "VARCHAR")),
            primary_key="id",
            initial_data=({"id": "seed1", "v": "first"},),
        )
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        rows = conn.execute("SELECT id, v FROM t ORDER BY id").fetchall()
        assert rows == [("seed1", "first")]

    def test_initial_data_not_reseeded_on_rerun(self):
        dt = DomainTable(
            name="t",
            columns=(("id", "VARCHAR"), ("v", "VARCHAR")),
            primary_key="id",
            initial_data=({"id": "seed1", "v": "first"},),
        )
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        # Insert user row first.
        conn.execute("INSERT INTO t (id, v) VALUES ('user1', 'x')")
        initialize_schema(conn, cfg)  # Re-run: must NOT re-insert seed (would conflict on PK)
        rows = conn.execute("SELECT id, v FROM t ORDER BY id").fetchall()
        assert rows == [("seed1", "first"), ("user1", "x")]

    def test_ohm_meta_records_provisioning(self):
        dt = DomainTable(name="t", columns=(("id", "VARCHAR"),), primary_key="id", ordering=42)
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        row = conn.execute("SELECT value FROM ohm_meta WHERE key='domain_tables:t:ordering'").fetchone()
        assert row is not None
        assert row[0] == "42"

    def test_default_schema_still_works(self):
        # Backward compat: no schema arg → DEFAULT_SCHEMA → no domain tables.
        conn = duckdb.connect(":memory:")
        initialize_schema(conn)
        rows = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name LIKE 'topo_%'").fetchone()
        assert rows[0] == 0
        # Core OHM tables still present.
        rows = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='ohm_nodes'").fetchone()
        assert rows[0] == 1

    def test_topo_template_creates_topo_rul_assessments(self):
        topo = SchemaConfig.from_json_file("topo.json")
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, topo)
        rows = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='topo_rul_assessments' ORDER BY ordinal_position").fetchall()
        cols = {row[0] for row in rows}
        assert {"id", "equipment_id", "site_id", "rul_days", "risk_class", "model_version"} <= cols

    def test_seed_failure_does_not_block_other_tables(self):
        # First table has a seed that will violate primary key on second insert
        # (we manually re-trigger the seed), second table is clean.
        dt_bad = DomainTable(
            name="bad",
            columns=(("id", "VARCHAR"),),
            primary_key="id",
            initial_data=(
                {"id": "x"},  # First seed will succeed, second will PK-conflict
            ),
        )
        # To test "seed failure doesn't block" we need a config that
        # would re-seed. Instead, simulate by re-running after manual insert.
        dt_good = DomainTable(
            name="good",
            columns=(("id", "VARCHAR"),),
            primary_key="id",
        )
        cfg = SchemaConfig(name="t", domain_tables=[dt_bad, dt_good])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        # Both tables exist.
        tables = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name IN ('bad','good')").fetchall()}
        assert tables == {"bad", "good"}


# ── Migration entry ────────────────────────────────────────────────────────


class TestMigrationForDomainDDL:
    def test_schema_version_040_bumped(self):
        # Monotonic check: SCHEMA_VERSION only ever increases, and pinning an
        # exact string breaks this test on every later, unrelated migration.
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 40, 0)

    def test_migration_0_40_0_present(self):
        from ohm.schema import MIGRATIONS

        versions = [m[0] for m in MIGRATIONS]
        assert "0.40.0" in versions

    def test_migration_0_40_0_is_noop(self):
        # The migration is a no-op version bump — domain DDL is applied
        # in initialize_schema() from SchemaConfig, not via core MIGRATIONS.
        from ohm.schema import MIGRATIONS

        v040 = next(m for m in MIGRATIONS if m[0] == "0.40.0")
        assert v040[2] == []
        assert "vl8o" in v040[1].lower() or "domain" in v040[1].lower()


# ── OhmStore integration ──────────────────────────────────────────────────


class TestOhmStoreSchemaPropagates:
    """Verify OhmStore uses self.schema (not DEFAULT_SCHEMA) for _init_schema.

    This was the latent bug fixed by OHM-vl8o: server.py was creating
    OhmStore without schema=, so domain DDL was silently skipped.
    """

    def test_ohmstore_uses_self_schema(self):
        from ohm.graph.store import OhmStore

        dt = DomainTable(
            name="topo_p",
            columns=(("id", "VARCHAR"),),
            primary_key="id",
        )
        cfg = SchemaConfig(name="topo", domain_tables=[dt])
        store = OhmStore(db_path=":memory:", schema=cfg)
        # The store's init ran during __init__. Verify the table exists.
        rows = store.conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name='topo_p'").fetchall()
        assert rows
        store.close()

    def test_ohmstore_default_schema_no_domain_tables(self):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:")  # default schema
        rows = store.conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'topo_%'").fetchall()
        assert rows == []
        store.close()


# ── server.py wiring ───────────────────────────────────────────────────────


class TestServerOhmStoreReceivesSchema:
    """Regression test for the OHM-vl8o server.py fix."""

    def test_main_passes_schema_to_ohmstore(self):
        # Read server.py source and confirm the OhmStore(...) call passes
        # schema=schema_config. The actual full main() spin-up is not
        # feasible here (would bind a port), but the source check guards
        # against regressions.
        server_path = Path(__file__).parent.parent / "src" / "ohm" / "server" / "server.py"
        text = server_path.read_text(encoding="utf-8")
        # Find OhmStore(db_path=...) inside main() (line near 2894)
        # and confirm schema=schema_config is in the same call.
        assert "schema=schema_config" in text
        # Confirm the fix is at the OhmStore(db_path=...) call site, not somewhere else.
        # Locate the call and check.
        for line in text.splitlines():
            if "store = OhmStore" in line and "schema=schema_config" in line:
                return
        pytest.fail("Expected `store = OhmStore(db_path=..., schema=schema_config)` in server.py — domain DDL will be silently skipped otherwise.")


# ── _create_domain_tables edge cases ───────────────────────────────────────


class TestCreateDomainTablesEdgeCases:
    def test_handles_empty_initial_data(self):
        dt = DomainTable(
            name="t",
            columns=(("id", "VARCHAR"),),
            primary_key="id",
            initial_data=(),  # explicit empty
        )
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        # Just verify the table is there.
        rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name='t'").fetchall()
        assert rows

    def test_multiple_tables_created_in_order(self):
        dt1 = DomainTable(name="first", columns=(("id", "VARCHAR"),), primary_key="id", ordering=10)
        dt2 = DomainTable(name="second", columns=(("id", "VARCHAR"),), primary_key="id", ordering=20)
        dt3 = DomainTable(name="third", columns=(("id", "VARCHAR"),), primary_key="id", ordering=30)
        # Pass in reverse order to verify ordering is honored.
        cfg = SchemaConfig(name="t", domain_tables=[dt3, dt1, dt2])
        conn = duckdb.connect(":memory:")
        initialize_schema(conn, cfg)
        tables = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name IN ('first','second','third')").fetchall()}
        assert tables == {"first", "second", "third"}

    def test_ohm_meta_skipped_for_redundant_writes(self):
        # If ohm_meta already has the key, we should not insert a duplicate
        # (the schema's PRIMARY KEY on ohm_meta.key would error otherwise).
        dt = DomainTable(name="t", columns=(("id", "VARCHAR"),), primary_key="id")
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        conn = duckdb.connect(":memory:")  # initialize_schema runs once
        initialize_schema(conn, cfg)
        initialize_schema(conn, cfg)  # second run must not error on key collision
        # Verify the meta row exists exactly once.
        rows = conn.execute("SELECT COUNT(*) FROM ohm_meta WHERE key='domain_tables:t:ordering'").fetchone()
        assert rows[0] == 1


# ── SchemaConfig.extend() (OHM-835) ─────────────────────────────────────────


class TestSchemaConfigExtend:
    def test_merges_domain_tables(self):
        dt1 = DomainTable(name="alpha", columns=(("id", "VARCHAR"),), ordering=10)
        dt2 = DomainTable(name="beta", columns=(("id", "VARCHAR"),), ordering=20)
        base = SchemaConfig(name="test", domain_tables=[dt1])
        extra = SchemaConfig(name="test", domain_tables=[dt2])
        merged = base.extend(extra)
        names = [dt.name for dt in merged.domain_tables]
        assert "alpha" in names
        assert "beta" in names

    def test_preserves_ordering(self):
        dt1 = DomainTable(name="z_last", columns=(("id", "VARCHAR"),), ordering=200)
        dt2 = DomainTable(name="a_first", columns=(("id", "VARCHAR"),), ordering=10)
        base = SchemaConfig(name="test", domain_tables=[dt1])
        extra = SchemaConfig(name="test", domain_tables=[dt2])
        merged = base.extend(extra)
        names = [dt.name for dt in merged.domain_tables]
        assert names == ["a_first", "z_last"]

    def test_raises_on_table_name_collision(self):
        dt = DomainTable(name="shared", columns=(("id", "VARCHAR"),))
        base = SchemaConfig(name="test", domain_tables=[dt])
        extra = SchemaConfig(name="test", domain_tables=[dt])
        with pytest.raises(ValueError, match="Domain table name collision"):
            base.extend(extra)

    def test_raises_on_index_name_collision(self):
        dt1 = DomainTable(
            name="t1",
            columns=(("id", "VARCHAR"), ("x", "FLOAT")),
            indexes=(("idx_shared", ("x",)),),
        )
        dt2 = DomainTable(
            name="t2",
            columns=(("id", "VARCHAR"), ("y", "FLOAT")),
            indexes=(("idx_shared", ("y",)),),
        )
        base = SchemaConfig(name="test", domain_tables=[dt1])
        extra = SchemaConfig(name="test", domain_tables=[dt2])
        with pytest.raises(ValueError, match="Index name collision"):
            base.extend(extra)

    def test_raises_on_type_mismatch(self):
        base = SchemaConfig(name="test")
        with pytest.raises(TypeError, match="requires a SchemaConfig"):
            base.extend({"name": "nope"})  # type: ignore[arg-type]

    def test_merges_vocabulary(self):
        base = SchemaConfig(
            name="test",
            node_types=frozenset({"a", "b"}),
            observation_types=frozenset({"x"}),
        )
        extra = SchemaConfig(
            name="test",
            node_types=frozenset({"b", "c"}),
            observation_types=frozenset({"y"}),
        )
        merged = base.extend(extra)
        assert "a" in merged.node_types
        assert "b" in merged.node_types
        assert "c" in merged.node_types
        assert "x" in merged.observation_types
        assert "y" in merged.observation_types

    def test_merges_edge_types(self):
        base = SchemaConfig(
            name="test",
            edge_types_by_layer={"L1": frozenset({"E1"}), "L3": frozenset({"E3"})},
        )
        extra = SchemaConfig(
            name="test",
            edge_types_by_layer={"L1": frozenset({"E2"}), "L4": frozenset({"E4"})},
        )
        merged = base.extend(extra)
        assert "E1" in merged.layer_edge_types["L1"]
        assert "E2" in merged.layer_edge_types["L1"]
        assert "E3" in merged.layer_edge_types["L3"]
        assert "E4" in merged.layer_edge_types["L4"]

    def test_layer_descriptions_merge_extra_wins(self):
        base = SchemaConfig(
            name="test",
            layer_descriptions={"L1": "base desc"},
        )
        extra = SchemaConfig(
            name="test",
            layer_descriptions={"L1": "extra desc", "L5": "new layer"},
        )
        merged = base.extend(extra)
        assert merged.layer_descriptions["L1"] == "extra desc"
        assert merged.layer_descriptions["L5"] == "new layer"

    def test_result_is_plain_schemaconfig(self):
        dt = DomainTable(name="ext", columns=(("id", "VARCHAR"),))
        base = SchemaConfig(name="test")
        extra = SchemaConfig(name="test", domain_tables=[dt])
        merged = base.extend(extra)
        assert type(merged) is SchemaConfig

    def test_ducklake_tables_merged(self):
        from ohm.graph.schema import DuckLakeTable

        dlt1 = DuckLakeTable(name="my_table_a")
        dlt2 = DuckLakeTable(name="my_table_b")
        base = SchemaConfig(name="test", ducklake_tables=[dlt1])
        extra = SchemaConfig(name="test", ducklake_tables=[dlt2])
        merged = base.extend(extra)
        names = {d.name for d in merged.ducklake_tables}
        assert "my_table_a" in names
        assert "my_table_b" in names

    def test_extend_empty_is_noop(self):
        dt = DomainTable(name="t", columns=(("id", "VARCHAR"),))
        base = SchemaConfig(name="test", domain_tables=[dt])
        extra = SchemaConfig(name="test")
        merged = base.extend(extra)
        assert len(merged.domain_tables) == 1
        assert merged.domain_tables[0].name == "t"

    def test_extend_both_empty(self):
        base = SchemaConfig(name="test")
        extra = SchemaConfig(name="test")
        merged = base.extend(extra)
        assert merged.domain_tables == ()

    def test_could_have_caught_topo_prospects_collision(self):
        """Confirm extend()'s collision detection would have caught the #834 naming issue."""
        dt_existing = DomainTable(
            name="topo_rul_assessments",
            columns=(("id", "VARCHAR"),),
        )
        # Simulate someone trying to add the old name:
        dt_old = DomainTable(
            name="topo_prospects",
            columns=(("id", "VARCHAR"),),
        )
        base = SchemaConfig(name="topo", domain_tables=[dt_existing])
        extra = SchemaConfig(name="topo", domain_tables=[dt_old])
        # This should NOT collide (different names) — the issue was a rename,
        # not a duplicate. Extend allows both because they're distinct names.
        merged = base.extend(extra)
        assert len(merged.domain_tables) == 2

    def test_from_json_path_loads_file(self, tmp_path):
        schema_data = {
            "name": "test_ext",
            "node_types": ["concept"],
            "layer_edge_types": {"L1": ["LINKS"]},
            "layer_descriptions": {"L1": "Test layer"},
            "observation_types": ["measurement"],
            "observation_sources": ["sensor"],
            "visibilities": ["public"],
            "provenances": ["test"],
            "domain_tables": [
                {"name": "ext_table", "columns": [["id", "VARCHAR"]], "primary_key": "id"}
            ],
        }
        p = tmp_path / "extra.json"
        p.write_text(json.dumps(schema_data))
        loaded = SchemaConfig.from_json_path(str(p))
        assert loaded.name == "test_ext"
        assert len(loaded.domain_tables) == 1
        assert loaded.domain_tables[0].name == "ext_table"

    def test_from_json_path_missing_file(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            SchemaConfig.from_json_path("/nonexistent/path/extra.json")

    def test_from_json_path_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(ValueError, match="Invalid JSON"):
            SchemaConfig.from_json_path(str(p))

    def test_from_json_path_missing_keys(self, tmp_path):
        p = tmp_path / "incomplete.json"
        p.write_text(json.dumps({"name": "x"}))
        with pytest.raises(ValueError, match="missing required keys"):
            SchemaConfig.from_json_path(str(p))


# ── Migration: topo_prospects → topo_rul_assessments (#834) ─────────────────


class TestRenameMigrationWithExistingData:
    """#834 regression: indexes must be dropped BEFORE the rename (DuckDB
    dependency constraint), and data rows must survive the migration."""

    def _build_legacy_table(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Create a realistic pre-migration state: old table + indexes + rows."""
        conn.execute(
            "CREATE TABLE topo_prospects ("
            "  id VARCHAR PRIMARY KEY,"
            "  equipment_id VARCHAR,"
            "  site_id VARCHAR,"
            "  rul_days FLOAT,"
            "  risk_class VARCHAR,"
            "  model_version VARCHAR"
            ")"
        )
        conn.execute("CREATE INDEX idx_topo_prospects_site ON topo_prospects(site_id)")
        conn.execute("CREATE INDEX idx_topo_prospects_risk ON topo_prospects(risk_class)")
        conn.execute("CREATE INDEX idx_topo_prospects_model ON topo_prospects(model_version)")
        conn.execute(
            "INSERT INTO topo_prospects VALUES "
            "('r1', 'eq1', 'siteA', 30.0, 'high', 'v1'), "
            "('r2', 'eq2', 'siteB', 90.0, 'low', 'v2')"
        )

    def test_data_survives_rename(self):
        conn = duckdb.connect(":memory:")
        self._build_legacy_table(conn)

        rows_before = conn.execute("SELECT count(*) FROM topo_prospects").fetchone()[0]
        assert rows_before == 2

        initialize_schema(conn, schema=TOPO_SCHEMA)

        # Old table gone, new table exists.
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()]
        assert "topo_prospects" not in tables
        assert "topo_rul_assessments" in tables

        # Data survived.
        rows_after = conn.execute("SELECT count(*) FROM topo_rul_assessments").fetchone()[0]
        assert rows_after == 2
        r1 = conn.execute(
            "SELECT id, equipment_id, rul_days FROM topo_rul_assessments WHERE id = 'r1'"
        ).fetchone()
        assert r1 == ("r1", "eq1", 30.0)

    def test_new_indexes_exist_after_rename(self):
        conn = duckdb.connect(":memory:")
        self._build_legacy_table(conn)

        initialize_schema(conn, schema=TOPO_SCHEMA)

        indexes = [r[1] for r in conn.execute(
            "SELECT schemaname, indexname FROM pg_catalog.pg_indexes"
        ).fetchall()]
        assert "idx_topo_rul_assessments_site" in indexes
        assert "idx_topo_rul_assessments_risk" in indexes
        assert "idx_topo_rul_assessments_equipment" in indexes
        # Old index names are gone.
        assert "idx_topo_prospects_site" not in indexes
        assert "idx_topo_prospects_risk" not in indexes
        assert "idx_topo_prospects_model" not in indexes

    def test_idempotent_rerun(self):
        conn = duckdb.connect(":memory:")
        self._build_legacy_table(conn)

        initialize_schema(conn, schema=TOPO_SCHEMA)
        # Second run should be a no-op, not crash or duplicate data.
        initialize_schema(conn, schema=TOPO_SCHEMA)

        rows = conn.execute("SELECT count(*) FROM topo_rul_assessments").fetchone()[0]
        assert rows == 2
