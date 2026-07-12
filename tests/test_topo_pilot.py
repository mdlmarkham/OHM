"""OHM-s4zq: TOPO library-mode pilot with one domain table.

Acceptance test for the TOPO migration chain: run a minimal end-to-end
pilot that exercises every piece of the OHM-vl8o / OHM-8bli / OHM-ue9k
stack against a real DuckLake.

Flow:
   1. Create OhmStore with SchemaConfig.topo() (which has the topo_rul_assessments
      domain table registered and the new metric/data_product/component/
      other node types).
   2. Seed data — both core (ohm_nodes/ohm_edges) AND domain (topo_rul_assessments).
  3. Attach DuckLake + push (sync_to_ducklake) — domain table mirror
     should be created automatically from the registry.
  4. Close the store and SIMULATE A CRASH by deleting the local DuckDB file.
  5. Reopen OhmStore on a fresh DB with the same SchemaConfig.topo().
  6. Run repair_from_ducklake() — both core and domain tables should
     be restored from the DuckLake mirror.
  7. Verify row counts match.

This is the go/no-go gate for broader TOPO migration. If this works
on topo_rul_assessments, the same pattern works for any other domain table
without code changes.
"""

import os
from pathlib import Path

import duckdb
import pytest

from ohm.graph.schema import (
    DomainTable,
    SchemaConfig,
    initialize_schema,
    resolve_schema_by_name,
)
from ohm.graph.store import OhmStore


# Skip the whole module if DuckLake isn't available — these tests
# require the ducklake extension to actually exercise the sync path.
duckdb_connection = duckdb.connect(":memory:")
try:
    duckdb_connection.execute("INSTALL ducklake FROM core")
    duckdb_connection.execute("LOAD ducklake")
    DUCKLAKE_AVAILABLE = True
except Exception:
    DUCKLAKE_AVAILABLE = False
duckdb_connection.close()
_skip_no_ducklake = pytest.mark.skipif(not DUCKLAKE_AVAILABLE, reason="ducklake extension not available")


# A minimal TOPO config with a single domain table for the pilot.
# This is the OHM-vl8o / OHM-8bli / OHM-ue9k stack exercised end-to-end.
def _make_topo_pilot_config() -> SchemaConfig:
    topo_rul_assessments = DomainTable(
        name="topo_rul_assessments",
        columns=(
            ("id", "VARCHAR"),
            ("equipment_id", "VARCHAR"),
            ("rul_days", "FLOAT"),
            ("risk_class", "VARCHAR"),
            ("model_version", "VARCHAR"),
            ("created_by", "VARCHAR NOT NULL"),
            ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ),
        primary_key="id",
        ordering=100,
        description="TOPO RUL assessments (pilot table)",
    )
    # Start from the OHM-built topo() and append our pilot domain table.
    base = resolve_schema_by_name("topo")
    return SchemaConfig(
        name="topo_pilot",
        node_types=base.node_types,
        edge_types_by_layer=dict(base.layer_edge_types),
        layer_descriptions=dict(base.layer_descriptions),
        observation_types=base.observation_types,
        observation_sources=base.observation_sources,
        visibilities=base.visibilities,
        provenances=base.provenances,
        case_strategy=base.case_strategy,  # 'uppercase' for legacy compat
        domain_tables=[topo_rul_assessments],
    )


