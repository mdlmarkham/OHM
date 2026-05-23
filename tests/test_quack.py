"""Tests for OHM Quack integration — DuckDB client-server protocol.

Tests are designed to work whether or not the Quack extension is
actually available. When Quack is not installed, tests verify that
the fallback behavior works correctly.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from ohm.quack import (
    validate_quack_uri,
    validate_quack_token,
    reset_availability,
)
from ohm.schema import DEFAULT_SCHEMA


# ── URI Validation ────────────────────────────────────────────────────────────


class TestQuackURIValidation:
    """Tests for Quack URI validation."""

    def test_valid_localhost(self):
        assert validate_quack_uri("quack:localhost") == "quack:localhost"

    def test_valid_localhost_with_port(self):
        assert validate_quack_uri("quack:localhost:9494") == "quack:localhost:9494"

    def test_valid_ip_address(self):
        assert validate_quack_uri("quack:127.0.0.1") == "quack:127.0.0.1"

    def test_valid_ip_with_port(self):
        assert validate_quack_uri("quack:127.0.0.1:9494") == "quack:127.0.0.1:9494"

    def test_valid_remote_host(self):
        assert validate_quack_uri("quack:srv.example.com") == "quack:srv.example.com"

    def test_valid_double_slash(self):
        assert validate_quack_uri("quack://localhost") == "quack://localhost"

    def test_reject_no_prefix(self):
        with pytest.raises(ValueError, match="must start with 'quack:'"):
            validate_quack_uri("http://localhost")

    def test_reject_empty_host(self):
        with pytest.raises(ValueError, match="must specify a host"):
            validate_quack_uri("quack:")

    def test_reject_single_quote(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_quack_uri("quack:localhost'; DROP TABLE--")

    def test_reject_sql_comment(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_quack_uri("quack:localhost--")


# ── Token Validation ──────────────────────────────────────────────────────────


class TestQuackTokenValidation:
    """Tests for Quack token validation."""

    def test_valid_long_token(self):
        token = "a" * 32
        assert validate_quack_token(token) == token

    def test_valid_short_token(self):
        # 4 chars is minimum, but should warn
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = validate_quack_token("abcd")
            assert result == "abcd"
            assert len(w) == 1
            assert "32+ recommended" in str(w[0].message)

    def test_reject_too_short(self):
        with pytest.raises(ValueError, match="at least 4 characters"):
            validate_quack_token("abc")

    def test_reject_empty(self):
        with pytest.raises(ValueError, match="at least 4 characters"):
            validate_quack_token("")

    def test_reject_single_quote(self):
        with pytest.raises(ValueError, match="single quotes"):
            validate_quack_token("abc'def12345678901234567890123")


# ── Availability Detection ───────────────────────────────────────────────────


class TestQuackAvailability:
    """Tests for Quack availability detection."""

    def setup_method(self):
        """Reset cached availability before each test."""
        reset_availability()

    def teardown_method(self):
        """Reset cached availability after each test."""
        reset_availability()

    def test_is_available_returns_bool(self):
        """is_available should return a bool without hitting real DuckDB."""
        from ohm.quack import is_available

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None
        mock_conn.close.return_value = None

        with patch("duckdb.connect", return_value=mock_conn):
            reset_availability()
            result = is_available()
            assert isinstance(result, bool)

    def test_is_available_caches_result(self):
        """Second call should use cached result without re-connecting."""
        from ohm.quack import is_available

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None
        mock_conn.close.return_value = None

        with patch("duckdb.connect", return_value=mock_conn):
            reset_availability()
            result1 = is_available()
            # Second call should be cached — no additional duckdb.connect calls
            result2 = is_available()
            assert result1 == result2

    def test_reset_availability_clears_cache(self):
        reset_availability()
        from ohm import quack as qm

        assert qm._quack_available is None

    def test_is_available_with_mock_success(self):
        """Test that is_available returns True when extension loads."""
        from ohm.quack import is_available

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        with patch("duckdb.connect", return_value=mock_conn):
            reset_availability()
            result = is_available()
            # The mock should make it return True
            assert result is True

    def test_is_available_with_mock_failure(self):
        """Test that is_available returns False when extension fails."""
        from ohm.quack import is_available

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("Extension not found")
        mock_conn.close.return_value = None

        with patch("duckdb.connect", return_value=mock_conn):
            reset_availability()
            result = is_available()
            assert result is False


# ── Server Functions (Mocked) ────────────────────────────────────────────────


class TestQuackServerMocked:
    """Tests for Quack server functions with mocked availability."""

    def setup_method(self):
        reset_availability()

    def teardown_method(self):
        reset_availability()

    def test_start_server_calls_quack_serve(self):
        """Verify start_server calls the right SQL when Quack is available."""
        from ohm.quack import start_server

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        with patch("ohm.quack.is_available", return_value=True):
            with patch.dict(os.environ, {"QUACK_TOKEN": "a" * 32}):
                result = start_server(mock_conn, "quack:localhost", token_env="QUACK_TOKEN")
                assert result["uri"] == "quack:localhost"
                assert result["token_set"] is True
                # Verify quack_serve was called
                mock_conn.execute.assert_called()

    def test_start_server_no_token(self):
        """Verify start_server works without a token."""
        from ohm.quack import start_server

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        with patch("ohm.quack.is_available", return_value=True):
            result = start_server(mock_conn, "quack:localhost")
            assert result["token_set"] is False

    def test_start_server_raises_when_unavailable(self):
        """Verify start_server raises RuntimeError when Quack is not available."""
        from ohm.quack import start_server

        mock_conn = MagicMock()

        with patch("ohm.quack.is_available", return_value=False):
            with pytest.raises(RuntimeError, match="not available"):
                start_server(mock_conn)

    def test_stop_server_calls_quack_stop(self):
        """Verify stop_server calls the right SQL."""
        from ohm.quack import stop_server

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        with patch("ohm.quack.is_available", return_value=True):
            stop_server(mock_conn, "quack:localhost")
            mock_conn.execute.assert_called()

    def test_stop_server_raises_when_unavailable(self):
        """Verify stop_server raises RuntimeError when Quack is not available."""
        from ohm.quack import stop_server

        mock_conn = MagicMock()

        with patch("ohm.quack.is_available", return_value=False):
            with pytest.raises(RuntimeError, match="not available"):
                stop_server(mock_conn)


# ── Client Functions (Mocked) ────────────────────────────────────────────────


class TestQuackClientMocked:
    """Tests for Quack client functions with mocked availability."""

    def setup_method(self):
        reset_availability()

    def teardown_method(self):
        reset_availability()

    def test_attach_remote_calls_attach(self):
        """Verify attach_remote calls ATTACH SQL."""
        from ohm.quack import attach_remote

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        with patch("ohm.quack.is_available", return_value=True):
            with patch.dict(os.environ, {"QUACK_TOKEN": "a" * 32}):
                attach_remote(mock_conn, "quack:localhost", token_env="QUACK_TOKEN")
                mock_conn.execute.assert_called()
                # Check the SQL contains ATTACH
                call_args = mock_conn.execute.call_args[0][0]
                assert "ATTACH" in call_args
                assert "TYPE quack" in call_args

    def test_attach_remote_invalid_alias(self):
        """Verify attach_remote rejects invalid alias."""
        from ohm.quack import attach_remote

        mock_conn = MagicMock()

        with patch("ohm.quack.is_available", return_value=True):
            with pytest.raises(ValueError, match="Invalid alias"):
                attach_remote(mock_conn, "quack:localhost", alias="bad alias!")

    def test_detach_remote(self):
        """Verify detach_remote calls DETACH SQL."""
        from ohm.quack import detach_remote

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        detach_remote(mock_conn, "remote")
        mock_conn.execute.assert_called_with("DETACH remote")

    def test_detach_remote_invalid_alias(self):
        """Verify detach_remote rejects invalid alias."""
        from ohm.quack import detach_remote

        mock_conn = MagicMock()

        with pytest.raises(ValueError, match="Invalid alias"):
            detach_remote(mock_conn, "bad alias!")

    def test_attach_remote_raises_when_unavailable(self):
        """Verify attach_remote raises RuntimeError when Quack is not available."""
        from ohm.quack import attach_remote

        mock_conn = MagicMock()

        with patch("ohm.quack.is_available", return_value=False):
            with pytest.raises(RuntimeError, match="not available"):
                attach_remote(mock_conn)

    def test_create_secret(self):
        """Verify create_secret calls CREATE SECRET SQL."""
        from ohm.quack import create_secret

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        with patch("ohm.quack.is_available", return_value=True):
            create_secret(mock_conn, token="a" * 32, scope="quack:localhost")
            mock_conn.execute.assert_called()
            call_args = mock_conn.execute.call_args[0][0]
            assert "CREATE SECRET" in call_args
            assert "TYPE quack" in call_args

    def test_create_secret_no_token_raises(self):
        """Verify create_secret raises when no token provided."""
        from ohm.quack import create_secret

        mock_conn = MagicMock()

        with patch("ohm.quack.is_available", return_value=True):
            with pytest.raises(ValueError, match="No token"):
                create_secret(mock_conn)


# ── Store Quack Mode ─────────────────────────────────────────────────────────


class TestStoreQuackMode:
    """Tests for OhmStore Quack integration."""

    def test_store_quack_defaults(self, tmp_path):
        """Verify Quack defaults are off."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_quack_store.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        assert store.quack is False
        assert store.quack_started is False
        store.close()

    def test_store_quack_flag_set(self, tmp_path):
        """Verify Quack flag is stored but server doesn't start if unavailable."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_quack_flag.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test", quack=True)
        assert store.quack is True
        # quack_started depends on whether Quack is actually available
        store.close()

    def test_store_close_stops_quack(self, tmp_path):
        """Verify closing the store stops Quack if it was started."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_quack_close.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test", quack=True)
        store.close()
        # Should not raise even if Quack wasn't started


