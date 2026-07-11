"""Tests for OHM-802: Generic external signal attachments table."""

from __future__ import annotations

import pytest

from ohm.graph.schema import initialize_schema
from ohm.graph.queries import (
    create_external_signal,
    get_external_signals,
    delete_external_signal,
    create_node,
)


@pytest.fixture
def db():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


@pytest.fixture
def node(db):
    n = create_node(db, label="Test Node", created_by="test-agent")
    return n["id"]


class TestCreateExternalSignal:
    def test_creates_signal(self, db, node):
        result = create_external_signal(
            db,
            node_id=node,
            source_type="opc_ua",
            source_id="ns=2;s=keystone.TAG.Value",
            source_path="TA/MA/CEM/RCC/RM1/VIBRATION",
            unit="mm/s",
            domain="topo",
            metadata={"plant": "RCC", "opc_namespace": 2},
            created_by="test-agent",
        )
        assert result["node_id"] == node
        assert result["source_type"] == "opc_ua"
        assert result["domain"] == "topo"

    def test_idempotent_on_same_source_id(self, db, node):
        r1 = create_external_signal(db, node_id=node, source_type="opc_ua", source_id="ns=2;s=TAG1", created_by="test-agent")
        r2 = create_external_signal(db, node_id=node, source_type="opc_ua", source_id="ns=2;s=TAG1", created_by="test-agent")
        assert r1["id"] == r2["id"]

    def test_different_source_ids_create_separate(self, db, node):
        create_external_signal(db, node_id=node, source_type="opc_ua", source_id="TAG1", created_by="a")
        create_external_signal(db, node_id=node, source_type="opc_ua", source_id="TAG2", created_by="a")
        signals = get_external_signals(db, node)
        assert len(signals) == 2

    def test_different_source_types_same_id_create_separate(self, db, node):
        create_external_signal(db, node_id=node, source_type="opc_ua", source_id="TAG1", created_by="a")
        create_external_signal(db, node_id=node, source_type="timescale", source_id="TAG1", created_by="a")
        signals = get_external_signals(db, node)
        assert len(signals) == 2

    def test_metadata_stored_as_json(self, db, node):
        result = create_external_signal(
            db,
            node_id=node,
            source_type="market_feed",
            source_id="AAPL",
            domain="trading",
            metadata={"exchange": "NASDAQ", "currency": "USD"},
            created_by="test-agent",
        )
        import json

        meta = json.loads(result["metadata"]) if isinstance(result["metadata"], str) else result["metadata"]
        assert meta["exchange"] == "NASDAQ"


class TestGetExternalSignals:
    def test_returns_all_for_node(self, db, node):
        create_external_signal(db, node_id=node, source_type="opc_ua", source_id="T1", created_by="a")
        create_external_signal(db, node_id=node, source_type="timescale", source_id="T2", created_by="a")
        signals = get_external_signals(db, node)
        assert len(signals) == 2

    def test_filters_by_source_type(self, db, node):
        create_external_signal(db, node_id=node, source_type="opc_ua", source_id="T1", created_by="a")
        create_external_signal(db, node_id=node, source_type="timescale", source_id="T2", created_by="a")
        opc = get_external_signals(db, node, source_type="opc_ua")
        assert len(opc) == 1
        assert opc[0]["source_type"] == "opc_ua"

    def test_filters_by_domain(self, db, node):
        create_external_signal(db, node_id=node, source_type="opc_ua", source_id="T1", domain="topo", created_by="a")
        create_external_signal(db, node_id=node, source_type="market_feed", source_id="T2", domain="trading", created_by="a")
        topo = get_external_signals(db, node, domain="topo")
        assert len(topo) == 1
        assert topo[0]["domain"] == "topo"

    def test_empty_for_nonexistent_node(self, db):
        signals = get_external_signals(db, "nonexistent_node")
        assert signals == []

    def test_excludes_deleted(self, db, node):
        sig = create_external_signal(db, node_id=node, source_type="opc_ua", source_id="T1", created_by="a")
        delete_external_signal(db, sig["id"])
        signals = get_external_signals(db, node)
        assert signals == []


class TestDeleteExternalSignal:
    def test_soft_deletes(self, db, node):
        sig = create_external_signal(db, node_id=node, source_type="opc_ua", source_id="T1", created_by="a")
        assert delete_external_signal(db, sig["id"]) is True
        # Should not appear in queries
        assert get_external_signals(db, node) == []

    def test_returns_false_for_nonexistent(self, db):
        assert delete_external_signal(db, "nonexistent_id") is False


class TestCrossDomainIsolation:
    """Two domains using the same table without collision (OHM-811 pattern)."""

    def test_topo_and_trading_coexist(self, db, node):
        create_external_signal(db, node_id=node, source_type="opc_ua", source_id="ns=2;s=TAG", domain="topo", metadata={"plant": "RCC"}, created_by="topo-agent")
        create_external_signal(db, node_id=node, source_type="market_feed", source_id="AAPL", domain="trading", metadata={"exchange": "NASDAQ"}, created_by="trading-agent")

        topo_signals = get_external_signals(db, node, domain="topo")
        trading_signals = get_external_signals(db, node, domain="trading")

        assert len(topo_signals) == 1
        assert len(trading_signals) == 1
        assert topo_signals[0]["source_type"] == "opc_ua"
        assert trading_signals[0]["source_type"] == "market_feed"
