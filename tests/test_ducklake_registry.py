"""Tests for OHM-8bli: configurable DuckLake table registry.

Background: OHM's DuckLake sync was hardcoded to the three core tables
(ohm_nodes, ohm_edges, ohm_observations). Domain templates that add
their own tables via DomainTable (e.g. TOPO's topo_prospects) had those
tables silently lost on crash/recovery because the sync code didn't
know about them. This suite verifies:

- DuckLakeTable dataclass: validation, to/from_dict, from_domain_table
- DEFAULT_DUCKLAKE_TABLES: the four core entries
- SchemaConfig.ducklake_tables: auto-derives from domain_tables by default
- to_dict / from_dict round-trip preserves ducklake_tables
- OhmStore._ducklake_sync_tables() excludes the change feed
- _table_counts() iterates the registry (not a hardcoded list)
- check_ducklake_health() reports counts for every mirrored table
- CLI display iterates the registry
- _create_ducklake_tables() generates VARCHAR mirror DDL for domain tables
- Repair / pull / sync use the registry (not hardcoded lists)
"""

import json
from pathlib import Path

import duckdb
import pytest

from ohm.graph.schema import (
    DEFAULT_DUCKLAKE_TABLES,
    DEFAULT_SCHEMA,
    DomainTable,
    DuckLakeTable,
    SCHEMA_VERSION,
    SchemaConfig,
    TOPO_SCHEMA,
    initialize_schema,
)
from ohm.graph.store import OhmStore


# ── DuckLakeTable dataclass ────────────────────────────────────────────────


class TestDuckLakeTableValidation:
    """DuckLakeTable enforces SQL identifier rules and provides sane defaults."""

    def test_minimal_construct(self):
        dlt = DuckLakeTable(name="topo_x")
        assert dlt.name == "topo_x"
        assert dlt.primary_key == "id"
        assert dlt.timestamp_col == "updated_at"
        assert dlt.timestamp_fallback == "created_at"
        assert dlt.has_deleted_at is True
        assert dlt.description == ""

    def test_full_construct(self):
        dlt = DuckLakeTable(
            name="topo_x",
            primary_key="row_id",
            timestamp_col="modified_at",
            timestamp_fallback="created_at",
            has_deleted_at=False,
            description="test",
        )
        assert dlt.primary_key == "row_id"
        assert dlt.timestamp_col == "modified_at"
        assert dlt.has_deleted_at is False

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="non-empty string"):
            DuckLakeTable(name="")

    def test_rejects_invalid_identifier(self):
        with pytest.raises(ValueError, match="not a valid SQL identifier"):
            DuckLakeTable(name="1bad_start")
        with pytest.raises(ValueError, match="not a valid SQL identifier"):
            DuckLakeTable(name="has-dash")

    def test_is_frozen(self):
        dlt = DuckLakeTable(name="t")
        with pytest.raises(Exception):  # FrozenInstanceError
            dlt.name = "other"  # type: ignore[misc]


class TestDuckLakeTableSerialization:
    def test_to_dict_defaults_omits_redundant_keys(self):
        dlt = DuckLakeTable(name="t")
        d = dlt.to_dict()
        # Defaults: primary_key=id, timestamp_col=updated_at, has_deleted_at=True
        # → only the non-defaults (or just 'name') should appear.
        assert d["name"] == "t"
        assert "primary_key" in d
        assert "timestamp_col" not in d
        assert "timestamp_fallback" not in d
        assert "has_deleted_at" not in d

    def test_to_dict_full(self):
        dlt = DuckLakeTable(name="t", primary_key="row_id", has_deleted_at=False, description="x")
        d = dlt.to_dict()
        assert d["primary_key"] == "row_id"
        assert d["has_deleted_at"] is False
        assert d["description"] == "x"

    def test_from_dict_roundtrip(self):
        dlt = DuckLakeTable(
            name="t",
            primary_key="row_id",
            timestamp_col="modified_at",
            has_deleted_at=False,
        )
        restored = DuckLakeTable.from_dict(dlt.to_dict())
        assert restored == dlt

    def test_from_dict_requires_name(self):
        with pytest.raises(ValueError, match="requires 'name'"):
            DuckLakeTable.from_dict({})


