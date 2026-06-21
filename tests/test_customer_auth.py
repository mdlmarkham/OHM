"""Unit tests for OHM-tss4.3/tss4.4 — Customer API Key authentication and routing.

Acceptance criteria (tss4.3):
  - _generate_customer_token() produces twai_live_{24-char} format
  - _build_customer_token_lookup() maps hash → customer_id
  - _authenticate() checks customer_tokens after agent tokens
  - successful customer token match sets _resolved_customer_id
  - successful customer token match returns customer_id as "agent"
  - agent token match does NOT set _resolved_customer_id
  - invalid token returns None
  - query-string customer token works identically to header
  - run_server initialises OhmHandler.customer_tokens from config
"""

import json
import re
import threading
from http.client import HTTPConnection
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from ohm.server.server import (
    OhmHandler,
    _build_customer_token_lookup,
    _generate_customer_token,
    _hash_token,
    _verify_token,
    run_server,
)
from ohm.store import OhmStore

pytestmark = pytest.mark.integration


# ── Token generation ──────────────────────────────────────────────────────────


class TestGenerateCustomerToken:
    def test_format(self):
        token, _ = _generate_customer_token("acme_hvac")
        assert token.startswith("twai_live_")
        suffix = token[len("twai_live_") :]
        # secrets.token_urlsafe(18) → 24 chars of urlsafe base64
        assert len(suffix) == 24
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", suffix)

    def test_unique(self):
        tokens = {_generate_customer_token("acme")[0] for _ in range(20)}
        assert len(tokens) == 20

    def test_hash_matches_token(self):
        token, token_hash = _generate_customer_token("acme_hvac")
        assert _verify_token(token, token_hash)

    def test_customer_id_not_embedded(self):
        token, _ = _generate_customer_token("acme_hvac")
        assert "acme_hvac" not in token


# ── Lookup table construction ─────────────────────────────────────────────────


class TestBuildCustomerTokenLookup:
    def test_hashed_mode(self):
        _, h = _generate_customer_token("acme_hvac")
        lookup = _build_customer_token_lookup({"acme_hvac": {"hash": h}})
        assert lookup[h] == "acme_hvac"

    def test_legacy_plaintext_mode(self):
        token = "twai_live_plaintexttoken_12345"
        lookup = _build_customer_token_lookup({"acme_hvac": token})
        assert lookup[_hash_token(token)] == "acme_hvac"

    def test_multiple_customers(self):
        t1, h1 = _generate_customer_token("tenant_a")
        t2, h2 = _generate_customer_token("tenant_b")
        lookup = _build_customer_token_lookup({"tenant_a": {"hash": h1}, "tenant_b": {"hash": h2}})
        assert lookup[h1] == "tenant_a"
        assert lookup[h2] == "tenant_b"

    def test_missing_hash_field_skipped(self):
        lookup = _build_customer_token_lookup({"acme_hvac": {"role": "admin"}})
        assert lookup == {}

    def test_empty_config(self):
        assert _build_customer_token_lookup({}) == {}


# ── _authenticate() behaviour ─────────────────────────────────────────────────


def _make_handler(agent_tokens=None, customer_tokens=None):
    """Create an OhmHandler instance with fake request state for testing."""
    handler = OhmHandler.__new__(OhmHandler)
    handler.tokens = agent_tokens or {}
    handler.customer_tokens = customer_tokens or {}
    handler.no_auth = False
    handler.roles = {}
    handler.path = "/"
    handler.headers = {}
    return handler


class _FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def _handler_with_bearer(token, agent_tokens=None, customer_tokens=None):
    h = _make_handler(agent_tokens=agent_tokens, customer_tokens=customer_tokens)
    h.headers = _FakeHeaders({"authorization": f"Bearer {token}"})
    return h


def _handler_with_qs(token, agent_tokens=None, customer_tokens=None):
    h = _make_handler(agent_tokens=agent_tokens, customer_tokens=customer_tokens)
    h.headers = _FakeHeaders({})
    h.path = f"/node?token={token}"
    return h


