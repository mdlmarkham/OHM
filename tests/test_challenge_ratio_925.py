"""Tests for ``_get_challenge_ratio`` and the ``contradiction_alert`` nudge (issue #925).

``GraphHandlerMixin._get_challenge_ratio`` (used by POST /edge nudge enrichment)
ran two COUNT queries against a table named ``edges`` — but every table in this
schema is prefixed ``ohm_``. The bare ``except Exception`` swallowed the
``CatalogException`` every time, so the ratio always returned the class-level
default ``0.0`` instead of a real value.

These tests verify:
  * the queries now hit ``ohm_edges`` and return a real, non-zero ratio;
  * the ``except`` block logs a WARNING (no longer silent) and still falls back
    to the cached default;
  * the identical ``FROM edges`` typo in the ``contradiction_alert`` nudge
    (``ohm/server/nudges.py``) is also fixed;
  * end-to-end, POST /edge surfaces the real ratio in the ``challenge_reminder``
    nudge message.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import duckdb
import pytest

from ohm.schema import initialize_schema
from ohm.server.handlers.graph import GraphHandlerMixin
from ohm.server.nudges import generate_nudges

_GRAPH_LOGGER = "ohm.server.handlers.graph"


class _StubHandler(GraphHandlerMixin):
    """Minimal handler exposing ``current_store`` for unit-testing ``_get_challenge_ratio``.

    ``GraphHandlerMixin`` inherits ``OhmHandlerBase`` (``BaseHTTPRequestHandler``),
    whose ``__init__`` requires a live socket. We bypass it by not calling
    ``super().__init__``; ``_get_challenge_ratio`` only needs ``self.current_store``
    plus the class-level cache.
    """

    def __init__(self, store):  # noqa: D401 - simple constructor
        self._store = store

    @property
    def current_store(self):
        return self._store


class _MissingTableConn:
    """Connection stand-in that simulates a missing ``ohm_edges`` table.

    Raises the same ``duckdb.CatalogException`` DuckDB raises for an unknown
    table, so the ``except`` branch of ``_get_challenge_ratio`` is exercised
    with a realistic exception rather than a contrived one.
    """

    def execute(self, sql, *args, **kwargs):  # noqa: D401 - mimic duckdb API
        raise duckdb.CatalogException("Table 'ohm_edges' does not exist")


@pytest.fixture
def conn():
    """Fresh in-memory DuckDB with the OHM schema initialized."""
    c = duckdb.connect(":memory:")
    initialize_schema(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_challenge_ratio_cache():
    """Reset the class-level cache (5-minute TTL) so tests never leak stale values."""
    GraphHandlerMixin._challenge_ratio_cache = 0.0
    GraphHandlerMixin._challenge_ratio_cache_time = 0.0
    yield
    GraphHandlerMixin._challenge_ratio_cache = 0.0
    GraphHandlerMixin._challenge_ratio_cache_time = 0.0


def _seed_nodes(conn, *ids):
    for nid in ids:
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, 'concept', 'test_agent')",
            [nid, nid],
        )


def _seed_edge(conn, eid, from_node, to_node, *, layer="L3", edge_type="CAUSES"):
    conn.execute(
        """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence)
           VALUES (?, ?, ?, ?, ?, 'test_agent', 0.8)""",
        [eid, from_node, to_node, layer, edge_type],
    )


class TestGetChallengeRatio:
    """Direct unit tests for ``GraphHandlerMixin._get_challenge_ratio`` (issue #925)."""

    def test_zero_ratio_with_no_challenges(self, conn):
        _seed_nodes(conn, "a", "b", "c", "d")
        _seed_edge(conn, "e1", "a", "b", edge_type="CAUSES")
        _seed_edge(conn, "e2", "c", "d", edge_type="INFLUENCES")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        assert handler._get_challenge_ratio() == 0.0

    def test_nonzero_ratio_with_challenges(self, conn):
        # 4 L3 CAUSES + 1 L3 CHALLENGED_BY -> 1 / 5 = 0.2
        _seed_nodes(conn, "n1", "n2", "n3", "n4", "n5", "tgt")
        for i in range(4):
            _seed_edge(conn, f"l3-{i}", f"n{i + 1}", "tgt", edge_type="CAUSES")
        _seed_edge(conn, "ch-0", "l3-0", "tgt", edge_type="CHALLENGED_BY")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        assert handler._get_challenge_ratio() == pytest.approx(0.2, abs=0.001)

    def test_ratio_uses_ohm_edges_not_edges(self, conn):
        # Regression guard for #925: querying the bare `edges` table raised
        # CatalogException and silently fell back to 0.0. With `ohm_edges` the
        # real ratio is returned. 2 L3 CAUSES + 1 L3 CHALLENGED_BY -> 1 / 3.
        _seed_nodes(conn, "a", "b", "c", "d")
        _seed_edge(conn, "l3-1", "a", "b", edge_type="CAUSES")
        _seed_edge(conn, "l3-2", "c", "d", edge_type="CAUSES")
        _seed_edge(conn, "ch-1", "l3-1", "b", edge_type="CHALLENGED_BY")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        assert handler._get_challenge_ratio() == pytest.approx(1 / 3, abs=0.001)

    def test_ratio_excludes_non_l3_edges_from_denominator(self, conn):
        # 2 L3 + 2 L2 edges, 1 CHALLENGED_BY on L3 -> 1 / 3 (L2 edges excluded
        # from the denominator; the CHALLENGED_BY edge is itself L3 so it counts).
        _seed_nodes(conn, "a", "b", "c", "d", "e", "f")
        _seed_edge(conn, "l3-1", "a", "b", layer="L3", edge_type="CAUSES")
        _seed_edge(conn, "l3-2", "c", "d", layer="L3", edge_type="PREDICTS")
        _seed_edge(conn, "l2-1", "e", "f", layer="L2", edge_type="INFLUENCES")
        _seed_edge(conn, "l2-2", "a", "e", layer="L2", edge_type="REFERENCES")
        _seed_edge(conn, "ch-1", "l3-1", "b", layer="L3", edge_type="CHALLENGED_BY")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        assert handler._get_challenge_ratio() == pytest.approx(1 / 3, abs=0.001)

    def test_ratio_excludes_soft_deleted_edges(self, conn):
        _seed_nodes(conn, "a", "b", "c", "d")
        _seed_edge(conn, "l3-1", "a", "b", edge_type="CAUSES")
        _seed_edge(conn, "l3-2", "c", "d", edge_type="CAUSES")
        _seed_edge(conn, "ch-1", "l3-1", "b", edge_type="CHALLENGED_BY")
        conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'ch-1'")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        # challenge soft-deleted -> 0 / 2 = 0.0
        assert handler._get_challenge_ratio() == 0.0

    def test_empty_graph_returns_zero(self, conn):
        handler = _StubHandler(SimpleNamespace(conn=conn))
        # No edges -> 0 / max(0, 1) = 0.0
        assert handler._get_challenge_ratio() == 0.0

    def test_cache_populated_after_first_call(self, conn):
        _seed_nodes(conn, "a", "b")
        _seed_edge(conn, "l3-1", "a", "b", edge_type="CAUSES")
        _seed_edge(conn, "ch-1", "l3-1", "b", edge_type="CHALLENGED_BY")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        first = handler._get_challenge_ratio()
        assert first == pytest.approx(0.5, abs=0.001)
        assert GraphHandlerMixin._challenge_ratio_cache == pytest.approx(0.5, abs=0.001)
        assert GraphHandlerMixin._challenge_ratio_cache_time > 0.0

    def test_cache_returns_stale_value_within_window(self, conn):
        _seed_nodes(conn, "a", "b")
        _seed_edge(conn, "l3-1", "a", "b", edge_type="CAUSES")
        _seed_edge(conn, "ch-1", "l3-1", "b", edge_type="CHALLENGED_BY")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        first = handler._get_challenge_ratio()
        # Mutate the graph after the cache is populated; within the 5-min window
        # the second call must return the stale cached value, not recompute.
        _seed_edge(conn, "l3-2", "a", "b", edge_type="PREDICTS")
        second = handler._get_challenge_ratio()
        assert second == first  # stale cached value


class TestGetChallengeRatioErrorPath:
    """The ``except`` block must log a WARNING and fall back to the cached default."""

    def test_missing_table_logs_warning_and_returns_cached_default(self, conn, caplog):
        # Cache starts at 0.0 (reset by the autouse fixture). A missing table
        # forces a CatalogException; the ratio must fall back to 0.0 AND log.
        handler = _StubHandler(SimpleNamespace(conn=_MissingTableConn()))
        with caplog.at_level(logging.WARNING, logger=_GRAPH_LOGGER):
            ratio = handler._get_challenge_ratio()
        assert ratio == 0.0  # cached default
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == _GRAPH_LOGGER]
        assert warnings, "expected a WARNING log for the swallowed challenge_ratio query error"
        assert any("challenge_ratio" in r.getMessage() for r in warnings)

    def test_missing_table_returns_previous_cached_value(self, conn, caplog):
        # Populate the cache with a real value, then force a refresh against a
        # missing table; the fallback must return the previously-cached value.
        _seed_nodes(conn, "a", "b", "c", "d")
        _seed_edge(conn, "l3-1", "a", "b", edge_type="CAUSES")
        _seed_edge(conn, "l3-2", "c", "d", edge_type="CAUSES")
        _seed_edge(conn, "ch-1", "l3-1", "b", edge_type="CHALLENGED_BY")
        handler = _StubHandler(SimpleNamespace(conn=conn))
        cached = handler._get_challenge_ratio()
        assert cached == pytest.approx(1 / 3, abs=0.001)

        # Swap to a connection whose table is missing and force a refresh.
        handler._store = SimpleNamespace(conn=_MissingTableConn())
        GraphHandlerMixin._challenge_ratio_cache_time = 0.0  # backdate -> refresh path runs
        with caplog.at_level(logging.WARNING, logger=_GRAPH_LOGGER):
            ratio = handler._get_challenge_ratio()
        assert ratio == pytest.approx(1 / 3, abs=0.001)  # previous cached value
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == _GRAPH_LOGGER]
        assert warnings, "expected a WARNING log on the failed refresh"
        assert any("challenge_ratio" in r.getMessage() for r in warnings)


