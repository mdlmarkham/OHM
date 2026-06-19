"""End-to-end tests for source_tier + confidence ceilings (OHM-wvz8.1, ADR-028).

Covers the queries layer, OhmStore, and SDK. HTTP handler is covered by
test_server.py. Migration covered by test_schema.py.
"""

from __future__ import annotations

import pytest

from ohm.graph.queries import create_node as queries_create_node, create_edge as queries_create_edge
from ohm.graph.store import OhmStore
from ohm.graph.schema import initialize_schema, VALID_SOURCE_TIERS, SOURCE_TIER_CEILINGS


class TestSourceTierSchema:
    """Migration sanity: source_tier column exists, ceiling values are sane."""

    def test_valid_source_tiers_match_doc(self):
        assert VALID_SOURCE_TIERS == frozenset(
            {"raw", "unverified", "preliminary", "official", "verified"}
        )

    def test_ceilings_strictly_ordered(self):
        ordered = ["raw", "unverified", "preliminary", "official", "verified"]
        ceilings = [SOURCE_TIER_CEILINGS[t] for t in ordered]
        assert ceilings == sorted(ceilings), "ceilings must be non-decreasing"

    def test_ceiling_for_verified_is_one(self):
        assert SOURCE_TIER_CEILINGS["verified"] == 1.0

    def test_ceiling_for_raw_is_low(self):
        assert SOURCE_TIER_CEILINGS["raw"] <= 0.3

    def test_column_exists_on_both_tables(self, test_db):
        for table in ("ohm_nodes", "ohm_edges"):
            cols = {
                row[0]
                for row in test_db.execute(
                    f"SELECT column_name FROM duckdb_columns() WHERE table_name = '{table}'"
                ).fetchall()
            }
            assert "source_tier" in cols, f"{table} missing source_tier column"

    def test_indexes_exist(self, test_db):
        idx = {
            row[0]
            for row in test_db.execute(
                "SELECT index_name FROM duckdb_indexes() WHERE table_name IN ('ohm_nodes', 'ohm_edges')"
            ).fetchall()
        }
        assert "idx_nodes_source_tier" in idx
        assert "idx_edges_source_tier" in idx


class TestCreateNodeWithTier:
    def test_create_with_tier_persists(self, test_db):
        n = queries_create_node(
            test_db,
            label="Verified Paper",
            created_by="metis",
            source_tier="verified",
            confidence=0.95,
        )
        assert n["source_tier"] == "verified"
        row = test_db.execute(
            "SELECT source_tier FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [n["id"]],
        ).fetchone()
        assert row[0] == "verified"

    def test_create_without_tier_persists_null(self, test_db):
        n = queries_create_node(test_db, label="Untracked", created_by="metis", confidence=0.8)
        assert n["source_tier"] is None

    def test_create_above_ceiling_raises(self, test_db):
        with pytest.raises(ValueError, match="exceeds ceiling"):
            queries_create_node(
                test_db, label="Overconfident", created_by="metis",
                source_tier="raw", confidence=0.5,
            )

    def test_create_at_ceiling_passes(self, test_db):
        n = queries_create_node(
            test_db, label="At Limit", created_by="metis",
            source_tier="raw", confidence=0.3,
        )
        assert n["source_tier"] == "raw"

    def test_create_invalid_tier_raises(self, test_db):
        with pytest.raises(ValueError, match="Invalid source_tier"):
            queries_create_node(
                test_db, label="Bogus", created_by="metis",
                source_tier="primary",
            )

    @pytest.mark.parametrize(
        "tier,ceiling",
        [
            ("raw", 0.3),
            ("unverified", 0.5),
            ("preliminary", 0.7),
            ("official", 0.9),
            ("verified", 1.0),
        ],
    )
    def test_each_tier_at_its_ceiling(self, test_db, tier, ceiling):
        n = queries_create_node(
            test_db, label=f"At {tier}", created_by="metis",
            source_tier=tier, confidence=ceiling,
        )
        assert n["source_tier"] == tier


class TestCreateEdgeWithTier:
    def test_create_with_tier_persists(self, test_db):
        a = queries_create_node(test_db, label="A", created_by="metis")
        b = queries_create_node(test_db, label="B", created_by="metis")
        e = queries_create_edge(
            test_db, from_node=a["id"], to_node=b["id"],
            layer="L3", edge_type="CAUSES", created_by="metis",
            source_tier="preliminary", confidence=0.6,
        )
        assert e["source_tier"] == "preliminary"

    def test_create_above_ceiling_raises(self, test_db):
        a = queries_create_node(test_db, label="A", created_by="metis")
        b = queries_create_node(test_db, label="B", created_by="metis")
        with pytest.raises(ValueError, match="exceeds ceiling"):
            queries_create_edge(
                test_db, from_node=a["id"], to_node=b["id"],
                layer="L3", edge_type="CAUSES", created_by="metis",
                source_tier="raw", confidence=0.9,
            )

    def test_create_invalid_tier_raises(self, test_db):
        a = queries_create_node(test_db, label="A", created_by="metis")
        b = queries_create_node(test_db, label="B", created_by="metis")
        with pytest.raises(ValueError, match="Invalid source_tier"):
            queries_create_edge(
                test_db, from_node=a["id"], to_node=b["id"],
                layer="L3", edge_type="CAUSES", created_by="metis",
                source_tier="bogus",
            )


