"""Tests for the 2026-07-02 security hardening (OHM-7jj2).

Covers:
- validate_table_name (H1): bare-SQL-identifier guard for table interpolation.
- Auth rate limiting (M2): per-IP brute-force lockout.
- Webhook SSRF (M4): _resolve_webhook_ips rejects private/loopback targets.
- Hook admin gate (H3): non-admin agents cannot register hooks in auth mode.
- CORS no-op (M1): _set_extra_cors_headers is a safe no-op for agent-only access.
"""

import ipaddress

import pytest

import ohm.server.server as srv
from ohm.exceptions import AuthenticationError, PermissionDeniedError, ValidationError
from ohm.validation import canonicalize_ip, validate_backup_id, validate_table_name


class TestValidateTableName:
    """H1: table names used in f-string interpolation must be bare identifiers."""

    @pytest.mark.parametrize("value", ["ohm_nodes", "ohm_edges", "topo_prospects", "_x", "a1", "ohm_observations"])
    def test_accepts_bare_identifiers(self, value):
        assert validate_table_name(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "foo.bar",
            "sys.tables",
            "ohm_nodes; DROP",
            "'; --",
            "has space",
            "my-table",
            "1abc",
            "",
        ],
    )
    def test_rejects_unsafe(self, value):
        with pytest.raises(ValueError, match="Invalid table"):
            validate_table_name(value)

    def test_custom_name_in_error(self):
        with pytest.raises(ValueError, match="Invalid alias"):
            validate_table_name("foo.bar", name="alias")


class TestAuthRateLimit:
    """M2: per-IP brute-force protection for authentication."""

    def _reset(self, ip):
        srv._auth_failures.pop(ip, None)
        srv._auth_lockout.pop(ip, None)

    def test_under_threshold_does_not_lock(self, monkeypatch):
        ip = "10.20.30.40"
        self._reset(ip)
        monkeypatch.setattr(srv, "_AUTH_FAIL_THRESHOLD", 5)
        for _ in range(4):
            srv._record_auth_failure(ip)
        srv._check_auth_rate_limit(ip)
        self._reset(ip)

    def test_threshold_locks_out(self, monkeypatch):
        ip = "10.20.30.41"
        self._reset(ip)
        monkeypatch.setattr(srv, "_AUTH_FAIL_THRESHOLD", 3)
        for _ in range(3):
            srv._record_auth_failure(ip)
        with pytest.raises(AuthenticationError, match="Too many failed"):
            srv._check_auth_rate_limit(ip)
        self._reset(ip)

    def test_clear_resets_lockout(self, monkeypatch):
        ip = "10.20.30.42"
        self._reset(ip)
        monkeypatch.setattr(srv, "_AUTH_FAIL_THRESHOLD", 3)
        for _ in range(3):
            srv._record_auth_failure(ip)
        with pytest.raises(AuthenticationError):
            srv._check_auth_rate_limit(ip)
        srv._clear_auth_failures(ip)
        srv._check_auth_rate_limit(ip)
        self._reset(ip)

    def test_none_ip_is_noop(self):
        srv._record_auth_failure(None)
        srv._check_auth_rate_limit(None)
        srv._clear_auth_failures(None)


class TestResolveWebhookIps:
    """M4: SSRF guard rejects private/loopback targets at resolve time."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "::1", "10.0.0.1", "192.168.1.1"])
    def test_rejects_private(self, host):
        with pytest.raises(ValidationError, match="private"):
            srv._resolve_webhook_ips(host)

    @pytest.mark.parametrize(
        "host",
        [
            "::ffff:169.254.169.254",  # IPv4-mapped link-local (AWS metadata)
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:10.0.0.1",  # IPv4-mapped private
        ],
    )
    def test_rejects_ipv4_mapped_ipv6(self, host):
        """IPv4-mapped IPv6 literals must not bypass the IPv4 blocklist (SSRF)."""
        with pytest.raises(ValidationError, match="private"):
            srv._resolve_webhook_ips(host)

    def test_validate_rejects_non_http_scheme(self):
        with pytest.raises(ValidationError, match="http or https"):
            srv._validate_webhook_url("ftp://example.com/hook")

    def test_validate_rejects_missing_host(self):
        with pytest.raises(ValidationError, match="missing host"):
            srv._validate_webhook_url("http:///path")


class TestCanonicalizeIp:
    """IPv4-mapped / NAT64 IPv6 addresses collapse to their embedded IPv4 so
    that SSRF blocklists (which mix IPv4 and IPv6 networks) cannot be bypassed
    by ipaddress's cross-family membership check silently returning False."""

    def test_ipv4_mapped_collapses_to_ipv4(self):
        canon = canonicalize_ip(ipaddress.ip_address("::ffff:169.254.169.254"))
        assert canon == ipaddress.ip_address("169.254.169.254")
        assert canon in ipaddress.ip_network("169.254.0.0/16")

    def test_nat64_collapses_to_ipv4(self):
        # 64:ff9b::/96 embeds the IPv4 in the low 32 bits (7f00:0001 = 127.0.0.1)
        assert canonicalize_ip(ipaddress.ip_address("64:ff9b::7f00:1")) == ipaddress.ip_address("127.0.0.1")

    def test_genuine_ipv6_unchanged(self):
        for addr in ("::1", "fc00::1", "fe80::1", "2606:4700:4700::1111"):
            assert canonicalize_ip(ipaddress.ip_address(addr)) == ipaddress.ip_address(addr)

    def test_genuine_ipv4_unchanged(self):
        assert canonicalize_ip(ipaddress.ip_address("8.8.8.8")) == ipaddress.ip_address("8.8.8.8")


