"""Unit tests for the defensive query helpers in ohm.server.handlers._safe_query (GH #896)."""

from __future__ import annotations

from ohm.server.handlers import _safe_query


class TestSafeScalar:
    """safe_scalar returns the first column of the first row, or a safe default."""

    def test_none_conn_returns_default(self):
        assert _safe_query.safe_scalar(None, "SELECT 1") == 0

    def test_none_conn_custom_default(self):
        assert _safe_query.safe_scalar(None, "SELECT 1", default=None) is None
        assert _safe_query.safe_scalar(None, "SELECT 1", default=7) == 7

    def test_empty_result_set_returns_default(self, test_db):
        assert _safe_query.safe_scalar(
            test_db, "SELECT 1 FROM ohm_nodes WHERE 1=0", default=5
        ) == 5

    def test_count_query_returns_zero_not_default(self, test_db):
        assert _safe_query.safe_scalar(
            test_db, "SELECT count(*) FROM ohm_nodes WHERE deleted_at IS NULL"
        ) == 0

    def test_invalid_sql_returns_default(self, test_db):
        assert _safe_query.safe_scalar(test_db, "SELECT FROM no_such_table", default=-1) == -1


class TestSafeRows:
    """safe_rows returns a list of row tuples, or [] on failure."""

    def test_none_conn_returns_empty(self):
        assert _safe_query.safe_rows(None, "SELECT 1") == []

    def test_empty_result_returns_empty_list(self, test_db):
        rows = _safe_query.safe_rows(
            test_db,
            "SELECT type, count(*) FROM ohm_nodes WHERE deleted_at IS NULL GROUP BY type",
        )
        assert rows == []

    def test_invalid_sql_returns_empty(self, test_db):
        assert _safe_query.safe_rows(test_db, "SELECT FROM no_such_table") == []


class TestSafeUnpackTypeRows:
    """safe_unpack_type_rows skips malformed rows before unpacking."""

    def test_well_shaped_rows_pass_through(self):
        assert _safe_query.safe_unpack_type_rows([("a", 1), ("b", 2)]) == [("a", 1), ("b", 2)]

    def test_malformed_rows_are_skipped(self):
        rows = [("a", 1), ("b", 2, 3), ("c",), "x", None, ("d", 4)]
        assert _safe_query.safe_unpack_type_rows(rows) == [("a", 1), ("d", 4)]

    def test_expected_cols_three(self):
        assert _safe_query.safe_unpack_type_rows([("a", 1, 2), ("b", 3)], expected_cols=3) == [("a", 1, 2)]

    def test_none_input_returns_empty(self):
        assert _safe_query.safe_unpack_type_rows(None) == []


class TestDbUnavailableResponse:
    """db_unavailable_response returns the canonical 503 payload."""

    def test_returns_503_with_error(self):
        status, body = _safe_query.db_unavailable_response()
        assert status == 503
        assert body == {"error": "database_unavailable"}