class TestOhmStoreTier:
    def test_write_node_persists_tier(self):
        store = OhmStore(db_path=":memory:", agent_name="metis")
        try:
            result = store.write_node(
                id="store_tier_test_1",
                label="Store Tier Test",
                type="concept",
                confidence=0.6,
                source_tier="preliminary",
            )
            assert result["source_tier"] == "preliminary"
        finally:
            store.close()

    def test_write_node_above_ceiling_raises(self):
        store = OhmStore(db_path=":memory:", agent_name="metis")
        try:
            with pytest.raises(ValueError, match="exceeds ceiling"):
                store.write_node(
                    id="store_tier_bad",
                    label="Bad",
                    type="concept",
                    confidence=0.9,
                    source_tier="raw",
                )
        finally:
            store.close()

    def test_write_edge_persists_tier(self):
        store = OhmStore(db_path=":memory:", agent_name="metis")
        try:
            store.write_node(id="store_a", label="A", type="concept", confidence=1.0)
            store.write_node(id="store_b", label="B", type="concept", confidence=1.0)
            edge = store.write_edge(
                from_node="store_a", to_node="store_b",
                edge_type="CAUSES", layer="L3",
                confidence=0.6, source_tier="preliminary",
            )
            assert edge is not None
            assert edge["source_tier"] == "preliminary"
        finally:
            store.close()

    def test_write_edge_above_ceiling_raises(self):
        store = OhmStore(db_path=":memory:", agent_name="metis")
        try:
            store.write_node(id="store_c", label="C", type="concept", confidence=1.0)
            store.write_node(id="store_d", label="D", type="concept", confidence=1.0)
            with pytest.raises(ValueError, match="exceeds ceiling"):
                store.write_edge(
                    from_node="store_c", to_node="store_d",
                    edge_type="CAUSES", layer="L3",
                    confidence=0.9, source_tier="raw",
                )
        finally:
            store.close()


class TestSdkTier:
    def test_sdk_create_node_with_tier(self):
        from ohm.sdk import connect

        with connect(":memory:", actor="metis") as graph:
            n = graph.create_node(
                "SDK Tier Test",
                source_tier="verified",
                confidence=0.95,
            )
            assert n["source_tier"] == "verified"

    def test_sdk_create_node_ceiling_violation(self):
        from ohm.sdk import connect

        with connect(":memory:", actor="metis") as graph:
            with pytest.raises(ValueError, match="exceeds ceiling"):
                graph.create_node(
                    "Overconfident SDK",
                    source_tier="unverified",
                    confidence=0.9,
                )

    def test_sdk_create_edge_with_tier(self):
        from ohm.sdk import connect

        with connect(":memory:", actor="metis") as graph:
            a = graph.create_node("SDK Edge A")
            b = graph.create_node("SDK Edge B")
            e = graph.create_edge(
                from_node=a["id"],
                to_node=b["id"],
                edge_type="CAUSES",
                layer="L3",
                source_tier="preliminary",
                confidence=0.6,
            )
            assert e["source_tier"] == "preliminary"


class TestBackwardCompatibility:
    """Legacy callers passing no source_tier should continue to work."""

    def test_node_without_tier_passes_ceiling_check(self, test_db):
        n = queries_create_node(test_db, label="Legacy Node", created_by="metis", confidence=0.99)
        assert n["source_tier"] is None

    def test_edge_without_tier_passes_ceiling_check(self, test_db):
        a = queries_create_node(test_db, label="Legacy A", created_by="metis")
        b = queries_create_node(test_db, label="Legacy B", created_by="metis")
        e = queries_create_edge(
            test_db, from_node=a["id"], to_node=b["id"],
            layer="L3", edge_type="CAUSES", created_by="metis",
            confidence=0.99,
        )
        assert e["source_tier"] is None

    def test_store_node_without_tier_passes(self):
        store = OhmStore(db_path=":memory:", agent_name="metis")
        try:
            result = store.write_node(
                id="legacy_store",
                label="Legacy Store Node",
                type="concept",
                confidence=0.99,
            )
            assert result["source_tier"] is None
        finally:
            store.close()