class TestValidateBackupId:
    """backup_id is joined into a filesystem path in restore_tenant and must be
    validated to prevent path traversal (it arrives from request bodies)."""

    @pytest.mark.parametrize("value", ["20260524T180000Z", "20260524T180000Z_a1b2c3", "pre_restore-1"])
    def test_accepts_generated_ids(self, value):
        assert validate_backup_id(value) == value

    @pytest.mark.parametrize(
        "value",
        ["../../etc", "a/b", "a\\b", "..", "foo/../bar", "", "with space", "semi;colon"],
    )
    def test_rejects_traversal_and_unsafe(self, value):
        with pytest.raises(ValueError):
            validate_backup_id(value)


class TestHookAdminGate:
    """H3: hook registration requires admin role when auth is enabled."""

    def _make(self, **kw):
        h = srv.OhmHandler.__new__(srv.OhmHandler)
        h.no_auth = kw.get("no_auth", True)
        h.roles = kw.get("roles", {})
        h.multi_tenant = False
        return h

    def test_non_admin_rejected_in_auth_mode(self):
        handler = self._make(no_auth=False, roles={"metis": "read-write"})
        with pytest.raises(PermissionDeniedError, match="admin"):
            handler._post_hooks("/hooks", {}, {"event": "pre_ingest", "command": "python3 -c pass"}, "metis")

    def test_no_auth_skips_gate(self):
        handler = self._make(no_auth=True, roles={"metis": "read-write"})
        with pytest.raises(Exception) as exc_info:
            handler._post_hooks("/hooks", {}, {"event": "pre_ingest", "command": "python3 -c pass"}, "metis")
        assert not isinstance(exc_info.value, PermissionDeniedError)

    def test_admin_passes_gate(self):
        handler = self._make(no_auth=False, roles={"boss": "admin"})
        with pytest.raises(Exception) as exc_info:
            handler._post_hooks("/hooks", {}, {"event": "pre_ingest", "command": "python3 -c pass"}, "boss")
        assert not isinstance(exc_info.value, PermissionDeniedError)


class TestCorsNoOp:
    """M1: _set_extra_cors_headers is a safe no-op (fixes latent AttributeError)."""

    def test_is_safe_noop(self):
        handler = srv.OhmHandler.__new__(srv.OhmHandler)
        handler._set_extra_cors_headers()


@pytest.mark.integration
class TestAdminEndpointGate:
    """Destructive /admin/* POST endpoints require the admin role, not merely
    write access. Normal agent writes use /node, /edge, /observe — never
    /admin/*. Gated centrally in _do_POST."""

    @pytest.fixture
    def admin_server(self, tmp_path):
        from tests.conftest import _start_test_server
        from ohm.graph.embeddings import NullBackend
        from ohm.store import OhmStore

        store = OhmStore(
            db_path=str(tmp_path / "test_admin_gate.duckdb"),
            agent_name="test_agent",
            embedding_backend=NullBackend(dimensions=768),
        )
        tokens = {"rw-token": "worker", "admin-token": "boss"}
        roles = {"worker": "read-write", "boss": "admin"}
        port, server, thread = _start_test_server(store, tokens=tokens, roles=roles)
        yield port
        server.shutdown()
        thread.join(timeout=2)
        store.close()

    def test_read_write_agent_rejected(self, admin_server):
        from tests.conftest import _request

        status, data = _request("POST", admin_server, "/admin/apply-decay", body={"dry_run": True}, token="rw-token")
        assert status == 403, f"expected 403 for read-write agent, got {status}: {data}"

    def test_admin_agent_allowed(self, admin_server):
        from tests.conftest import _request

        status, data = _request("POST", admin_server, "/admin/apply-decay", body={"dry_run": True}, token="admin-token")
        assert status != 403, f"admin agent should not be blocked, got 403: {data}"