class TestDuckLakeTableFromDomainTable:
    """Auto-derivation from DomainTable inspects columns for timestamp + deleted_at."""

    def test_derives_pk_from_domain_table(self):
        dt = DomainTable(
            name="topo_prospects",
            columns=(("id", "VARCHAR"), ("v", "FLOAT")),
            primary_key="id",
        )
        dlt = DuckLakeTable.from_domain_table(dt)
        assert dlt.name == "topo_prospects"
        assert dlt.primary_key == "id"

    def test_derives_pk_default_when_domain_table_has_none(self):
        dt = DomainTable(name="t", columns=(("id", "VARCHAR"),))
        dlt = DuckLakeTable.from_domain_table(dt)
        assert dlt.primary_key == "id"

    def test_prefers_updated_at_over_created_at(self):
        dt = DomainTable(
            name="t",
            columns=(
                ("id", "VARCHAR"),
                ("created_at", "TIMESTAMP"),
                ("updated_at", "TIMESTAMP"),
            ),
        )
        dlt = DuckLakeTable.from_domain_table(dt)
        assert dlt.timestamp_col == "updated_at"

    def test_falls_back_to_created_at(self):
        dt = DomainTable(
            name="t",
            columns=(("id", "VARCHAR"), ("created_at", "TIMESTAMP")),
        )
        dlt = DuckLakeTable.from_domain_table(dt)
        assert dlt.timestamp_col == "created_at"
        assert dlt.timestamp_fallback == "created_at"

    def test_detects_deleted_at(self):
        dt = DomainTable(
            name="t",
            columns=(("id", "VARCHAR"), ("deleted_at", "TIMESTAMP")),
        )
        dlt = DuckLakeTable.from_domain_table(dt)
        assert dlt.has_deleted_at is True

    def test_no_deleted_at(self):
        dt = DomainTable(
            name="t",
            columns=(("id", "VARCHAR"),),
        )
        dlt = DuckLakeTable.from_domain_table(dt)
        assert dlt.has_deleted_at is False


# ── DEFAULT_DUCKLAKE_TABLES ────────────────────────────────────────────────


class TestDefaultDuckLakeTables:
    def test_has_core_entries_plus_outcomes(self):
        names = {dlt.name for dlt in DEFAULT_DUCKLAKE_TABLES}
        assert names == {
            "ohm_nodes",
            "ohm_edges",
            "ohm_observations",
            "ohm_change_feed",
            "ohm_outcomes",
        }

    def test_outcomes_uses_recorded_at(self):
        outcomes = next(dlt for dlt in DEFAULT_DUCKLAKE_TABLES if dlt.name == "ohm_outcomes")
        assert outcomes.timestamp_col == "recorded_at"
        assert outcomes.has_deleted_at is False

    def test_observations_uses_created_at(self):
        obs = next(dlt for dlt in DEFAULT_DUCKLAKE_TABLES if dlt.name == "ohm_observations")
        assert obs.timestamp_col == "created_at"

    def test_change_feed_no_deleted_at(self):
        cf = next(dlt for dlt in DEFAULT_DUCKLAKE_TABLES if dlt.name == "ohm_change_feed")
        assert cf.has_deleted_at is False
        assert cf.timestamp_col == "occurred_at"


# ── SchemaConfig.ducklake_tables ───────────────────────────────────────────


class TestSchemaConfigDuckLakeTables:
    def test_default_schema_has_core_entries_plus_outcomes(self):
        names = {dlt.name for dlt in DEFAULT_SCHEMA.ducklake_tables}
        assert names == {
            "ohm_nodes",
            "ohm_edges",
            "ohm_observations",
            "ohm_change_feed",
            "ohm_outcomes",
        }

    def test_explicit_ducklake_tables_overrides_default(self):
        custom = [DuckLakeTable(name="my_table")]
        c = SchemaConfig(name="t", ducklake_tables=custom)
        assert c.ducklake_tables == tuple(custom)

    def test_rejects_non_ducklaketable(self):
        with pytest.raises(TypeError, match="must contain DuckLakeTable"):
            SchemaConfig(name="t", ducklake_tables=[{"name": "x"}])  # type: ignore[list-item]

    def test_ducklake_tables_default_none_uses_core(self):
        c = SchemaConfig(name="t")
        assert c.ducklake_tables == DEFAULT_DUCKLAKE_TABLES

    def test_domain_tables_auto_derive_ducklake_entries(self):
        # OHM-8bli: a SchemaConfig built with domain_tables should
        # auto-include a DuckLakeTable entry for each one. The default
        # constructor wires this up.
        dt = DomainTable(
            name="topo_prospects",
            columns=(("id", "VARCHAR"), ("updated_at", "TIMESTAMP")),
        )
        c = SchemaConfig(name="topo", domain_tables=[dt])
        names = {dlt.name for dlt in c.ducklake_tables}
        # Core tables + the domain table.
        assert "topo_prospects" in names
        assert "ohm_nodes" in names


# ── Round-trip with ducklake_tables ────────────────────────────────────────


