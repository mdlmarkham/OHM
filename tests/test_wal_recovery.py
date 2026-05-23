"""Tests for WAL corruption recovery (OHM-b5a).

DuckDB raises InternalException (not IOException) for WAL replay
failures. Both exception types must be caught for reliable recovery.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestWALRecoveryStorePy:
    """Test store.py._connect_with_wal_recovery() catches both exception types."""

    def test_ioexception_wal_triggers_recovery(self, tmp_path):
        """IOException with WAL in message triggers WAL deletion."""
        import duckdb
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.db")
        wal_path = db_path + ".wal"
        mock_conn = MagicMock()
        call_count = 0

        def mock_connect(path, read_only=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise duckdb.IOException("WAL file is corrupt")
            return mock_conn

        with patch("duckdb.connect", side_effect=mock_connect):
            with patch("os.path.exists", return_value=True):
                with patch("os.remove") as mock_remove:
                    result = OhmStore._connect_with_wal_recovery(db_path)
                    mock_remove.assert_called_once_with(wal_path)
                    assert result is mock_conn

    def test_internal_exception_wal_triggers_recovery(self, tmp_path):
        """InternalException with WAL replay error triggers WAL deletion.

        This is the bug case: DuckDB raises InternalException for
        'Failure while replaying WAL file' errors, not IOException.
        """
        import duckdb
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.db")
        wal_path = db_path + ".wal"
        mock_conn = MagicMock()
        call_count = 0

        def mock_connect(path, read_only=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise duckdb.InternalException("INTERNAL Error: Failure while replaying WAL file '/var/lib/ohm/ohm.duckdb.wal': Calling DatabaseManager::GetDefaultDatabase with no default database set")
            return mock_conn

        with patch("duckdb.connect", side_effect=mock_connect):
            with patch("os.path.exists", return_value=True):
                with patch("os.remove") as mock_remove:
                    result = OhmStore._connect_with_wal_recovery(db_path)
                    mock_remove.assert_called_once_with(wal_path)
                    assert result is mock_conn

    def test_internal_exception_replay_keyword_triggers_recovery(self, tmp_path):
        """InternalException with 'replay' triggers recovery even without 'WAL'."""
        import duckdb
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.db")
        wal_path = db_path + ".wal"
        mock_conn = MagicMock()
        call_count = 0

        def mock_connect(path, read_only=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise duckdb.InternalException("Error during log replay: corrupted entry")
            return mock_conn

        with patch("duckdb.connect", side_effect=mock_connect):
            with patch("os.path.exists", return_value=True):
                with patch("os.remove") as mock_remove:
                    result = OhmStore._connect_with_wal_recovery(db_path)
                    mock_remove.assert_called_once_with(wal_path)
                    assert result is mock_conn

    def test_non_wal_internal_exception_reraises(self, tmp_path):
        """InternalException NOT related to WAL is re-raised."""
        import duckdb
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.db")

        def mock_connect(path, read_only=False):
            raise duckdb.InternalException("Some other internal error")

        with patch("duckdb.connect", side_effect=mock_connect):
            with pytest.raises(duckdb.InternalException, match="Some other internal error"):
                OhmStore._connect_with_wal_recovery(db_path)

    def test_non_wal_ioexception_reraises(self, tmp_path):
        """IOException NOT related to WAL is re-raised."""
        import duckdb
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test.db")

        def mock_connect(path, read_only=False):
            raise duckdb.IOException("Cannot open file: permission denied")

        with patch("duckdb.connect", side_effect=mock_connect):
            with pytest.raises(duckdb.IOException, match="permission denied"):
                OhmStore._connect_with_wal_recovery(db_path)


class TestWALRecoveryDbPy:
    """Test db.py.connect() catches both exception types."""

    def test_internal_exception_wal_triggers_ducklake_recovery(self, tmp_path):
        """InternalException WAL error tries DuckLake recovery first, then WAL delete."""
        import duckdb

        db_path = str(tmp_path / "test.db")
        mock_conn = MagicMock()
        # fetchone()[0] must return a real int so _auto_restore_if_empty can compare it
        mock_conn.execute.return_value.fetchone.return_value = (1,)
        call_count = 0

        def mock_connect(path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise duckdb.InternalException("Failure while replaying WAL file '/var/lib/ohm/ohm.duckdb.wal'")
            return mock_conn

        with patch("duckdb.connect", side_effect=mock_connect):
            with patch("ohm.db._try_ducklake_recovery", return_value=False):
                with patch("ohm.db._auto_restore_if_empty"):
                    with patch("ohm.schema.initialize_schema"):
                        with patch("os.path.exists", return_value=True):
                            with patch("os.remove"):
                                from ohm.db import connect

                                result = connect(str(db_path))
                                assert result is mock_conn

    def test_internal_exception_wal_ducklake_recovery_succeeds(self, tmp_path):
        """When DuckLake recovery works, WAL deletion is skipped."""
        import duckdb

        db_path = str(tmp_path / "test.db")
        mock_conn = MagicMock()
        # fetchone()[0] must return a real int so _auto_restore_if_empty can compare it
        mock_conn.execute.return_value.fetchone.return_value = (1,)
        call_count = 0

        def mock_connect(path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise duckdb.InternalException("Failure while replaying WAL file")
            return mock_conn

        with patch("duckdb.connect", side_effect=mock_connect):
            with patch("ohm.db._try_ducklake_recovery", return_value=True):
                with patch("ohm.db._auto_restore_if_empty"):
                    with patch("ohm.schema.initialize_schema"):
                        from ohm.db import connect

                        result = connect(str(db_path))
                        assert result is mock_conn