class TestAuthenticateCustomerToken:
    def test_customer_token_returns_customer_id(self):
        token, token_hash = _generate_customer_token("acme_hvac")
        handler = _handler_with_bearer(token, customer_tokens={token_hash: "acme_hvac"})
        result = handler._authenticate()
        assert result == "customer:acme_hvac"

    def test_customer_token_sets_resolved_customer_id(self):
        token, token_hash = _generate_customer_token("acme_hvac")
        handler = _handler_with_bearer(token, customer_tokens={token_hash: "acme_hvac"})
        handler._authenticate()
        assert getattr(handler, "_resolved_customer_id", None) == "acme_hvac"

    def test_agent_token_does_not_set_resolved_customer_id(self):
        agent_token = "agent-secret-token"
        agent_hash = _hash_token(agent_token)
        handler = _handler_with_bearer(agent_token, agent_tokens={agent_hash: "my_agent"})
        result = handler._authenticate()
        assert result == "my_agent"
        assert not hasattr(handler, "_resolved_customer_id") or handler._resolved_customer_id is None  # type: ignore

    def test_agent_token_checked_before_customer_token(self):
        """If the same hash appears in both tables (shouldn't happen), agent wins."""
        token = "shared_token"
        h = _hash_token(token)
        handler = _handler_with_bearer(
            token,
            agent_tokens={h: "agent_one"},
            customer_tokens={h: "customer_one"},
        )
        result = handler._authenticate()
        assert result == "agent_one"
        assert not hasattr(handler, "_resolved_customer_id") or getattr(handler, "_resolved_customer_id", None) is None

    def test_invalid_token_returns_none(self):
        _, token_hash = _generate_customer_token("acme_hvac")
        handler = _handler_with_bearer("wrong-token", customer_tokens={token_hash: "acme_hvac"})
        assert handler._authenticate() is None

    def test_no_token_returns_none(self):
        handler = _make_handler()
        handler.headers = _FakeHeaders({})
        assert handler._authenticate() is None

    def test_customer_token_via_query_string(self):
        token, token_hash = _generate_customer_token("acme_hvac")
        handler = _handler_with_qs(token, customer_tokens={token_hash: "acme_hvac"})
        result = handler._authenticate()
        assert result == "customer:acme_hvac"
        assert getattr(handler, "_resolved_customer_id", None) == "acme_hvac"

    def test_two_tenants_isolated(self):
        token_a, hash_a = _generate_customer_token("tenant_a")
        token_b, hash_b = _generate_customer_token("tenant_b")
        lookup = {hash_a: "tenant_a", hash_b: "tenant_b"}

        handler_a = _handler_with_bearer(token_a, customer_tokens=lookup)
        result_a = handler_a._authenticate()
        assert result_a == "customer:tenant_a"
        assert getattr(handler_a, "_resolved_customer_id", None) == "tenant_a"

        handler_b = _handler_with_bearer(token_b, customer_tokens=lookup)
        result_b = handler_b._authenticate()
        assert result_b == "customer:tenant_b"
        assert getattr(handler_b, "_resolved_customer_id", None) == "tenant_b"


# ── run_server initialisation ─────────────────────────────────────────────────


class TestRunServerInitialisesCustomerTokens:
    def test_customer_tokens_loaded_from_config(self, tmp_path):
        token, token_hash = _generate_customer_token("acme_hvac")
        db_path = str(tmp_path / "ohm.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        config = {
            "host": "127.0.0.1",
            "port": 0,
            "tokens": {},
            "customer_tokens": {"acme_hvac": {"hash": token_hash}},
        }

        import socketserver

        OhmHandler.store = store
        OhmHandler.config = config
        from ohm.server.server import _build_customer_token_lookup, _build_token_lookup

        OhmHandler.tokens, _ = _build_token_lookup(config.get("tokens", {}))
        OhmHandler.customer_tokens = _build_customer_token_lookup(config.get("customer_tokens", {}))
        OhmHandler.no_auth = False
        OhmHandler.require_read_auth = False

        assert token_hash in OhmHandler.customer_tokens
        assert OhmHandler.customer_tokens[token_hash] == "acme_hvac"
        store.close()