class TestContradictionAlertNudge:
    """The ``contradiction_alert`` nudge had the identical ``FROM edges`` typo (nudges.py)."""

    def test_contradiction_alert_fires_for_challenged_node(self, conn):
        _seed_nodes(conn, "target", "critic_node")
        _seed_edge(conn, "ch-1", "critic_node", "target", edge_type="CHALLENGED_BY")
        store = SimpleNamespace(conn=conn)
        nudges = generate_nudges(action="observation", node_id="target", store=store)
        alert = [n for n in nudges if n.get("type") == "contradiction_alert"]
        assert alert, f"expected contradiction_alert nudge, got: {[n.get('type') for n in nudges]}"
        assert alert[0]["data"]["challenge_count"] == 1

    def test_no_contradiction_alert_for_unchallenged_node(self, conn):
        _seed_nodes(conn, "target", "src")
        _seed_edge(conn, "e-1", "src", "target", edge_type="CAUSES")
        store = SimpleNamespace(conn=conn)
        nudges = generate_nudges(action="observation", node_id="target", store=store)
        assert not [n for n in nudges if n.get("type") == "contradiction_alert"]


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestChallengeRatioHTTPEndpoint:
    """End-to-end: POST /edge surfaces the real challenge ratio in the nudge message."""

    @staticmethod
    def _seed(conn, nodes, edges):
        for nid in nodes:
            conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, 'concept', 'seeder')",
                [nid, nid],
            )
        for eid, frm, to, etype in edges:
            conn.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence) "
                "VALUES (?, ?, ?, 'L3', ?, 'seeder', 0.8)",
                [eid, frm, to, etype],
            )

    def test_edge_nudge_reflects_real_challenge_ratio(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        conn = store.conn
        # 4 L3 CAUSES + 1 L3 CHALLENGED_BY (ratio = 1/5 = 20% before the POST).
        self._seed(
            conn,
            nodes=["s1", "s2", "s3", "s4", "t1", "t2", "t3", "t4", "post_from", "post_to"],
            edges=[(f"e{i}", f"s{i + 1}", f"t{i + 1}", "CAUSES") for i in range(4)],
        )
        conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence) "
            "VALUES ('ch0', 'e0', 't1', 'L3', 'CHALLENGED_BY', 'seeder', 0.9)"
        )
        # POST a new CAUSES edge. _get_challenge_ratio runs AFTER write_edge, so
        # the new edge is already counted: 1 CHALLENGED_BY / 6 L3 = 16.7%.
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={"from": "post_from", "to": "post_to", "type": "CAUSES", "layer": "L3", "confidence": 0.8},
        )
        assert status == 201, f"expected 201, got {status}: {data}"
        nudges = data.get("nudges", []) if isinstance(data, dict) else []
        reminder = [n for n in nudges if n.get("type") == "challenge_reminder"]
        assert reminder, f"expected challenge_reminder nudge, got: {[n.get('type') for n in nudges]}"
        msg = reminder[0]["message"]
        assert "Challenge ratio is 16.7%" in msg, msg
        # Regression: the buggy version always reported 0.0% (silent fallback).
        assert "0.0%" not in msg

    def test_edge_nudge_reports_zero_when_no_challenges(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        conn = store.conn
        self._seed(conn, nodes=["post_from2", "post_to2"], edges=[])
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={"from": "post_from2", "to": "post_to2", "type": "CAUSES", "layer": "L3", "confidence": 0.8},
        )
        assert status == 201, f"expected 201, got {status}: {data}"
        nudges = data.get("nudges", []) if isinstance(data, dict) else []
        reminder = [n for n in nudges if n.get("type") == "challenge_reminder"]
        assert reminder, f"expected challenge_reminder nudge, got: {[n.get('type') for n in nudges]}"
        assert "Challenge ratio is 0.0%" in reminder[0]["message"]
