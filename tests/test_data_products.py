"""Tests for data product CRUD operations (OHM-ksi0 / ADR-027).

Tests three code paths:
  1. Query functions (direct connection API)
  2. OhmStore methods (daemon path)
  3. SDK Graph methods (agent path)
"""

from __future__ import annotations

import pytest

from ohm.queries import (
    register_data_product,
    get_data_product,
    get_data_product_by_odps_id,
    list_data_products,
)
from ohm.graph.store import OhmStore
import ohm.sdk as ohm


# ── Query function tests ────────────────────────────────────────────────────


class TestQueryFunctions:
    def test_register_and_retrieve(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.pnl.monthly",
            name="Monthly P&L",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
        )
        assert product["product_id"] == "bos.pnl.monthly"
        assert product["name"] == "Monthly P&L"
        assert product["type"] == "reports"
        assert product["producer_agent"] == "hephaestus"
        assert product["visibility"] == "private"
        assert product["status"] == "draft"
        assert product["language"] == "en"
        assert product["internal_id"] is not None

        retrieved = get_data_product(test_db, product["internal_id"])
        assert retrieved is not None
        assert retrieved["product_id"] == "bos.pnl.monthly"

    def test_upsert_updates_existing(self, test_db):
        register_data_product(
            test_db,
            product_id="bos.risk.weekly",
            name="Risk Report v1",
            type="reports",
            producer_agent="clio",
            created_by="test",
        )
        updated = register_data_product(
            test_db,
            product_id="bos.risk.weekly",
            name="Risk Report v2",
            type="reports",
            producer_agent="clio",
            created_by="test",
            status="production",
        )
        assert updated["name"] == "Risk Report v2"
        assert updated["status"] == "production"

        products = list_data_products(test_db)
        assert len(products) == 1

    def test_get_by_odps_id(self, test_db):
        register_data_product(
            test_db,
            product_id="bos.research.digest",
            name="Research Digest",
            type="reports",
            producer_agent="metis",
            created_by="test",
        )
        product = get_data_product_by_odps_id(test_db, "bos.research.digest")
        assert product is not None
        assert product["name"] == "Research Digest"

        assert get_data_product_by_odps_id(test_db, "nonexistent") is None

    def test_list_with_filters(self, test_db):
        register_data_product(test_db, product_id="p1", name="P1", type="reports", producer_agent="metis", created_by="test")
        register_data_product(test_db, product_id="p2", name="P2", type="analytic view", producer_agent="clio", created_by="test")
        register_data_product(test_db, product_id="p3", name="P3", type="reports", producer_agent="hephaestus", created_by="test", status="production")

        all_products = list_data_products(test_db)
        assert len(all_products) == 3

        reports = list_data_products(test_db, type="reports")
        assert len(reports) == 2

        metis_products = list_data_products(test_db, producer_agent="metis")
        assert len(metis_products) == 1
        assert metis_products[0]["product_id"] == "p1"

        production = list_data_products(test_db, status="production")
        assert len(production) == 1
        assert production[0]["product_id"] == "p3"

    def test_unique_constraint(self, test_db):
        register_data_product(test_db, product_id="dup", name="First", type="reports", producer_agent="a", created_by="test")
        # Same product_id + language + customer_id (NULL) should upsert, not error
        result = register_data_product(test_db, product_id="dup", name="Second", type="reports", producer_agent="a", created_by="test")
        assert result["name"] == "Second"
        assert len(list_data_products(test_db)) == 1


# ── OhmStore tests ──────────────────────────────────────────────────────────


class TestOhmStore:
    def test_register_and_get(self):
        store = OhmStore(db_path=":memory:", agent_name="test")
        product = store.register_data_product(
            "bos.audit.q1",
            "Q1 Audit Summary",
            "reports",
            producer_agent="hephaestus",
        )
        assert product is not None
        assert product["product_id"] == "bos.audit.q1"
        assert product["created_by"] == "test"

        retrieved = store.get_data_product(product["internal_id"])
        assert retrieved is not None
        assert retrieved["name"] == "Q1 Audit Summary"
        store.close()

    def test_list_data_products(self):
        store = OhmStore(db_path=":memory:", agent_name="test")
        store.register_data_product("p1", "P1", "reports", producer_agent="a")
        store.register_data_product("p2", "P2", "reports", producer_agent="b")
        products = store.list_data_products()
        assert len(products) == 2
        store.close()


# ── SDK tests ───────────────────────────────────────────────────────────────


class TestSDK:
    def test_register_and_get(self):
        with ohm.connect(":memory:", actor="metis") as graph:
            product = graph.register_data_product(
                "bos.kpi.dashboard",
                "KPI Dashboard",
                "analytic view",
                producer_agent="deepthought",
            )
            assert product["product_id"] == "bos.kpi.dashboard"
            assert product["created_by"] == "metis"

            retrieved = graph.get_data_product(product["internal_id"])
            assert retrieved is not None
            assert retrieved["name"] == "KPI Dashboard"

    def test_list_data_products(self):
        with ohm.connect(":memory:", actor="metis") as graph:
            graph.register_data_product("s1", "S1", "reports", producer_agent="metis")
            graph.register_data_product("s2", "S2", "reports", producer_agent="clio")
            products = graph.list_data_products()
            assert len(products) == 2