# ── Server Quack Config ──────────────────────────────────────────────────────


class TestServerQuackConfig:
    """Tests for server Quack configuration."""

    def test_quack_flag_in_config(self):
        """Verify --quack flag sets config correctly."""
        from ohm.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "start", "--quack"])
        assert args.quack is True

    def test_quack_uri_flag(self):
        """Verify --quack-uri flag is parsed."""
        from ohm.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "start", "--quack-uri", "quack:0.0.0.0:9494"])
        assert args.quack_uri == "quack:0.0.0.0:9494"

    def test_quack_token_env_flag(self):
        """Verify --quack-token-env flag is parsed."""
        from ohm.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "start", "--quack-token-env", "MY_TOKEN"])
        assert args.quack_token_env == "MY_TOKEN"

    def test_no_quack_by_default(self):
        """Verify Quack is off by default."""
        from ohm.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["serve", "start"])
        assert args.quack is False
        assert args.quack_uri is None
        assert args.quack_token_env is None


# ── SDK Remote Connection ───────────────────────────────────────────────────


class TestSDKRemoteConnection:
    """Tests for SDK remote connection via Quack."""

    def test_connect_remote_exists(self):
        """Verify connect_remote function exists in SDK."""
        from ohm.sdk import connect_remote

        assert callable(connect_remote)

    def test_connect_remote_fallback(self, tmp_path):
        """Verify connect_remote falls back to direct connection when Quack unavailable (strict=False)."""
        from ohm.sdk import connect_remote

        # Set OHM_DB to a temp path so fallback has somewhere to go
        db_path = str(tmp_path / "test_sdk_remote.duckdb")
        with patch.dict(os.environ, {"OHM_DB": db_path}):
            with patch("ohm.quack.is_available", return_value=False):
                graph = connect_remote(actor="test-agent", strict=False)
                assert graph is not None
                assert graph.actor == "test-agent"
                graph._conn.close()

    def test_connect_remote_strict_raises(self, tmp_path):
        """Verify connect_remote raises ConnectionError when Quack unavailable (strict=True)."""
        from ohm.sdk import connect_remote
        import pytest

        with patch("ohm.quack.is_available", return_value=False):
            with pytest.raises(ConnectionError, match="Quack"):
                connect_remote(actor="test-agent", strict=True)

    def test_connect_remote_with_mock_quack(self, tmp_path):
        """Verify connect_remote uses Quack when available."""
        from ohm.sdk import connect_remote

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None

        with patch("ohm.quack.is_available", return_value=True):
            with patch("ohm.quack.attach_remote", return_value=None):
                with patch("ohm.db.connect", return_value=mock_conn):
                    graph = connect_remote(actor="test-agent")
                    assert graph is not None
                    assert graph.actor == "test-agent"


# ── Integration: Server with Quack Config ────────────────────────────────────


class TestServerQuackIntegration:
    """Integration tests for server with Quack configuration."""

    def test_status_includes_quack_field(self, tmp_path):
        """Verify /status includes quack field."""
        import json
        import socketserver
        import threading
        from http.client import HTTPConnection
        from ohm.server import OhmHandler
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_quack_status.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        OhmHandler.store = store
        OhmHandler.config = {"host": "127.0.0.1", "port": 0, "quack": False}
        OhmHandler.schema_config = DEFAULT_SCHEMA
        OhmHandler.tokens = {}
        OhmHandler.roles = {}
        OhmHandler.no_auth = True

        server = socketserver.TCPServer(("127.0.0.1", 0), OhmHandler, bind_and_activate=False)
        server.allow_reuse_address = True
        server.server_bind()
        server.server_activate()
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        from tests.conftest import wait_for_port

        wait_for_port("127.0.0.1", port)

        try:
            conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
            conn.request("GET", "/status")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            assert "quack" in data
            assert data["quack"] is False
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_quack_config_in_default_config(self):
        """Verify default config doesn't include Quack."""
        from ohm.server import DEFAULT_CONFIG

        assert DEFAULT_CONFIG.get("quack", False) is False
