"""Tests for graceful degradation of GET /admin/verification-scan (GH #896).

Covers the ``NoneType not subscriptable`` crash when ``current_store.conn`` is
None, and a regression that the scan still succeeds on an empty database
(verifies the duplicate source_reliability block removal and the safe_scalar
summary statistics).
"""

from __future__ import annotations

from ohm.server.handlers.admin import AdminHandlerMixin


class _FakeStore:
    """Stand-in for OhmStore exposing only the connection attributes."""

    def __init__(self, conn=None, read_conn=None):
        self.conn = conn
        self.read_conn = read_conn


class _CapturingAdminHandler(AdminHandlerMixin):
    """AdminHandlerMixin subclass that captures _json_response calls.

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


class TestAdminVerificationScan503:
    """GET /admin/verification-scan returns 503 when conn is None."""

    def test_returns_503_when_conn_none(self):
        handler = _CapturingAdminHandler(_FakeStore(conn=None))
        handler._get_admin_verification_scan("/admin/verification-scan", {})
        assert handler.captured is not None
        status, body = handler.captured
        assert status == 503
        assert body == {"error": "database_unavailable"}

    def test_returns_200_on_empty_db(self, test_db):
        handler = _CapturingAdminHandler(_FakeStore(conn=test_db))
        handler._get_admin_verification_scan("/admin/verification-scan", {})
        assert handler.captured is not None
        status, body = handler.captured
        assert status == 200
        assert body["unverified_edge_count"] == 0
        assert body["high_confidence_no_obs_count"] == 0
        assert body["source_reliability"] == []
        assert body["summary"]["total_outcomes_recorded"] == 0
        assert body["summary"]["total_causal_edges"] == 0
        assert body["summary"]["verification_rate"] == 0.0