@_skip_no_ducklake
class TestTopoLibraryModePilot:
    """End-to-end pilot: init → seed → crash → repair → verify."""

    def test_initialize_creates_core_and_domain_tables(self, tmp_path):
        cfg = _make_topo_pilot_config()
        # Library mode: just DuckDB + initialize_schema, no OhmStore needed
        # for the schema-only smoke test.
        conn = duckdb.connect(str(tmp_path / "pilot.duckdb"))
        initialize_schema(conn, cfg)
        # Core tables
        for t in ("ohm_nodes", "ohm_edges", "ohm_observations"):
            assert conn.execute(f"SELECT COUNT(*) FROM information_schema.tables WHERE table_name='{t}'").fetchone()[0] == 1
        # Domain table from OHM-vl8o
        assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='topo_rul_assessments'").fetchone()[0] == 1
        # DuckLake registry (OHM-8bli) includes the domain table
        names = {dlt.name for dlt in cfg.ducklake_tables}
        assert "topo_rul_assessments" in names
        assert "ohm_nodes" in names
        conn.close()

    def test_node_type_vocab_includes_new_types(self):
        # OHM-ue9k: metric/data_product/component/other present.
        cfg = _make_topo_pilot_config()
        for t in ("metric", "data_product", "component", "other"):
            assert t in cfg.node_types
        # case_strategy=uppercase so legacy ALL-CAPS form validates.
        assert cfg.case_strategy == "uppercase"
        assert cfg.validate_node_type("METRIC") is True
        assert cfg.validate_node_type("metric") is True
        # normalize_node_type canonicalizes UPPERCASE → lowercase.
        assert cfg.normalize_node_type("METRIC") == "metric"
        assert cfg.normalize_node_type("DATA_PRODUCT") == "data_product"

    def test_full_pilot_init_seed_crash_repair(self, tmp_path):
        """The OHM-s4zq acceptance test: initialize, seed, crash, repair."""
        local_path = str(tmp_path / "pilot_local.duckdb")
        ducklake_path = str(tmp_path / "pilot_ducklake.ducklake")
        data_path = str(tmp_path / "pilot_ducklake_data")

        cfg = _make_topo_pilot_config()

        # ── Phase 1: seed data with the topo config ─────────────────────
        store = OhmStore(db_path=local_path, schema=cfg, agent_name="pilot_agent")
        # Core OHM writes — exercise the schema's case_strategy (use the
        # legacy UPPERCASE form to prove OHM-ue9k works end-to-end).
        store.write_node("motor_01", "Pump motor 01", "equipment", confidence=0.9)
        store.write_node("sensor_01", "Vibration sensor 01", "sensor", confidence=0.8)
        # Domain write — topo_rul_assessments gets the pilot's RUL data.
        store.conn.execute(
            """INSERT INTO topo_rul_assessments
               (id, equipment_id, rul_days, risk_class, model_version, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ["p1", "motor_01", 30.5, "high", "v1.0", "pilot_agent"],
        )
        store.conn.execute(
            """INSERT INTO topo_rul_assessments
               (id, equipment_id, rul_days, risk_class, model_version, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ["p2", "sensor_01", 90.0, "low", "v1.0", "pilot_agent"],
        )
        store.conn.execute("CHECKPOINT")
        # Pre-crash row counts.
        before_nodes = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        before_prospects = store.conn.execute("SELECT COUNT(*) FROM topo_rul_assessments").fetchone()[0]
        assert before_nodes == 2
        assert before_prospects == 2

        # ── Phase 2: attach DuckLake and push to mirror ──────────────────
        attached = store.attach_ducklake(catalog_path=ducklake_path, data_path=data_path)
        assert attached, "DuckLake attach failed"
        # The mirror for topo_rul_assessments should be auto-created from the
        # registry (OHM-8bli). Verify the catalog has the table.
        mirror_cols = store.conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_catalog='ohm_lake' AND table_name='topo_rul_assessments' ORDER BY ordinal_position").fetchall()
        # All VARCHAR (mirror convention).
        for col, dtype in mirror_cols:
            assert dtype == "VARCHAR", f"mirror column {col} expected VARCHAR, got {dtype}"
        assert len(mirror_cols) >= 6  # id, equipment_id, rul_days, etc.

        # Push initial sync — both core and domain tables go to the mirror.
        pushed = store.sync_to_ducklake(alias="ohm_lake")
        assert pushed > 0
        # Verify the mirror has the data.
        mirror_prospects_count = store.conn.execute("SELECT COUNT(*) FROM ohm_lake.topo_rul_assessments").fetchone()[0]
        assert mirror_prospects_count == 2
        mirror_nodes_count = store.conn.execute("SELECT COUNT(*) FROM ohm_lake.ohm_nodes").fetchone()[0]
        assert mirror_nodes_count == 2
        store.close()

        # ── Phase 3: simulate crash — delete the local DuckDB ───────────
        assert os.path.exists(local_path)
        os.remove(local_path)
        # Also delete any WAL files that might exist.
        for ext in (".wal", ".wal.tmp"):
            p = local_path + ext
            if os.path.exists(p):
                os.remove(p)
        assert not os.path.exists(local_path)

        # ── Phase 4: reopen on a fresh local DB with the same config ────
        new_store = OhmStore(db_path=local_path, schema=cfg, agent_name="pilot_recovery")
        # Confirm the new store starts empty (no auto-restore yet —
        # repair_from_ducklake is the explicit recovery step).
        empty_nodes = new_store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        empty_prospects = new_store.conn.execute("SELECT COUNT(*) FROM topo_rul_assessments").fetchone()[0]
        assert empty_nodes == 0
        assert empty_prospects == 0

        # Re-attach DuckLake (the catalog from Phase 2 is still on disk).
        new_store.ducklake_path = ducklake_path
        new_store.ducklake_data_path = data_path
        attached = new_store.attach_ducklake(catalog_path=ducklake_path, data_path=data_path)
        assert attached, "DuckLake re-attach on recovery failed"

        # ── Phase 5: run repair_from_ducklake() ─────────────────────────
        result = new_store.repair_from_ducklake(alias="ohm_lake")
        # Should report successful inserts from the mirror.
        assert result["verified"], f"repair did not verify: errors={result.get('errors', [])}"
        assert result["inserted"] >= 4  # 2 nodes + 2 prospects

        # ── Phase 6: verify row counts match the pre-crash state ───────
        recovered_nodes = new_store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        recovered_prospects = new_store.conn.execute("SELECT COUNT(*) FROM topo_rul_assessments").fetchone()[0]
        assert recovered_nodes == before_nodes, f"nodes mismatch: before={before_nodes}, after={recovered_nodes}"
        assert recovered_prospects == before_prospects, f"prospects mismatch: before={before_prospects}, after={recovered_prospects}"
        # Spot-check a domain-table row: the high-risk motor with RUL 30.5d.
        p1 = new_store.conn.execute("SELECT equipment_id, rul_days, risk_class FROM topo_rul_assessments WHERE id='p1'").fetchone()
        assert p1 is not None
        assert p1[0] == "motor_01"
        assert p1[1] == 30.5
        assert p1[2] == "high"
        new_store.close()

    def test_pilot_schema_health_reports_domain_table(self, tmp_path):
        """check_ducklake_health() should include the domain table
        in the local_counts / ducklake_counts dicts (OHM-8bli)."""
        cfg = _make_topo_pilot_config()
        store = OhmStore(db_path=str(tmp_path / "health.duckdb"), schema=cfg)
        try:
            health = store.check_ducklake_health()
            # Domain table is in the registry, so its count is reported
            # even when no DuckLake is attached.
            assert "topo_rul_assessments" in health["local_counts"]
            assert health["local_counts"]["topo_rul_assessments"] == 0
        finally:
            store.close()


@_skip_no_ducklake
class TestTopoPilotScenarios:
    """Variant scenarios on top of the pilot stack."""

    def test_pilot_supports_uppercase_legacy_node_types(self, tmp_path):
        cfg = _make_topo_pilot_config()
        store = OhmStore(db_path=str(tmp_path / "legacy.duckdb"), schema=cfg)
        try:
            # Legacy TOPO data: use UPPERCASE node type "EQUIPMENT".
            # OHM-ue9k's case_strategy="uppercase" makes this validate.
            store.write_node("legacy_motor", "Legacy motor", "EQUIPMENT", confidence=0.7)
            # The store should canonicalize on write (or at least accept
            # without rejecting). The query path is case-insensitive.
            row = store.conn.execute("SELECT type FROM ohm_nodes WHERE id='legacy_motor'").fetchone()
            assert row is not None
        finally:
            store.close()

    def test_pilot_with_no_domain_table(self, tmp_path):
        """A topo config with domain tables stripped still works for core OHM writes."""
        full = resolve_schema_by_name("topo")
        cfg = SchemaConfig(
            name=full.name,
            node_types=full.node_types,
            layer_descriptions=full.layer_descriptions,
            observation_types=full.observation_types,
            observation_sources=full.observation_sources,
            provenances=full.provenances,
            case_strategy=full.case_strategy,
        )
        store = OhmStore(db_path=str(tmp_path / "core_only.duckdb"), schema=cfg)
        try:
            # Core OHM writes work as before.
            store.write_node("n1", "node 1", "concept")
            # No topo_rul_assessments in the registry.
            assert "topo_rul_assessments" not in {dlt.name for dlt in cfg.ducklake_tables}
            # check_ducklake_health works without the domain table.
            health = store.check_ducklake_health()
            assert "topo_rul_assessments" not in health["local_counts"]
        finally:
            store.close()