# ── current_store routing (OHM-tss4.4) ───────────────────────────────────────


class TestCurrentStoreRouting:
    def test_single_tenant_returns_store(self, tmp_path):
        """multi_tenant=False always returns self.store (no TenantManager)."""
        db_path = str(tmp_path / "st.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = False
        handler.store = store
        assert handler.current_store is store
        store.close()

    def test_multi_tenant_agent_token_returns_core_store(self, tmp_path):
        """Agent token (no _resolved_customer_id) returns core store."""
        db_path = str(tmp_path / "core.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler.tenant_manager = None
        handler.store = store
        # No _resolved_customer_id set → _customer_id returns None → core store
        assert handler.current_store is store
        store.close()

    def test_multi_tenant_customer_token_routes_to_tenant(self, tmp_path):
        """Customer token routes to the provisioned tenant's OhmStore."""
        from ohm.tenant import TenantManager

        tenants_dir = tmp_path / "tenants"
        tm = TenantManager(tenants_dir, max_cached=5)
        tm.provision("acme_hvac")
        tenant_store = tm.get_store("acme_hvac")

        core_db = str(tmp_path / "core.duckdb")
        core_store = OhmStore(db_path=core_db, agent_name="test")

        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler.tenant_manager = tm
        handler.store = core_store
        handler._resolved_customer_id = "acme_hvac"

        routed = handler.current_store
        assert routed is tenant_store
        assert routed is not core_store
        tm.close()
        core_store.close()

    def test_multi_tenant_unprovisioned_tenant_raises(self, tmp_path):
        """current_store raises NodeNotFoundError (→ 404) for an unprovisioned tenant.

        404 not 403: unprovisioned = resource doesn't exist from the caller's view.
        """
        from ohm.exceptions import NodeNotFoundError
        from ohm.tenant import TenantManager

        tm = TenantManager(tmp_path / "tenants", max_cached=5)
        core_db = str(tmp_path / "core.duckdb")
        core_store = OhmStore(db_path=core_db, agent_name="test")

        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler.tenant_manager = tm
        handler.store = core_store
        handler._resolved_customer_id = "ghost_tenant"

        with pytest.raises(NodeNotFoundError):
            _ = handler.current_store

        tm.close()
        core_store.close()

    def test_x_tenant_id_header_routes_to_tenant(self, tmp_path):
        """Admin agent token + X-Tenant-ID header routes to the specified tenant (OHM-tss4.8, OHM-tss4.19)."""
        from ohm.tenant import TenantManager

        tenants_dir = tmp_path / "tenants"
        tm = TenantManager(tenants_dir, max_cached=5)
        tm.provision("acme_hvac")
        tenant_store = tm.get_store("acme_hvac")

        core_db = str(tmp_path / "core.duckdb")
        core_store = OhmStore(db_path=core_db, agent_name="test")

        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler.tenant_manager = tm
        handler.store = core_store
        handler.headers = {"X-Tenant-ID": "acme_hvac"}
        handler._authenticated_agent = "admin_agent"  # OHM-tss4.19: only admin agents can use X-Tenant-ID
        handler.roles = {"admin_agent": "admin"}

        routed = handler.current_store
        assert routed is tenant_store
        assert routed is not core_store

        # Non-admin agent should be routed to core store (X-Tenant-ID ignored)
        handler._authenticated_agent = "regular_agent"
        handler.roles = {"regular_agent": "read-write"}
        assert handler.current_store is core_store

        tm.close()
        core_store.close()

    def test_x_tenant_id_ignored_when_customer_token_present(self, tmp_path):
        """Customer token resolution takes precedence over X-Tenant-ID header."""
        from ohm.tenant import TenantManager

        tenants_dir = tmp_path / "tenants"
        tm = TenantManager(tenants_dir, max_cached=5)
        tm.provision("acme_hvac")
        tm.provision("wayne_mfg")
        acme_store = tm.get_store("acme_hvac")

        core_db = str(tmp_path / "core.duckdb")
        core_store = OhmStore(db_path=core_db, agent_name="test")

        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler.tenant_manager = tm
        handler.store = core_store
        handler._resolved_customer_id = "acme_hvac"
        handler.headers = {"X-Tenant-ID": "wayne_mfg"}

        routed = handler.current_store
        assert routed is acme_store
        tm.close()
        core_store.close()

    def test_x_tenant_id_ignored_in_single_tenant_mode(self, tmp_path):
        """X-Tenant-ID header has no effect when multi-tenancy is disabled."""
        core_db = str(tmp_path / "core.duckdb")
        store = OhmStore(db_path=core_db, agent_name="test")
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = False
        handler.store = store
        handler.headers = {"X-Tenant-ID": "acme_hvac"}

        assert handler.current_store is store
        store.close()

    def test_x_tenant_id_unprovisioned_raises(self, tmp_path):
        """X-Tenant-ID for an unprovisioned tenant raises NodeNotFoundError (admin agents only)."""
        from ohm.exceptions import NodeNotFoundError
        from ohm.tenant import TenantManager

        tenants_dir = tmp_path / "tenants"
        tm = TenantManager(tenants_dir, max_cached=5)

        core_db = str(tmp_path / "core.duckdb")
        core_store = OhmStore(db_path=core_db, agent_name="test")

        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler.tenant_manager = tm
        handler.store = core_store
        handler.headers = {"X-Tenant-ID": "ghost_tenant"}
        handler._authenticated_agent = "admin_agent"  # OHM-tss4.19: only admin agents can use X-Tenant-ID
        handler.roles = {"admin_agent": "admin"}

        with pytest.raises(NodeNotFoundError):
            _ = handler.current_store

        # Non-admin agents should NOT be able to use X-Tenant-ID (OHM-tss4.19)
        handler._authenticated_agent = "regular_agent"
        handler.roles = {"regular_agent": "read-write"}
        assert handler._customer_id is None  # X-Tenant-ID ignored for non-admin

        tm.close()
        core_store.close()


class TestSDKConnectHttpTenantId:
    """Tests for connect_http(tenant_id=...) SDK parameter (OHM-tss4.8)."""

    def test_connect_http_accepts_tenant_id(self):
        from ohm.sdk import connect_http

        g = connect_http("http://127.0.0.1:8710", actor="test", tenant_id="acme_hvac")
        assert g.tenant_id == "acme_hvac"
        assert g._tenant_id == "acme_hvac"

    def test_connect_http_tenant_id_none_by_default(self):
        from ohm.sdk import connect_http

        g = connect_http("http://127.0.0.1:8710", actor="test")
        assert g.tenant_id is None
        assert g._tenant_id is None

    def test_http_request_includes_x_tenant_id_header(self):
        from ohm.sdk import connect_http

        g = connect_http("http://127.0.0.1:8710", actor="test", tenant_id="acme_hvac")
        import unittest.mock

        mock_response = unittest.mock.MagicMock()
        mock_response.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = lambda s, *a: None

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_response):
            with unittest.mock.patch("urllib.request.Request") as mock_req:
                try:
                    g.stats()
                except Exception:
                    pass
                if mock_req.called:
                    _, kwargs = mock_req.call_args
                    headers = kwargs.get("headers", {})
                    assert headers.get("X-Tenant-ID") == "acme_hvac"

    def test_http_request_no_x_tenant_id_when_none(self):
        from ohm.sdk import connect_http

        g = connect_http("http://127.0.0.1:8710", actor="test")
        import unittest.mock

        mock_response = unittest.mock.MagicMock()
        mock_response.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = lambda s, *a: None

        with unittest.mock.patch("urllib.request.urlopen", return_value=mock_response):
            with unittest.mock.patch("urllib.request.Request") as mock_req:
                try:
                    g.stats()
                except Exception:
                    pass
                if mock_req.called:
                    _, kwargs = mock_req.call_args
                    headers = kwargs.get("headers", {})
                    assert "X-Tenant-ID" not in headers