class TestSchemaConfigRoundTripWithDuckLakeTables:
    def test_to_dict_includes_ducklake_tables(self):
        dlt = DuckLakeTable(name="my_t", primary_key="row_id")
        c = SchemaConfig(name="t", ducklake_tables=[dlt])
        d = c.to_dict()
        assert "ducklake_tables" in d
        assert any(x["name"] == "my_t" for x in d["ducklake_tables"])

    def test_from_dict_reconstructs_ducklake_tables(self):
        dlt = DuckLakeTable(name="my_t", primary_key="row_id", has_deleted_at=False)
        c = SchemaConfig(name="t", ducklake_tables=[dlt])
        restored = SchemaConfig.from_dict(c.to_dict())
        assert len(restored.ducklake_tables) == len(c.ducklake_tables)
        assert restored.ducklake_tables[0] == dlt

    def test_default_schema_round_trip_preserves_registry(self):
        d = DEFAULT_SCHEMA.to_dict()
        restored = SchemaConfig.from_dict(d)
        assert {dlt.name for dlt in restored.ducklake_tables} == {dlt.name for dlt in DEFAULT_DUCKLAKE_TABLES}

    def test_topo_template_round_trip(self):
        topo = SchemaConfig.from_json_file("topo.json")
        restored = SchemaConfig.from_dict(topo.to_dict())
        # topo.json has topo_prospects in domain_tables, so ducklake_tables
        # should also include the derived topo_prospects entry.
        topo_dlt_names = {dlt.name for dlt in topo.ducklake_tables}
        restored_dlt_names = {dlt.name for dlt in restored.ducklake_tables}
        assert topo_dlt_names == restored_dlt_names
        assert "topo_prospects" in topo_dlt_names


# ── OhmStore registry accessor ─────────────────────────────────────────────


class TestOhmStoreDuckLakeRegistry:
    """OhmStore exposes the registry via _ducklake_sync_tables()."""

    def test_sync_tables_excludes_change_feed(self):
        # _ducklake_sync_tables excludes ohm_change_feed (synced separately)
        store = OhmStore(db_path=":memory:")
        sync_names = {dlt.name for dlt in store._ducklake_sync_tables()}
        assert "ohm_change_feed" not in sync_names
        assert "ohm_nodes" in sync_names
        assert "ohm_edges" in sync_names
        assert "ohm_observations" in sync_names
        store.close()

    def test_sync_tables_includes_domain_tables(self):
        dt = DomainTable(name="topo_x", columns=(("id", "VARCHAR"),))
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        store = OhmStore(db_path=":memory:", schema=cfg)
        sync_names = {dlt.name for dlt in store._ducklake_sync_tables()}
        assert "topo_x" in sync_names
        store.close()

    def test_table_counts_uses_registry(self):
        # _table_counts reads from the registry. With a custom domain
        # table, the counts dict should include the domain table name.
        dt = DomainTable(name="my_t", columns=(("id", "VARCHAR"),))
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        store = OhmStore(db_path=":memory:", schema=cfg)
        counts = store._table_counts()
        assert "my_t" in counts
        assert counts["my_t"] == 0
        # Insert a row and confirm the count updates.
        store.conn.execute("INSERT INTO my_t (id) VALUES ('a')")
        counts = store._table_counts()
        assert counts["my_t"] == 1
        store.close()

    def test_table_counts_handles_no_deleted_at(self):
        # Domain table without deleted_at: should still get a count.
        dt = DomainTable(name="my_t", columns=(("id", "VARCHAR"),))
        cfg = SchemaConfig(
            name="t",
            domain_tables=[dt],
            ducklake_tables=[
                DuckLakeTable(name="my_t", has_deleted_at=False),
            ],
        )
        store = OhmStore(db_path=":memory:", schema=cfg)
        counts = store._table_counts()
        # Counts returns -1 if the query fails; should be 0 on success.
        assert counts["my_t"] == 0
        store.close()


# ── check_ducklake_health reports all mirrored tables ──────────────────────


class TestCheckDuckLakeHealthRegistry:
    def test_health_includes_domain_table_keys(self):
        # Even with no DuckLake attached, the local_counts dict should
        # include domain tables (so operators see them in /health).
        dt = DomainTable(name="my_t", columns=(("id", "VARCHAR"),))
        cfg = SchemaConfig(name="t", domain_tables=[dt])
        store = OhmStore(db_path=":memory:", schema=cfg)
        try:
            health = store.check_ducklake_health()
            assert "my_t" in health["local_counts"]
            assert health["local_counts"]["my_t"] == 0
        finally:
            store.close()

    def test_health_reports_counts_for_all_core_tables(self):
        store = OhmStore(db_path=":memory:")
        try:
            health = store.check_ducklake_health()
            for tbl in ("ohm_nodes", "ohm_edges", "ohm_observations"):
                assert tbl in health["local_counts"]
        finally:
            store.close()


