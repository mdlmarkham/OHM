"""Tests for the startup node-count assertion (OHM #919).

Covers the daemon_metadata helpers (read / persist / check / assert), the
warning-logging path on a detected drop, and the GET /health exposure of the
assertion result. Mirrors the style of tests/test_read_scopes.py (TestXxx
classes, fresh test_db per test, no parametrize).
"""

from __future__ import annotations

import logging

from ohm.graph.daemon_metadata import (
    assert_node_count_at_startup,
    check_node_count_baseline,
    count_live_nodes,
    persist_node_count_baseline,
    read_node_count_baseline,
)


def _insert_node(conn, node_id: str = "n1") -> None:
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence) "
        "VALUES (?, 'test', 'concept', 'test_agent', 'team', 1.0)",
        [node_id],
    )


class TestReadNodeCountBaseline:
    def test_no_row_returns_none(self, test_db):
        assert read_node_count_baseline(test_db) is None

    def test_returns_int_when_present(self, test_db):
        test_db.execute("INSERT INTO ohm_meta (key, value) VALUES ('last_node_count', '42')")
        assert read_node_count_baseline(test_db) == 42

    def test_corrupt_value_returns_none(self, test_db):
        test_db.execute(
            "INSERT INTO ohm_meta (key, value) VALUES ('last_node_count', 'not-a-number')"
        )
        assert read_node_count_baseline(test_db) is None


class TestCountLiveNodes:
    def test_empty_db(self, test_db):
        assert count_live_nodes(test_db) == 0

    def test_counts_non_deleted(self, test_db):
        _insert_node(test_db, "n1")
        _insert_node(test_db, "n2")
        assert count_live_nodes(test_db) == 2

    def test_excludes_soft_deleted(self, test_db):
        _insert_node(test_db, "n1")
        _insert_node(test_db, "n2")
        test_db.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'n1'")
        assert count_live_nodes(test_db) == 1


class TestPersistNodeCountBaseline:
    def test_writes_and_returns(self, test_db):
        _insert_node(test_db, "n1")
        _insert_node(test_db, "n2")
        result = persist_node_count_baseline(test_db)
        assert result == 2
        assert read_node_count_baseline(test_db) == 2

    def test_overwrites_existing(self, test_db):
        test_db.execute("INSERT INTO ohm_meta (key, value) VALUES ('last_node_count', '99')")
        _insert_node(test_db, "n1")
        result = persist_node_count_baseline(test_db)
        assert result == 1
        assert read_node_count_baseline(test_db) == 1


class TestCheckNodeCountBaseline:
    def test_first_startup_no_baseline(self, test_db):
        check = check_node_count_baseline(test_db)
        assert check == {"last": None, "current": 0, "delta": 0, "dropped": False}

    def test_equal_counts_no_drop(self, test_db):
        _insert_node(test_db, "n1")
        persist_node_count_baseline(test_db)
        check = check_node_count_baseline(test_db)
        assert check == {"last": 1, "current": 1, "delta": 0, "dropped": False}

    def test_drop_detected(self, test_db):
        for i in range(5):
            _insert_node(test_db, f"n{i}")
        persist_node_count_baseline(test_db)
        # Simulate WAL loss: hard-delete 3 nodes (not a soft delete).
        test_db.execute("DELETE FROM ohm_nodes WHERE id IN ('n2', 'n3', 'n4')")
        check = check_node_count_baseline(test_db)
        assert check == {"last": 5, "current": 2, "delta": -3, "dropped": True}

    def test_growth_no_drop(self, test_db):
        _insert_node(test_db, "n1")
        persist_node_count_baseline(test_db)
        _insert_node(test_db, "n2")
        _insert_node(test_db, "n3")
        check = check_node_count_baseline(test_db)
        assert check == {"last": 1, "current": 3, "delta": 2, "dropped": False}


class TestAssertNodeCountAtStartup:
    def test_first_startup_persists_baseline(self, test_db):
        check = assert_node_count_at_startup(test_db)
        assert check["dropped"] is False
        assert check["last"] == 0
        assert check["current"] == 0
        assert read_node_count_baseline(test_db) == 0

    def test_first_startup_with_nodes(self, test_db):
        _insert_node(test_db, "n1")
        _insert_node(test_db, "n2")
        check = assert_node_count_at_startup(test_db)
        assert check["dropped"] is False
        assert check["last"] == 2
        assert check["current"] == 2
        assert read_node_count_baseline(test_db) == 2

    def test_equal_counts_no_warning(self, test_db, caplog):
        _insert_node(test_db, "n1")
        persist_node_count_baseline(test_db)
        with caplog.at_level(logging.WARNING):
            check = assert_node_count_at_startup(test_db)
        assert check["dropped"] is False
        assert check["delta"] == 0
        assert "node-count drop" not in caplog.text

    def test_drop_logs_warning(self, test_db, caplog):
        for i in range(5):
            _insert_node(test_db, f"n{i}")
        persist_node_count_baseline(test_db)
        test_db.execute("DELETE FROM ohm_nodes WHERE id IN ('n2', 'n3', 'n4')")
        with caplog.at_level(logging.WARNING):
            check = assert_node_count_at_startup(test_db)
        assert check["dropped"] is True
        assert check["delta"] == -3
        assert "node-count drop" in caplog.text
        assert "last=5" in caplog.text
        assert "current=2" in caplog.text

    def test_growth_no_warning(self, test_db, caplog):
        _insert_node(test_db, "n1")
        persist_node_count_baseline(test_db)
        _insert_node(test_db, "n2")
        with caplog.at_level(logging.WARNING):
            check = assert_node_count_at_startup(test_db)
        assert check["dropped"] is False
        assert check["delta"] == 1
        assert "node-count drop" not in caplog.text


class TestHealthExposure:
    def test_health_no_check_fields_when_unset(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        store.node_count_check = None
        status, body = _request("GET", port, "/health")
        assert status == 200
        graph = body.get("graph", {})
        assert "node_count_drop" not in graph
        assert "last_node_count" not in graph
        assert "current_node_count" not in graph

    def test_health_surfaces_drop(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        store.node_count_check = {"last": 10, "current": 7, "delta": -3, "dropped": True}
        status, body = _request("GET", port, "/health")
        assert status == 200
        graph = body["graph"]
        assert graph["node_count_drop"] == {"last": 10, "current": 7, "delta": -3}
        assert graph["last_node_count"] == 10
        assert graph["current_node_count"] == 7

    def test_health_surfaces_counts_when_not_dropped(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        store.node_count_check = {"last": 5, "current": 8, "delta": 3, "dropped": False}
        status, body = _request("GET", port, "/health")
        assert status == 200
        graph = body["graph"]
        assert "node_count_drop" not in graph
        assert graph["last_node_count"] == 5
        assert graph["current_node_count"] == 8

    def test_health_surfaces_equal_counts(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        store.node_count_check = {"last": 4, "current": 4, "delta": 0, "dropped": False}
        status, body = _request("GET", port, "/health")
        assert status == 200
        graph = body["graph"]
        assert "node_count_drop" not in graph
        assert graph["last_node_count"] == 4
        assert graph["current_node_count"] == 4
