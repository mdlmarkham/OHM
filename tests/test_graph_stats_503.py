"""Tests for graceful degradation of graph_stats and GET /graph/stats (GH #896).

Covers the ``NoneType not subscriptable`` crash paths:
  - ``graph_stats(None)`` returns an error dict instead of crashing.
  - ``graph_stats`` on an empty DB returns safe defaults instead of crashing.
  - ``AnalysisHandlerMixin._get_graph_stats`` returns 503 when read_conn is None.
  - ``_get_graph_stats`` returns 200 with stats when a real connection is present.
"""

from __future__ import annotations

from ohm.graph.methods import graph_stats
from ohm.server.handlers.analysis import AnalysisHandlerMixin


class _FakeStore:
    """Stand-in for OhmStore exposing only the connection attributes."""

    def __init__(self, conn=None, read_conn=None):
        self.conn = conn
        self.read_conn = read_conn


class _CapturingAnalysisHandler(AnalysisHandlerMixin):
    """AnalysisHandlerMixin subclass that captures _json_response calls.

    Bypasses BaseHTTPRequestHandler.__init__ (which needs a live socket) and
    stubs the two attributes the handler methods rely on.
    """

    def __init__(self, store):
        self._store = store
        self.captured = None

    @property
    def current_store(self):
        return self._store

    def _json_response(self, status, body):
        self.captured = (status, body)


class TestGraphStatsNoneConn:
    """graph_stats degrades gracefully when conn is None."""

    def test_none_conn_returns_error_dict(self):
        result = graph_stats(None)
        assert result == {"error": "database_unavailable"}

    def test_empty_db_returns_safe_defaults(self, test_db):
        result = graph_stats(test_db)
        assert result["total_nodes"] == 0
        assert result["total_edges"] == 0
        assert result["density"] == 0
        assert result["orphan_count"] == 0
        assert result["dead_end_count"] == 0
        assert result["hub_count"] == 0
        assert result["avg_confidence"] is None
        assert result["nodes_by_type"] == {}
        assert result["edges_by_type"] == {}
        assert result["edges_by_layer"] == {}


class TestGetGraphStats503:
    """GET /graph/stats returns 503 when read_conn is None."""

    def test_returns_503_when_read_conn_none(self):
        handler = _CapturingAnalysisHandler(_FakeStore(read_conn=None))
        handler._get_graph_stats("/graph/stats", {})
        assert handler.captured is not None
        status, body = handler.captured
        assert status == 503
        assert body == {"error": "database_unavailable"}

    def test_returns_200_when_read_conn_present(self, test_db):
        handler = _CapturingAnalysisHandler(_FakeStore(read_conn=test_db))
        handler._get_graph_stats("/graph/stats", {})
        assert handler.captured is not None
        status, body = handler.captured
        assert status == 200
        assert body["total_nodes"] == 0
        assert "nodes_by_type" in body