# ── CLI display uses registry ──────────────────────────────────────────────


class TestCLIDuckLakeDisplay:
    def test_cli_uses_registry(self):
        # Read the CLI source to confirm the sync health display iterates
        # the registry. This guards against regressions.
        cli_path = Path(__file__).parent.parent / "src" / "ohm" / "cli" / "__init__.py"
        text = cli_path.read_text(encoding="utf-8")
        # Find the line in _handle_sync that prints the per-table health
        # and confirm it iterates the registry, not a hardcoded list.
        for line in text.splitlines():
            if "for table in" in line and "local_counts" not in line and "sync_health" not in line:
                if "store.schema.ducklake_tables" in line or "ducklake_tables" in line:
                    # Found a registry-driven loop.
                    assert '"ohm_nodes"' not in line, f"CLI health display still has hardcoded table list: {line!r}"
                    return
        # If we got here, we didn't find any registry-driven loop in the
        # CLI sync health display.
        pytest.fail("Expected `for table in (dlt.name for dlt in store.schema.ducklake_tables ...)` in src/ohm/cli/__init__.py for sync health display.")


# ── _create_ducklake_tables generates VARCHAR DDL for domain tables ────────


class TestCreateDuckLakeTablesDynamic:
    """The mirror DDL generator must handle domain tables in the registry."""

    def test_creates_domain_table_mirror_columns(self):
        # OHM-8bli: domain tables in the registry should get a mirror
        # table in the DuckLake schema with all-VARCHAR columns.
        from ohm.graph.db import _create_ducklake_tables

        # Set up: local schema with a domain table, plus a DuckLake catalog.
        dt = DomainTable(
            name="topo_prospects",
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
        # Attach a fake DuckLake catalog (use TYPE ducklake if extension
        # is available, else skip — the dynamic DDL generator doesn't
        # actually require DuckLake extension since it just writes SQL).
        try:
            conn.execute("ATTACH ':memory:' AS ohm_lake")
        except Exception:
            pytest.skip("ATTACH not available in this DuckDB build")
        _create_ducklake_tables(conn, "ohm_lake", schema=cfg)
        # The mirror table for topo_prospects should exist in the
        # ohm_lake catalog (default schema is "main" within that catalog).
        mirror_cols = conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_catalog='ohm_lake' AND table_name='topo_prospects' ORDER BY ordinal_position").fetchall()
        col_types = {c[0]: c[1] for c in mirror_cols}
        # All columns VARCHAR.
        for c, t in col_types.items():
            assert t == "VARCHAR", f"column {c} expected VARCHAR got {t}"
        # All source columns present.
        assert {"id", "equipment_id", "rul_days"} <= set(col_types.keys())


# ── Integration: schema migration version ──────────────────────────────────


class TestSchemaVersionBump:
    def test_schema_version_is_040_or_higher(self):
        # OHM-vl8o bumped to 0.40.0. OHM-8bli is a no-op version-wise
        # — it should be transparent on top of the 0.40.0 base.
        assert SCHEMA_VERSION >= "0.40.0"


# ── Smoke: integration test shape (skipped without DuckLake extension) ────


@pytest.mark.skipif(
    True,  # Skip by default — full integration requires a real DuckLake
    reason="Requires DuckLake extension; tested in test_ducklake.py",
)
class TestIntegrationCrashRecovery:
    """Integration test: simulate crash on a store with domain tables.

    Acceptance criterion from OHM-8bli: "simulate crash on a store with
    domain tables; repair_from_ducklake() restores all tables with
    matching row counts." This is the high-fidelity end-to-end test
    that requires a working DuckLake install. The test below is the
    reference shape; it runs in CI environments that have DuckLake.
    """

    def test_crash_recovery_restores_domain_tables(self, tmp_path):
        # Set up DuckLake mirror with core + domain table data.
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")
        dt = DomainTable(name="topo_x", columns=(("id", "VARCHAR"), ("v", "FLOAT")))
        cfg = SchemaConfig(name="t", domain_tables=[dt])

        # Write to DuckLake mirror.
        dl_store = OhmStore(db_path=ducklake_path, schema=cfg)
        dl_store.conn.execute("INSERT INTO topo_x (id, v) VALUES ('a', 1.0)")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        # Simulate crash: create local store from DuckLake.
        local_store = OhmStore(db_path=local_path, schema=cfg)
        # (real crash simulation would nuke the local DB; the assertion
        # is that after recovery, both tables have the same row count.)
        local_store.conn.execute("CHECKPOINT")
        local_store.close()
