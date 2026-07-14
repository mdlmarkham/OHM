"""
OHM Daemon — HTTP server for multi-agent shared access to the knowledge graph.

Uses Quack (DuckDB client-server protocol) for concurrent access with
token-based authentication and per-agent role enforcement.
"""

import argparse
import collections
import hashlib
import ipaddress
import json
import os
import secrets
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any, Optional
import logging

from ohm.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    EdgeNotFoundError,
    NodeNotFoundError,
    OHMError,
    PermissionDeniedError,
    ValidationError,
)
from ohm.schema import DEFAULT_SCHEMA, SchemaConfig, VALID_VISIBILITIES
from ohm.store import OhmStore


# ── Configuration ──────────────────────────────────────────

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8710,
    "db_path": str(Path.home() / ".ohm" / "ohm.duckdb"),
    "tokens": {
        # agent_name: token_string
        # Populated from config file or env vars
    },
    "customer_tokens": {
        # customer_id: {"hash": "sha256_hex"}
        # Generated via --init-customer-token; plaintext never stored
    },
    "log_level": "INFO",
    "multi_tenant": False,
    "semantic_layer": {
        # OHM-wx42: automatic semantic-layer metric actions.
        "auto_actions_enabled": False,  # default disabled
        "auto_actions_interval_seconds": 3600,  # run every 1 hour when enabled
        "auto_actions_rate_limit_seconds": 86400,  # dedup window: 24 hours
    },
    "ducklake": {
        "path": "",  # DuckLake catalog path (e.g., /var/lib/ohm/ohm_lake.ducklake)
        "data_path": "",  # Parquet data path (e.g., /var/lib/ohm/ohm_lake_data)
        "sync_interval_seconds": 60,  # How often to sync to DuckLake
    },
    "beads_sync": {
        # OHM-sbtz: background Beads->OHM task sync so agents
        # see their assigned work via /tasks?assigned_to=...
        # without an operator having to POST /admin/sync-beads.
        "enabled": True,
        "interval_seconds": 60,
        "startup_sync": True,  # one-shot sync at server boot
    },
    "bedrock": {
        "knowledge_base_id": "",  # OHM_BEDROCK_KB_ID env var
        "data_source_id": "",  # OHM_BEDROCK_DATA_SOURCE_ID env var
        "region": "us-east-1",  # AWS_REGION / OHM_BEDROCK_REGION
    },
}

_START_TIME = time.time()
logger = logging.getLogger(__name__)


def _prewarm_pgmpy() -> None:
    """Pre-warm pgmpy imports to avoid 5.3s cold-import penalty on first inference call.

    Called at daemon startup after DB initialization but before accepting connections.
    Subsequent calls are no-ops (Python's module cache).
    """
    try:
        from pgmpy.inference import VariableElimination  # noqa: F401
        from pgmpy.models import BayesianNetwork  # noqa: F401
        from pgmpy.factors.discrete import TabularCPD  # noqa: F401

        logger.debug("pgmpy pre-warmed (imports loaded)")
    except ImportError:
        logger.info("pgmpy not available — Bayesian inference will be disabled")


def _prewarm_pgmpy_async() -> None:
    """Start pgmpy pre-warming in a daemon thread so /health can respond sooner."""
    thread = threading.Thread(target=_prewarm_pgmpy, daemon=True, name="pgmpy-prewarm")
    thread.start()


def run_metric_actions_heartbeat(conn: Any, repo_path: str | None = None) -> dict[str, Any]:
    """Run semantic-layer metrics and execute threshold actions.

    Intended to be called from the daemon's background scheduler or heartbeat
    loop. Records the last execution time of each action in
    `ohm_metric_action_log` so the same (metric, threshold, action_type) does
    not create duplicate tasks within the configured window.

    Args:
        conn: DuckDB connection with OHM schema.
        repo_path: Optional Beads repo path for `create_task` actions.

    Returns:
        Dict with 'metrics', 'actions', and 'executed'.
    """
    from ohm.semantic_layer import run_metrics_and_actions

    repo = repo_path or "/root/olympus/OHM"
    return run_metrics_and_actions(
        conn,
        repo_path=repo,
        execute=True,
        use_ibis=False,
        rate_limit_window_seconds=DEFAULT_CONFIG["semantic_layer"]["auto_actions_rate_limit_seconds"],
    )


def _register_builtin_hooks(store: OhmStore) -> None:
    """Register built-in pre_ingest hooks on startup (OHM-aznh.11).

    Idempotent — if the hook already exists (same event + command + created_by),
    it is not duplicated.
    """

    builtins = [
        ("pre_ingest", "python:ohm.hooks_builtin.cross_link_check", "system"),
        ("pre_ingest", "python:ohm.hooks_builtin.source_url_required", "system"),
        ("pre_ingest", "python:ohm.hooks_builtin.observation_source_required", "system"),
        ("post_event_create", "python:ohm.hooks_builtin.propagate_on_event", "system"),
    ]
    for event, command, created_by in builtins:
        existing = store.conn.execute(
            "SELECT id FROM ohm_hooks WHERE event = ? AND command = ? AND created_by = ?",
            [event, command, created_by],
        ).fetchone()
        if existing is None:
            store.conn.execute(
                "INSERT INTO ohm_hooks (event, command, created_by) VALUES (?, ?, ?)",
                [event, command, created_by],
            )
            logger.debug("Registered built-in hook: %s (%s)", command, event)


def _do_beads_sync(
    conn,
    actor: str = "system",
) -> dict[str, Any] | None:
    """One-shot Beads→OHM task sync (OHM-sbtz).

    Fetches issues via the ``bd`` CLI (with a .beads/issues.jsonl
    fallback) and mirrors assigned ones into OHM task nodes so
    ``GET /tasks?assigned_to=<agent>`` returns the right work
    without the operator having to POST /admin/sync-beads every
    time. Returns the sync report on success, None on failure
    (already logged). Skipped silently if the beads integration
    module is not importable.

    Extracted from ``run_server`` to be unit-testable without
    spinning up a full daemon.
    """
    try:
        from ohm.integrations.beads_sync import (
            fetch_beads_issues,
            sync_beads_to_ohm_tasks,
        )
    except ImportError as exc:
        logger.debug("Beads sync skipped — module not importable: %s", exc)
        return None
    try:
        issues = fetch_beads_issues()
        logger.info("Beads sync: fetched %d issues", len(issues))
    except Exception:
        logger.exception("Beads sync: fetch_beads_issues failed")
        return None
    try:
        report = sync_beads_to_ohm_tasks(conn, issues, actor=actor)
        logger.info(
            "Beads sync report: %d created, %d updated, %d skipped, %d errors (of %d)",
            report.get("created", 0),
            report.get("updated", 0),
            report.get("skipped", 0),
            len(report.get("errors", [])),
            report.get("total", 0),
        )
        if report.get("errors"):
            for err in report["errors"][:5]:
                logger.warning("Beads sync error: %s", err)
        return report
    except Exception:
        logger.exception("Beads sync: sync_beads_to_ohm_tasks failed")
        return None


# ── Security Constants ─────────────────────────────────────

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB — reject bodies larger than this
MAX_BATCH_SIZE = 500  # Maximum nodes + edges per /batch request
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 5000  # per window per IP

# Simple in-memory rate limiter: {(ip, customer_id): [(timestamp, ...)]}
# Keyed by (client_ip, customer_id) — provides DDoS/abuse protection at the
# network level AND per-tenant quota isolation (OHM-1s14.4). customer_id=None
# means single-tenant or operator-scope (agent token) requests.
# Per-customer quota enforcement (resource-level, not rate-level) is tracked
# separately in TenantManager.check_quota (OHM-982m).
_rate_limit_store: dict[tuple[str, str | None], list[float]] = {}
_rate_limit_lock = threading.Lock()

# ── Metrics ────────────────────────────────────────────────

# Simple in-memory metrics collector
_metrics: dict[str, int] = {
    "requests_total": 0,
    "requests_get": 0,
    "requests_post": 0,
    "errors_4xx": 0,
    "errors_5xx": 0,
    "rate_limited": 0,
}
_metrics_lock = threading.Lock()
_request_latencies: collections.deque = collections.deque(maxlen=1000)

# OHM-lqpk.5: per-endpoint latency tracking
_endpoint_latencies: dict[str, collections.deque] = {}
_endpoint_counts: dict[str, int] = {}
_perf_log_file: str | None = os.environ.get("OHM_PERF_LOG", None)
if _perf_log_file == "":
    _perf_log_file = None


def _normalize_perf_path(path: str) -> str:
    """Normalize a request path for perf logging by replacing UUIDs and IDs with placeholders.

    Prevents cardinality explosion in /perf endpoint when paths contain literal
    node/edge IDs (OHM-cbui).

    Examples:
        /node/abc-123-def → /node/{id}
        /edge/uuid-here → /edge/{id}
        /neighborhood/some-id → /neighborhood/{id}
        /runbook/my-runbook/steps → /runbook/{id}/steps
    """
    import re as _re

    # Replace UUIDs (standard hex UUID format with dashes)
    path = _re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{id}", path, flags=_re.IGNORECASE)
    # Replace short hex IDs with dashes (like abc-123-def-456)
    path = _re.sub(r"/[a-z0-9]+(?:-[a-z0-9]+)+", "/{id}", path, flags=_re.IGNORECASE)
    # Replace node_ prefix IDs
    path = _re.sub(r"/node_[a-z0-9_]+", "/{id}", path, flags=_re.IGNORECASE)
    # Replace edge_ prefix IDs
    path = _re.sub(r"/edge_[a-z0-9_]+", "/{id}", path, flags=_re.IGNORECASE)
    # Replace nudge_ prefix IDs
    path = _re.sub(r"/nudge_[a-z0-9_]+", "/{id}", path, flags=_re.IGNORECASE)

    return path


def _record_endpoint_latency(method: str, path: str, elapsed_ms: float, status: int) -> None:
    """Record per-endpoint latency for perf analysis (OHM-lqpk.5).

    Maintains a per-endpoint deque of the last 500 latencies and a
    per-endpoint request count. When OHM_PERF_LOG is set to a file path,
    writes a structured JSON line per request for offline analysis.
    """
    from urllib.parse import urlparse as _up

    endpoint = _up(path).path.rstrip("/") or "/"
    endpoint = _normalize_perf_path(endpoint)
    key = f"{method} {endpoint}"
    with _metrics_lock:
        if key not in _endpoint_latencies:
            _endpoint_latencies[key] = collections.deque(maxlen=500)
        _endpoint_latencies[key].append(elapsed_ms)
        _endpoint_counts[key] = _endpoint_counts.get(key, 0) + 1
    if _perf_log_file:
        try:
            import json as _json

            with open(_perf_log_file, "a", encoding="utf-8") as f:
                f.write(_json.dumps({"method": method, "path": endpoint, "ms": round(elapsed_ms, 2), "status": status, "ts": time.time()}) + "\n")
        except OSError:
            pass


# ── Webhook Registry ──────────────────────────────────────

# In-memory registry: {customer_id: {agent_name: {"url": str, "events": list[str]}}}
# Nested by customer_id so webhooks only fire within the originating tenant.
# customer_id=None is the single-tenant default (backward compat with pre-MT deployments).
_webhook_registry: dict[str | None, dict[str, dict]] = {}
_webhook_lock = threading.Lock()


# ── SSE Subscriber Registry ──────────────────────────────────────────────────

# In-memory registry: {subscription_id: {"agent_name": str, "since": str, ..., "customer_id": str|None}}
# SSE subscribers receive change feed events from their own tenant's store only.
# customer_id is stored for audit; actual isolation is enforced by routing each
# handler to the correct OhmStore (tss4.4). customer_id=None = single-tenant default.
_sse_subscribers: dict[str, dict] = {}
_sse_lock = threading.Lock()

# Guards mutations to OhmHandler.customer_tokens (class-level dict shared across threads).
_customer_tokens_lock = threading.Lock()


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return True if *ip* falls in any of the private/loopback networks."""
    return any(ip in net for net in _PRIVATE_NETWORKS)


def _resolve_webhook_ips(host: str) -> list[str]:
    """Resolve *host* and reject any private/loopback address (SSRF guard).

    Returns the deduplicated list of resolved address strings (order
    preserved). Raises ``ValidationError`` if the host is unresolvable or
    resolves to *any* private/loopback address.

    Called at both registration and delivery time so DNS rebinding — a
    public address at registration that re-resolves to a private address
    at delivery — is caught at the delivery boundary.
    """
    import socket

    from ohm.framework.validation import canonicalize_ip

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise ValidationError(f"Cannot resolve webhook host: {host!r}")

    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr in seen:
            continue
        # Canonicalize IPv4-mapped/NAT64 IPv6 to IPv4 so mapped literals like
        # ::ffff:169.254.169.254 cannot bypass the IPv4 blocklist entries.
        ip = canonicalize_ip(ipaddress.ip_address(addr))
        if _is_private_ip(ip):
            raise ValidationError(f"Webhook URL targets a private/loopback address ({addr}) — SSRF not allowed")
        seen.append(addr)
    if not seen:
        raise ValidationError(f"Cannot resolve webhook host: {host!r}")
    return seen


def _validate_webhook_url(url: str) -> None:
    """Reject webhook URLs that could enable SSRF attacks.

    Raises ValidationError for non-http(s) schemes and private/loopback targets.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(f"Webhook URL must use http or https scheme, got: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValidationError("Webhook URL missing host")
    _resolve_webhook_ips(host)


def _deliver_webhook(url: str, event: dict, timeout: float = 5.0) -> bool:
    """Deliver a webhook event to a callback URL.

    Uses HTTP POST with JSON body. Returns True on success, False on failure.
    Failures are logged but not raised — webhooks are fire-and-forget.

    SSRF / DNS-rebinding: the host is resolved and validated again at
    delivery time (not just at registration). For HTTP the connection is
    pinned to a validated IP so a hostname that re-resolves to a private
    address between validation and connect cannot reach it.
    """
    import http.client
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        ips = _resolve_webhook_ips(host)  # raises ValidationError on private/unresolvable
        pinned_ip = ips[0]
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query
        body = json.dumps(event).encode("utf-8")
        headers = {"Content-Type": "application/json", "Host": host}
        if parsed.scheme == "http":
            # Pin the TCP connection to the validated IP (full rebinding mitigation).
            conn = http.client.HTTPConnection(pinned_ip, port, timeout=timeout)
            try:
                conn.request("POST", request_path, body=body, headers=headers)
                resp = conn.getresponse()
                return resp.status in (200, 201, 202, 204)
            finally:
                conn.close()
        # HTTPS: re-validated above; TLS certificate verification against the
        # original hostname provides additional rebinding protection.
        import urllib.request

        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 201, 202, 204)
    except Exception:
        return False


def _trigger_webhooks(event: dict, customer_id: str | None = None) -> None:
    """Trigger webhooks for agents registered in customer_id's tenant.

    Events are delivered asynchronously to avoid blocking the request that
    triggered them. Only webhooks registered under the same customer_id
    receive the event — preventing cross-tenant event leakage.
    customer_id=None selects the single-tenant default bucket.
    """
    import concurrent.futures

    event_type = event.get("type", "")

    def deliver_to_agent(agent_name: str, config: dict) -> None:
        url = config.get("url", "")
        events = config.get("events", [])
        if url and (event_type in events or "*" in events):
            _deliver_webhook(url, event)

    # Snapshot only the customer's bucket under lock, then deliver without holding the lock
    with _webhook_lock:
        tenant_registry = dict(_webhook_registry.get(customer_id, {}))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for agent_name, config in tenant_registry.items():
            executor.submit(deliver_to_agent, agent_name, config)


# ── Token Security ──────────────────────────────────────────


def _hash_token(token: str) -> str:
    """Hash a token using SHA-256 for storage.

    Tokens are never stored in plaintext. The hash is one-way —
    the original token is only shown once at creation time.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify_token(provided: str, token_hash: str) -> bool:
    """Constant-time comparison of a provided token against a stored hash.

    Uses secrets.compare_digest to prevent timing attacks.
    Hashes the provided token first, then compares the hashes.
    """
    return secrets.compare_digest(_hash_token(provided), token_hash)


# ── Auth rate limiting (M2) ───────────────────────────────────
# Per-IP brute-force protection for authentication. A client that exceeds
# _AUTH_FAIL_THRESHOLD failed attempts within _AUTH_FAIL_WINDOW_SEC is
# locked out for _AUTH_LOCKOUT_SEC. Tunable via environment.
_AUTH_FAIL_THRESHOLD = int(os.environ.get("OHM_AUTH_FAIL_THRESHOLD", "10"))
_AUTH_FAIL_WINDOW_SEC = int(os.environ.get("OHM_AUTH_FAIL_WINDOW_SEC", "300"))
_AUTH_LOCKOUT_SEC = int(os.environ.get("OHM_AUTH_LOCKOUT_SEC", "900"))

_auth_failures: dict[str, collections.deque] = {}
_auth_lockout: dict[str, float] = {}
_auth_failures_lock = threading.Lock()


def _check_auth_rate_limit(client_ip: str | None) -> None:
    """Raise AuthenticationError if *client_ip* is currently locked out."""
    if not client_ip:
        return
    now = time.monotonic()
    with _auth_failures_lock:
        until = _auth_lockout.get(client_ip)
        if until is not None:
            if now < until:
                raise AuthenticationError(f"Too many failed authentication attempts from {client_ip} — try again later")
            _auth_lockout.pop(client_ip, None)
            _auth_failures.pop(client_ip, None)


def _record_auth_failure(client_ip: str | None) -> None:
    """Record a failed authentication attempt and lock out if threshold reached."""
    if not client_ip:
        return
    now = time.monotonic()
    with _auth_failures_lock:
        failures = _auth_failures.get(client_ip)
        if failures is None:
            failures = collections.deque()
            _auth_failures[client_ip] = failures
        failures.append(now)
        cutoff = now - _AUTH_FAIL_WINDOW_SEC
        while failures and failures[0] < cutoff:
            failures.popleft()
        if len(failures) >= _AUTH_FAIL_THRESHOLD:
            _auth_lockout[client_ip] = now + _AUTH_LOCKOUT_SEC


def _clear_auth_failures(client_ip: str | None) -> None:
    """Clear recorded auth failures for *client_ip* (called on successful auth)."""
    if not client_ip:
        return
    with _auth_failures_lock:
        _auth_failures.pop(client_ip, None)
        _auth_lockout.pop(client_ip, None)


def _lookup_role(roles: dict, agent: str, customer_id: str | None = None) -> str:
    """Resolve an agent's role from either flat or customer-scoped role dict.

    OHM-1s14.2: roles may be scoped per tenant to prevent collisions when
    two tenants reuse the same agent name (e.g., both have an "admin" agent
    with different privileges). Supports two formats transparently:

      * Flat (legacy / single-tenant)::

          {"metis": "read-write", "admin": "admin"}

      * Scoped (multi-tenant)::

          {"": {"metis": "read-write"},            # operator scope
           "acme_hvac": {"admin": "admin"},         # tenant scope
           "bos_inc": {"admin": "read-only"}}

    The empty-string key ``""`` is the operator/global scope used for
    agent tokens that are not tied to a specific tenant. When a customer
    has no explicit role entry for the agent, the operator scope is used
    as fallback so operator-level agents (e.g., metis) keep working
    across tenants.

    Args:
        roles: Role dict (flat or scoped).
        agent: Agent name from token lookup.
        customer_id: Tenant id from ``_customer_id`` property, or None /
            empty string for the operator scope.

    Returns:
        Role string ("read-write", "read-only", "admin", ...). Defaults
        to "read-write" when no entry is found (consistent with legacy
        behaviour).
    """
    if not roles:
        return "read-write"
    sample = next(iter(roles.values()), None)
    if isinstance(sample, dict):
        cid = customer_id or ""
        tenant_roles = roles.get(cid, {})
        if not tenant_roles and cid:
            tenant_roles = roles.get("", {})
        return tenant_roles.get(agent, "read-write")
    return roles.get(agent, "read-write")


_ROLE_RANKS = {"read-only": 0, "read-write": 1, "admin": 2}


def _role_rank(role: str) -> int:
    """Return privilege rank (higher = more powerful). Used to prevent escalation."""
    return _ROLE_RANKS.get(role, 1)


def _build_token_lookup(tokens_config: dict) -> tuple[dict, dict]:
    """Build token lookup tables from config.

    Config format supports two modes:
      1. Hashed mode (recommended): {"agent_name": {"hash": "sha256_hex", "role": "read-write"}}
      2. Legacy plaintext mode: {"agent_name": "plaintext_token"}

    In legacy mode, tokens are hashed on load and the original plaintext
    is discarded from memory.

    Returns:
        (token_hashes, agent_roles) where:
        - token_hashes: {token_hash: agent_name} for O(1) lookup
        - agent_roles: {agent_name: role} from config
    """
    token_hashes: dict[str, str] = {}
    roles: dict[str, str] = {}

    for agent_name, value in tokens_config.items():
        if isinstance(value, dict):
            # Hashed mode: {"hash": "sha256_hex", "role": "read-write"}
            token_hash = value.get("hash", "")
            if token_hash:
                token_hashes[token_hash] = agent_name
            roles[agent_name] = value.get("role", "read-write")
        elif isinstance(value, str):
            # Legacy plaintext mode: hash the token and discard plaintext
            token_hashes[_hash_token(value)] = agent_name
            roles[agent_name] = "read-write"

    return token_hashes, roles


def _generate_customer_token(customer_id: str) -> tuple[str, str]:
    """Generate a customer API key for *customer_id*.

    Returns (token, token_hash). The token is shown once at generation time;
    only the hash is persisted in config.

    Token format: ``twai_live_{24-char urlsafe-base64}``
    (18 random bytes → 24 chars; total length ~33 chars)
    """
    token = f"twai_live_{secrets.token_urlsafe(18)}"
    return token, _hash_token(token)


def _build_customer_token_lookup(customer_tokens_config: dict) -> dict:
    """Build customer token lookup table from config.

    Config format (hashed, recommended)::

        {"acme_hvac": {"hash": "sha256_hex"}}

    Legacy plaintext format (hashed on load, plaintext discarded)::

        {"acme_hvac": "plaintext_token"}

    Returns:
        {token_hash: customer_id} for O(1) lookup in _authenticate().
    """
    lookup: dict[str, str] = {}
    for customer_id, value in customer_tokens_config.items():
        if isinstance(value, dict):
            token_hash = value.get("hash", "")
            if token_hash:
                lookup[token_hash] = customer_id
        elif isinstance(value, str):
            lookup[_hash_token(value)] = customer_id
    return lookup


def load_config(config_path: Optional[str] = None) -> dict:
    """Load configuration from file or defaults."""
    config = DEFAULT_CONFIG.copy()

    if config_path is None:
        config_path = os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json"))

    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file) as f:
            file_config = json.load(f)
            config.update(file_config)

    # Environment overrides
    if "OHM_PORT" in os.environ:
        config["port"] = int(os.environ["OHM_PORT"])
    if "OHM_HOST" in os.environ:
        config["host"] = os.environ["OHM_HOST"]
    if "OHM_DB_PATH" in os.environ:
        config["db_path"] = os.environ["OHM_DB_PATH"]

    # DuckLake config overrides (OHM-kdk.1)
    config.setdefault("ducklake", {})
    ducklake_config: dict[str, Any] = config["ducklake"]  # type: ignore[assignment]
    if "OHM_DUCKLAKE_PATH" in os.environ:
        ducklake_config["path"] = os.environ["OHM_DUCKLAKE_PATH"]
    if "OHM_DUCKLAKE_DATA" in os.environ:
        ducklake_config["data_path"] = os.environ["OHM_DUCKLAKE_DATA"]

    return config


# ── Error Mapping ──────────────────────────────────────────


def _map_exception_to_http(exc: Exception) -> tuple[int, str]:
    """Map OHMError subclasses to HTTP status codes."""
    if isinstance(exc, (NodeNotFoundError, EdgeNotFoundError)):
        return 404, "not_found"
    if isinstance(exc, PermissionDeniedError):
        return 403, "permission_denied"
    if isinstance(exc, AuthenticationError):
        return 401, "authentication_error"
    if isinstance(exc, ConflictError):
        return 409, "conflict"
    if isinstance(exc, ValidationError):
        return 400, "validation_error"
    if isinstance(exc, ConfigurationError):
        return 501, "not_implemented"
    if isinstance(exc, OHMError):
        return 500, "internal_error"
    return 500, "internal_error"


# ── Route Registry ─────────────────────────────────────────


class _RouteRegistry:
    """Declarative method→path registry for 405 Method Not Allowed enforcement.

    Stores which HTTP methods are valid for each path pattern.  Used before
    dispatching to the existing if/elif chains so that wrong-method calls
    (e.g. GET /deduplicate when the endpoint is POST-only) get a proper 405
    with an Allow header instead of falling through to a misleading 404.

    Path patterns:
      - Exact:  "/stats", "/node"  — matched case-sensitively
      - Prefix: "/node/", "/edge/" — matched with str.startswith()
        Prefix patterns must end with "/".  Longer prefixes take priority.
    """

    def __init__(self) -> None:
        self._exact: dict[str, set[str]] = {}
        # List of (prefix, methods) sorted longest-first for correct matching
        self._prefixes: list[tuple[str, set[str]]] = []

    def add(self, method: str, path: str) -> None:
        method = method.upper()
        # "/" is the root exact path, not a catch-all prefix
        if path.endswith("/") and path != "/":
            for existing_prefix, methods in self._prefixes:
                if existing_prefix == path:
                    methods.add(method)
                    return
            entry: set[str] = {method}
            self._prefixes.append((path, entry))
            self._prefixes.sort(key=lambda x: len(x[0]), reverse=True)
        else:
            self._exact.setdefault(path, set()).add(method)

    def methods_for(self, path: str) -> set[str] | None:
        """Return allowed methods for a path, or None if the path is not registered."""
        if path in self._exact:
            return self._exact[path]
        for prefix, methods in self._prefixes:
            if path.startswith(prefix):
                return methods
        return None

    def check(self, method: str, path: str) -> tuple[bool, set[str] | None]:
        """Return (is_allowed, allowed_methods).

        - (True, None)     → path unknown; pass through to existing 404 handling
        - (True, methods)  → method is allowed for this path
        - (False, methods) → method NOT allowed; caller should send 405
        """
        allowed = self.methods_for(path)
        if allowed is None:
            return True, None
        if method.upper() in allowed:
            return True, allowed
        return False, allowed


def _build_router() -> _RouteRegistry:
    r = _RouteRegistry()

    # Infrastructure — no auth, always open
    for _p in ("/", "/openapi.json", "/health", "/ready", "/metrics", "/instance"):
        r.add("GET", _p)
    r.add("GET", "/events")
    r.add("GET", "/events/")

    # Read endpoints (GET exact)
    for _p in (
        "/stats",
        "/status",
        "/schema",
        "/schema/node-types",
        "/layers",
        "/agents",
        "/nodes",
        "/listen",
        "/search",
        "/semantic_search",
        "/inference",
        "/belief",
        "/intervene",
        "/ate",
        "/sensitivity",
        "/adjustment",
        "/voi",
        "/voi/tasks",
        "/suggest_causes",
        "/refute",
        "/regime",
        "/game",
        "/nash",
        "/lint",
        "/contract",
        "/duplicates",
        "/stale",
        "/admin/embeddings",
        "/admin/islands",
        "/admin/learned-half-lives",
        "/admin/snapshots",
        "/admin/verification-scan",
        "/config",
        "/bootstrap",
        "/hooks",
        "/graph/at",
        "/graph/changes",
        "/markov/absorbing",
        "/markov/expected_steps",
        "/templates",
        "/queries",
        "/plans",
        "/reports",
        "/runs",
        "/rul",
        "/edges",
    ):
        r.add("GET", _p)

    # GET prefix routes (parameterised paths like /node/{id})
    for _p in (
        "/context-gate/",
        "/decision/",
        "/node/",
        "/deep/",
        "/edge/",
        "/neighborhood/",
        "/path/",
        "/impact/",
        "/confidence/",
        "/agent/",
        "/provenance/",
        "/monte-carlo/",
        "/calibration/",
        "/agent_profile/",
        "/reliability/",
        "/compound_confidence/",
        "/observation/",
        "/synthesis/",
        "/timeline/",
        "/report/",
        "/run/",
    ):
        r.add("GET", _p)

    # /source_reliability: alias for /reliability/{source} with ?source= param
    r.add("GET", "/source_reliability")

    # Multi-method: /observations supports both GET (list) and POST (bulk upload)
    r.add("GET", "/observations")
    r.add("POST", "/observations")

    # OHM-xdd4: /observation/{id}/supersede (POST) for supersession chain
    r.add("POST", "/observation/")

    # /deduplicate and /admin/checkpoint: POST is canonical; GET kept for compat
    r.add("GET", "/deduplicate")
    r.add("POST", "/deduplicate")
    r.add("GET", "/admin/checkpoint")
    r.add("POST", "/admin/checkpoint")
    r.add("POST", "/admin/cleanup-hooks")
    r.add("GET", "/admin/verification-scan")
    r.add("GET", "/admin/orphan-triage")
    r.add("GET", "/admin/duplicates")
    r.add("POST", "/admin/observation-source-urls")
    r.add("POST", "/admin/source-node-urls")
    r.add("POST", "/admin/edge-layer-fix")
    r.add("POST", "/admin/pert-backfill")
    r.add("POST", "/admin/backfill-relational-tags")
    r.add("POST", "/admin/verification-decay")
    r.add("POST", "/admin/apply-decay")
    r.add("POST", "/admin/merge")
    r.add("POST", "/admin/vacuum-lake")
    r.add("POST", "/admin/evict-fragments")
    r.add("POST", "/admin/sync-beads")
    r.add("GET", "/config")
    r.add("PUT", "/config")
    r.add("GET", "/bootstrap")
    r.add("POST", "/bootstrap")
    r.add("POST", "/scenario")
    r.add("POST", "/propose-action")
    r.add("POST", "/execute-action")
    r.add("GET", "/loop-status")
    r.add("GET", "/admin/health")

    # /decay: write-in-GET (legacy); registered as GET to avoid spurious 405
    r.add("GET", "/decay")

    # Semantic-layer metrics endpoints
    r.add("GET", "/metrics/semantic")
    r.add("POST", "/metrics/semantic/actions")

    # Prospect lifecycle (OHM-844)
    r.add("GET", "/prospects")
    r.add("POST", "/prospect")

    # POST-only write endpoints (exact)
    for _p in (
        "/node",
        "/node/find_or_create",
        "/edge",
        "/outcome",
        "/agent/synthesis",
        "/ask",
        "/batch",
        "/webhook",
        "/state",
        "/register",
        "/vault/promote",
        "/heartbeat",
        "/sync",
        "/hooks",
    ):
        r.add("POST", _p)

    # Webhook outbox routes (OHM-ufjk)
    r.add("GET", "/webhooks/dead-letter")
    r.add("GET", "/webhooks/outbox")

    # /tasks: GET (list) and POST (create)
    r.add("GET", "/tasks")
    r.add("POST", "/tasks")

    # Document library routes
    r.add("POST", "/documents/upload")
    r.add("GET", "/documents/*")

    # POST prefix routes
    for _p in (
        "/challenge/",
        "/support/",
        "/observe/",
        "/webhook/",
        "/node/sign/",
        "/node/verify/",
        "/edge/sign/",
        "/edge/verify/",
        "/tasks/",
        "/admin/hooks/",
        "/twin/design/",
    ):
        r.add("POST", _p)

    # GET prefix routes for twin design
    r.add("GET", "/twin/design/")

    # POST exact routes for twin
    r.add("POST", "/twin/register")
    r.add("POST", "/twin/assemble")
    r.add("POST", "/twin/design/start")
    r.add("POST", "/prospect/transition/")
    r.add("GET", "/prospect/")
    r.add("POST", "/simulate/")
    r.add("POST", "/decision/")
    r.add("POST", "/type-proposal/")
    r.add("GET", "/type-proposals")
    r.add("POST", "/nudge/evaluate")
    r.add("POST", "/nudge/promote")
    r.add("POST", "/nudge/demote")

    # PATCH
    r.add("PATCH", "/node/")
    r.add("PATCH", "/edge/")
    r.add("PATCH", "/edges/")
    r.add("PATCH", "/tasks/")

    # DELETE
    r.add("DELETE", "/node/")
    r.add("DELETE", "/edge/")
    r.add("DELETE", "/hooks/")

    return r


_ROUTER = _build_router()
del _build_router

# Tenant provisioning routes (OHM-tss4.6) — admin-only, only active with --multi-tenant
_ROUTER.add("GET", "/tenants")
_ROUTER.add("GET", "/tenant/")
_ROUTER.add("POST", "/tenant/provision")
_ROUTER.add("POST", "/tenant/")
_ROUTER.add("DELETE", "/tenant/")


# ── HTTP Handler ───────────────────────────────────────────


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads for concurrent access."""

    daemon_threads = True
    request_queue_size = 128  # OHM-yv35: avoid connection resets under burst load (default was 5)
    timeout = 60  # seconds before dropping a stuck request thread
    _ohm_store = None  # Set by run_server for fallback access in current_store

    def handle_error(self, request, client_address):
        """Log request errors without crashing."""
        import logging

        logging.getLogger("ohm.server").warning(f"Request error from {client_address}: {request}")


from ohm.server.handlers.admin import AdminHandlerMixin
from ohm.server.handlers.analysis import AnalysisHandlerMixin
from ohm.server.handlers.ask import AskHandlerMixin
from ohm.server.handlers.catalog import CatalogHandlerMixin
from ohm.server.handlers.decision import DecisionHandlerMixin
from ohm.server.handlers.documents import DocumentHandlerMixin
from ohm.server.handlers.graph import GraphHandlerMixin
from ohm.server.handlers.infra import InfraHandlerMixin
from ohm.server.handlers.inference import InferenceHandlerMixin
from ohm.server.handlers.markov import MarkovHandlerMixin
from ohm.server.handlers.tenant import TenantHandlerMixin
from ohm.server.handlers.type_proposals import TypeProposalHandlerMixin
from ohm.server.handlers.prospects import ProspectHandlerMixin
from ohm.server.handlers.twins import TwinHandlerMixin
from ohm.server.handlers.models import ModelHandlerMixin
from ohm.server.handlers.temporal import TemporalHandlerMixin
from ohm.server.handlers.fragments import FragmentHandlerMixin
from ohm.server.handlers.tasks import TaskHandlerMixin
from ohm.server.handlers.nodes import NodeHandlerMixin
from ohm.server.handlers.edges import EdgeHandlerMixin
from ohm.server.handlers.reports_misc import ReportsHandlerMixin
from ohm.server.handlers.search import SearchHandlerMixin
from ohm.server.handlers.observations import ObservationHandlerMixin
from ohm.server.handlers.narrative import NarrativeHandlerMixin
from ohm.server.handlers.scenario import ScenarioHandlerMixin


class OhmHandler(
    AdminHandlerMixin, AnalysisHandlerMixin, AskHandlerMixin, CatalogHandlerMixin, DecisionHandlerMixin, DocumentHandlerMixin, GraphHandlerMixin, InfraHandlerMixin, InferenceHandlerMixin, MarkovHandlerMixin, TenantHandlerMixin, TypeProposalHandlerMixin, ProspectHandlerMixin, TwinHandlerMixin, ModelHandlerMixin, TemporalHandlerMixin, FragmentHandlerMixin, TaskHandlerMixin, NodeHandlerMixin, EdgeHandlerMixin, ReportsHandlerMixin, SearchHandlerMixin, ObservationHandlerMixin, NarrativeHandlerMixin, ScenarioHandlerMixin, BaseHTTPRequestHandler
):
    """HTTP request handler for OHM daemon."""

    store: Optional[OhmStore] = None  # single-tenant core store (always set)
    tenant_manager = None  # TenantManager instance when multi_tenant=True
    config: dict = {}
    tokens: dict = {}  # token_hash → agent_name
    customer_tokens: dict = {}  # token_hash → customer_id (OHM-tss4.3)
    roles: dict = {}  # agent_name -> role (read-write, read-only)
    no_auth: bool = False  # --no-auth flag: bypass all auth (dev mode)
    require_read_auth: bool = False  # OHM-gwg: require auth for reads (default: public reads)
    schema_config: SchemaConfig = DEFAULT_SCHEMA  # configurable schema (OHM or TOPO)
    multi_tenant: bool = False  # OHM-l31g: feature flag for multi-tenancy (default: off)
    # OHM-rlfw: IPs allowed to set X-Forwarded-For / X-Real-IP. Empty
    # (default) = never trust forwarded headers = always use socket IP.
    # Operators behind nginx/Caddy should populate this with the proxy's IP
    # range or set OHM_TRUSTED_PROXIES=10.0.0.0/8,192.168.1.0/24.
    TRUSTED_PROXIES: frozenset[str] = frozenset()
    # OHM-cwrc: per-instance write lock that serialises ALL writes through this
    # server. DuckDB is single-writer per connection; without this, concurrent
    # POST /node and POST /edge requests race against the same store and corrupt
    # the graph (55 nodes from 50 writes, etc.). Multi-tenant mode nests the
    # per-tenant write_lock inside this one.
    _write_lock: "threading.RLock" = threading.RLock()

    # ── Dispatch tables (set after class body) ────────────────
    # Maps path → method_name (string) for runtime getattr dispatch.
    _GET_EXACT: dict = {}
    _GET_PREFIXES: list = []
    _POST_EXACT: dict = {}
    _POST_PREFIXES: list = []
    _PUT_EXACT: dict = {}
    _DELETE_PREFIXES: list = []

    @property
    def _customer_id(self) -> str | None:
        """Return the authenticated customer's tenant ID, or None in single-tenant mode.

        When multi-tenancy is disabled (OHM-l31g), always returns None —
        zero overhead, no TenantManager lookup, no route ambiguity.
        When enabled, set by _authenticate() as self._resolved_customer_id.
        Alternatively, an admin-role agent can specify X-Tenant-ID header to act
        on behalf of a tenant (OHM-tss4.8). Non-admin agents are NOT permitted
        to use X-Tenant-ID — this prevents cross-tenant data access (OHM-tss4.19).
        Customer API keys always route to their own tenant via _resolved_customer_id
        and do not need (nor can use) X-Tenant-ID.
        """
        if not self.multi_tenant:
            return None
        resolved = getattr(self, "_resolved_customer_id", None)
        if resolved is not None:
            return resolved
        # X-Tenant-ID is only allowed for admin-role agents (OHM-tss4.19)
        agent = getattr(self, "_authenticated_agent", None)
        if agent is not None:
            role = _lookup_role(self.roles, agent, None)  # operator scope (no customer_id yet)
            if role == "admin":
                x_tenant = getattr(self, "headers", None)
                if x_tenant is not None:
                    x_tenant = x_tenant.get("X-Tenant-ID")
                if x_tenant:
                    from ohm.framework.validation import validate_customer_id

                    try:
                        return validate_customer_id(x_tenant)
                    except ValueError:
                        return None  # Invalid customer_id — ignore header
        return None

    @property
    def current_store(self) -> OhmStore:
        """Return the OhmStore for the current request context.

        Single-tenant: returns the global class-level store directly (zero overhead).
        Multi-tenant + agent token (_customer_id=None): returns the core store.
        Multi-tenant + customer token: routes to the tenant's isolated DuckDB via
          TenantManager.get_store(), which is LRU-cached (sub-millisecond on hit).
        Raises NodeNotFoundError (→ HTTP 404) if the tenant is not provisioned.
        404 is intentional: unprovisioned = resource doesn't exist from caller's view.
        403 is reserved for actual authorisation failures (wrong role, cross-tenant key).
        """
        if not self.multi_tenant:
            if self.store is None:
                import logging

                logger = logging.getLogger("ohm.server")
                logger.critical(f"current_store: self.store is None! handler={self}, server={getattr(self, 'server', None)}")
                # Fallback: try to get store from server object
                server = getattr(self, "server", None)
                if server is not None and hasattr(server, "_ohm_store") and server._ohm_store is not None:
                    logger.warning("current_store: falling back to server._ohm_store")
                    return server._ohm_store
            return self.store
        customer_id = self._customer_id
        if customer_id is None or self.tenant_manager is None:
            return self.store
        from ohm.tenant import TenantNotFoundError

        try:
            return self.tenant_manager.get_store(customer_id)
        except TenantNotFoundError:
            raise NodeNotFoundError("Tenant not found — provision this tenant before use")

    # OHM-1s14.3: keys from meta.json that a tenant may override on top of the
    # global server config. Server-level keys (quack, host, port, sync, etc.)
    # are NOT overridable — they are infrastructure concerns.
    _TENANT_CONFIG_ALLOWLIST = frozenset({"enforce_layer_gates", "embeddings"})

    @property
    def current_config(self) -> dict:
        """Return config for the current request context.

        Single-tenant: returns ``self.config`` directly (zero overhead).
        Multi-tenant + agent token (customer_id=None): returns ``self.config``
          (operator scope — global defaults apply).
        Multi-tenant + customer token: merges global config with tenant-specific
          overrides from ``meta.json`` for allowlisted keys only
          (``enforce_layer_gates``, ``embeddings``). Server-level keys
          (``quack``, ``host``, ``port``, ``sync_interval_seconds``, …) are
          NOT overridable per tenant.

        If the tenant's meta.json cannot be read (deleted, corrupted), falls
        back to the global config so the request still succeeds.
        """
        if not self.multi_tenant:
            return self.config
        customer_id = self._customer_id
        if customer_id is None or self.tenant_manager is None:
            return self.config
        try:
            meta = self.tenant_manager.get_meta(customer_id)
        except Exception:
            return self.config
        merged = dict(self.config)
        for key in self._TENANT_CONFIG_ALLOWLIST:
            if key in meta:
                merged[key] = meta[key]
        return merged

    def log_message(self, format, *args):
        """Structured request logging with correlation ID."""
        import re

        corr_id = getattr(self, "_correlation_id", "-")
        timestamp = datetime.now(timezone.utc).isoformat()
        message = format % args
        message = re.sub(r"([?&]token=)[^&\s]+", r"\1[REDACTED]", message)
        sys.stderr.write(f"[{timestamp}] [{corr_id}] {message}\n")
        sys.stderr.flush()

    def _authenticate(self) -> Optional[str]:
        """Validate bearer token, return agent name or customer_id, or None.

        Checks agent tokens first (existing behaviour), then customer API keys.
        When a customer token matches, sets self._resolved_customer_id as a side
        effect so that _customer_id property can route to the correct tenant.
        Returns ``customer:{customer_id}`` for customer tokens (OHM-l1vs) so that
        boundary enforcement can distinguish customer writes from agent writes.
        All existing call sites (which do ``agent = self._authenticate()``) keep
        working without modification — the agent name is just a string.

        If X-Ohm-Agent header is provided and the authenticated agent has write
        access, the header value overrides the token-derived agent name. This allows
        SDK clients to specify their identity (e.g., connect_remote(actor="thalia")).
        """
        from urllib.parse import unquote

        # Reset per-request state (OHM-tss4.19)
        self._authenticated_agent = None
        self._resolved_customer_id = None

        # Rate-limit brute-force auth attempts (M2). Skipped in no-auth dev
        # mode and when the client address is unavailable (unit-test handlers).
        if getattr(self, "no_auth", False):
            client_ip = None
        else:
            try:
                client_ip = self.client_address[0]
            except (AttributeError, IndexError, TypeError):
                client_ip = None
            if client_ip:
                _check_auth_rate_limit(client_ip)

        token_provided = False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token_provided = True
            resolved = self._match_token(unquote(auth[7:]), client_ip)
            if resolved is not None:
                return resolved

        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        if "token" in qs:
            token_provided = True
            resolved = self._match_token(qs["token"][0], client_ip)
            if resolved is not None:
                return resolved

        if token_provided and client_ip:
            _record_auth_failure(client_ip)
        return None

    def _match_token(self, token: str, client_ip: str | None) -> str | None:
        """Resolve a provided token to an agent/customer identity.

        ``self.tokens`` and ``self.customer_tokens`` are both keyed by the
        SHA-256 hash of the token, so we hash the provided token once and do
        an O(1) dict lookup rather than re-hashing it per stored token (the
        previous O(N) per-request cost). A SHA-256 hash lookup leaks nothing
        about the secret, so constant-time comparison is unnecessary here.

        Returns the resolved identity string (agent name, an X-Ohm-Agent
        override, or ``customer:<id>``), or ``None`` if the token matches
        nothing. Sets ``_authenticated_agent`` / ``_resolved_customer_id`` as
        a side effect and clears the client's auth-failure count on success.
        """
        h = _hash_token(token)
        agent_name = self.tokens.get(h)
        if agent_name is not None:
            self._authenticated_agent = agent_name
            if client_ip:
                _clear_auth_failures(client_ip)
            # Honor X-Ohm-Agent header if the authenticated agent has write
            # access AND the header value does not escalate privileges (the
            # spoofed agent's role must not exceed the token's own role).
            ohm_agent = self.headers.get("X-Ohm-Agent")
            if ohm_agent:
                token_role = _lookup_role(self.roles, agent_name, self._customer_id)
                header_role = _lookup_role(self.roles, ohm_agent, self._customer_id)
                if token_role != "read-only" and _role_rank(header_role) <= _role_rank(token_role):
                    return ohm_agent
            return agent_name
        customer_id = self.customer_tokens.get(h)
        if customer_id is not None:
            self._resolved_customer_id = customer_id
            if client_ip:
                _clear_auth_failures(client_ip)
            return f"customer:{customer_id}"
        return None

    def _require_auth(self) -> str:
        """Authenticate and return agent name, or raise AuthenticationError."""
        agent = self._authenticate()
        if agent is None:
            raise AuthenticationError("Authentication required — provide Bearer token")
        return agent

    def _check_write_access(self, agent: str) -> None:
        """Verify agent has write access. Raises PermissionDeniedError if read-only."""
        role = _lookup_role(self.roles, agent, self._customer_id)
        if role == "read-only":
            raise PermissionDeniedError(f"Agent '{agent}' has read-only access — writes are not permitted")
        return None

    def _require_write_auth(self) -> str:
        """Authenticate and verify write access. Returns agent name or raises."""
        agent = self._require_auth()
        self._check_write_access(agent)
        return agent

    def _require_admin(self, action: str = "this admin endpoint") -> str:
        """Authenticate and require admin role. Returns agent name or raises.

        *action* only phrases the error message (e.g. "provisioning",
        "administrative maintenance endpoints").
        """
        agent = self._authenticate()
        if agent is None:
            if self.no_auth and not self.tokens and not self.customer_tokens:
                agent = "ohm"
            else:
                raise AuthenticationError("Authentication required — provide Bearer token")
        # Customer API keys are tenant-scoped and must never reach admin endpoints.
        if getattr(self, "_resolved_customer_id", None) is not None:
            raise PermissionDeniedError("Admin endpoints require an agent token, not a customer key")
        role = _lookup_role(self.roles, agent, self._customer_id)
        if role == "admin" or (self.no_auth and not self.roles):
            return agent
        raise PermissionDeniedError(f"Agent '{agent}' role '{role}' is not authorized for {action} (requires 'admin' role)")

    def _json_response(self, code: int, data):
        """Send a JSON response.

        If _buffer_json_response is True (set by _do_GET when post_query
        hooks are active), the response is buffered so post_query hooks
        can decorate it before sending.
        """
        if getattr(self, "_buffer_json_response", False):
            self._json_response_buffer = (code, data)
            return
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _binary_response(
        self,
        status: int,
        content_bytes: bytes,
        content_type: str = "application/octet-stream",
        filename: str | None = None,
    ) -> None:
        """Send a binary response with appropriate headers."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content_bytes)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content_bytes)

    def _method_not_allowed(self, allowed_methods: set[str]) -> None:
        """Send 405 Method Not Allowed with required Allow header (RFC 7231 §6.5.5)."""
        allow_header = ", ".join(sorted(allowed_methods))
        body = json.dumps(
            {
                "error": "method_not_allowed",
                "message": f"Method not allowed — use: {allow_header}",
                "allow": allow_header,
            },
            indent=2,
        ).encode()
        self.send_response(405)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Allow", allow_header)
        self.end_headers()
        self.wfile.write(body)

    def _handle_sse_events(self, path: str, qs: dict) -> None:
        """Handle SSE /events endpoint — streams change feed events.

        Query parameters:
        - since: ISO timestamp to stream from (optional, defaults to last sync)
        - agent: filter to changes by this agent (optional)
        - layer: filter to changes in this layer (optional, e.g. L3)
        - node_type: filter to changes for nodes of this type (optional)
        - node_id: filter to changes for a specific node (optional)
        - topics: comma-separated topic labels (optional)
        """

        # Auth for SSE (respects require_read_auth setting)
        agent = self._authenticate()
        if agent is None:
            if self.no_auth or not self.tokens:
                agent = "ohm"
            elif self.require_read_auth:
                raise AuthenticationError("Authentication required — provide Bearer token")
            else:
                agent = "ohm"

        # Parse parameters
        since = qs.get("since", [None])[0]
        filter_agent = qs.get("agent", [None])[0]
        filter_layer = qs.get("layer", [None])[0]
        filter_node_type = qs.get("node_type", [None])[0]
        filter_node_id = qs.get("node_id", [None])[0]
        topics_param = qs.get("topics", [None])[0]
        topics = topics_param.split(",") if topics_param else None

        # Resolve 'since' from agent state if not provided
        if not since:
            assert self.current_store is not None
            state = self.current_store.get_agent_state(agent)
            if state and state.get("last_sync"):
                since = state["last_sync"]
                # last_sync is a TIMESTAMP column — DuckDB returns datetime, not string
                if isinstance(since, datetime):
                    since = since.isoformat()
            else:
                # Default to current time (only stream new events)
                since = datetime.now(timezone.utc).isoformat()

        # Register subscription
        import uuid

        sub_id = str(uuid.uuid4())[:8]
        with _sse_lock:
            _sse_subscribers[sub_id] = {
                "agent_name": agent,
                "since": since,
                "last_event_id": sub_id,
                "topics": topics,
                "filter_agent": filter_agent,
                "filter_layer": filter_layer,
                "filter_node_type": filter_node_type,
                "filter_node_id": filter_node_id,
                "customer_id": self._customer_id,
            }

        # Send SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-SSE-Subscription-ID", sub_id)
        self.end_headers()

        # Send initial subscription event
        self.wfile.write(f"id: {sub_id}\n".encode())
        self.wfile.write("event: subscribed\n".encode())
        self.wfile.write(f"data: {json.dumps({'subscription_id': sub_id, 'since': since})}\n\n".encode())
        self.wfile.flush()

        # Stream change feed events in batches
        from ohm.queries import query_change_feed

        last_ts = since
        event_count = 0
        max_events = 1000  # Safety limit
        batch_size = 50  # Batch events for efficiency

        try:
            assert self.current_store is not None
            while event_count < max_events:
                # Query for new changes since last event
                changes = query_change_feed(
                    self.current_store.conn,
                    since=last_ts,
                    agent_name=filter_agent,
                    node_type=filter_node_type,
                    node_id=filter_node_id,
                    limit=batch_size,
                )

                if not changes:
                    # No new events — send heartbeat and wait
                    self.wfile.write(": heartbeat\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.5)
                    continue

                # Batch events together for efficiency (reduce syscall overhead)
                batch_lines: list[str] = []
                for change in changes:
                    event_id = f"{sub_id}-{event_count}"
                    last_ts = change.get("created_at", last_ts)

                    # Filter by topics if specified
                    if topics:
                        node_label = change.get("label", "").lower()
                        if not any(t.lower() in node_label for t in topics):
                            continue

                    batch_lines.append(f"id: {event_id}")
                    batch_lines.append(f"data: {json.dumps(change, default=str)}")
                    event_count += 1

                # Write entire batch in one syscall (batched SSE)
                if batch_lines:
                    self.wfile.write("\n".join(batch_lines).encode() + b"\n\n")
                    self.wfile.flush()

                # If we got fewer than batch_size, we've exhausted changes
                if len(changes) < batch_size:
                    time.sleep(0.5)

        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected
        finally:
            with _sse_lock:
                _sse_subscribers.pop(sub_id, None)

    def _error_response(self, exc: Exception):
        """Send a structured error response with correlation ID."""
        code, error_type = _map_exception_to_http(exc)
        corr_id = getattr(self, "_correlation_id", str(uuid.uuid4()))
        body = {
            "error": error_type,
            "message": str(exc),
            "correlation_id": corr_id,
            "status": code,
        }
        self._json_response(code, body)

    def send_response(self, code, message=None):
        """Track response code for logging."""
        self._response_code = code
        super().send_response(code, message)

    def _get_client_ip(self) -> str:
        """Resolve the real client IP, honouring X-Forwarded-For / X-Real-IP.

        OHM-rlfw: a raw socket IP (self.client_address[0]) is the proxy's
        loopback when running behind nginx/Caddy/etc, so a single agent
        can exhaust the per-IP bucket for everyone. We trust forwarded
        headers only when the immediate peer is in TRUSTED_PROXIES.
        The socket IP is the fallback.

        `TRUSTED_PROXIES` is a class-level set; populate via
        ``OhmHandler.TRUSTED_PROXIES = {"127.0.0.1", "10.0.0.0/8"}`` or
        via the ``OHM_TRUSTED_PROXIES`` env var (comma-separated). An
        empty set disables forwarded-header trust entirely.
        """
        peer = self.client_address[0]
        trusted = getattr(self, "TRUSTED_PROXIES", None)
        if not trusted:
            return peer
        if peer not in trusted:
            return peer
        # Peer is a trusted proxy — honour forwarded headers.
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        xri = self.headers.get("X-Real-IP")
        if xri:
            return xri.strip()
        return peer

    def _check_rate_limit(self) -> bool:
        """Check if the requesting IP is within rate limits. Returns True if allowed.

        OHM-1s14.4: keyed by (client_ip, customer_id) so two tenants sharing a
        NAT/proxy IP get independent rate limit counters. customer_id=None
        (single-tenant or operator-scope agent token) preserves legacy behaviour.
        """
        client_ip = self._get_client_ip()
        customer_id = self._customer_id
        key = (client_ip, customer_id)
        now = time.time()
        with _rate_limit_lock:
            if key not in _rate_limit_store:
                _rate_limit_store[key] = [now]
                return True

            # Prune old entries
            window_start = now - RATE_LIMIT_WINDOW
            _rate_limit_store[key] = [ts for ts in _rate_limit_store[key] if ts > window_start]

            # OHM-41g: Prune stale keys that have no recent timestamps.
            # Without this, unique (ip, customer_id) pairs accumulate forever.
            if not _rate_limit_store[key]:
                del _rate_limit_store[key]
                # Periodically prune other stale keys (every ~100 requests)
                if len(_rate_limit_store) > 100:
                    stale = [k for k, timestamps in _rate_limit_store.items() if not timestamps or timestamps[-1] < window_start]
                    for k in stale:
                        del _rate_limit_store[k]
                _rate_limit_store[key] = [now]
                return True

            if len(_rate_limit_store[key]) >= RATE_LIMIT_MAX_REQUESTS:
                return False

            _rate_limit_store[key].append(now)
            return True

    def _read_body(self, allow_raw: bool = False):
        """Read and parse JSON request body. Enforces size limit.

        If ``allow_raw`` is True, non-JSON bodies are returned as raw bytes
        instead of raising a validation error. This is used by endpoints that
        accept multipart/form-data uploads.
        """
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length < 0 or length > MAX_BODY_SIZE:
            raise ValidationError(f"Request body too large: {length} bytes (max {MAX_BODY_SIZE})")
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            if allow_raw:
                return body
            raise ValidationError(f"Invalid JSON in request body: {e}")

    # ── Body Validation Schemas ─────────────────────────────

    _REQUIRED_FIELDS = {
        "/node": ["id", "label"],
        "/tasks": ["label"],
        "/edge": ["from", "to", "type"],
        "/state": [],
        "/register": [],
        "/heartbeat": [],
        "/sync": [],
        "/challenge": [],
        "/support": [],
        "/observe": [],
        "/observations": [],
        "/outcome": [],
        "/ask": ["question"],
        "/reliability": [],
        "/webhook": [],
    }

    _FIELD_TYPES: dict[str, dict[str, type | tuple[type, ...]]] = {
        "/node": {
            "id": str,
            "label": str,
            "type": str,
            "content": (str, type(None)),
            "confidence": (int, float),
            "visibility": str,
            "provenance": (str, type(None)),
            "tags": (list, type(None)),
            "metadata": (dict, type(None)),
        },
        "/edge": {
            "from": str,
            "to": str,
            "type": str,
            "layer": str,
            "confidence": (int, float, type(None)),
            "condition": (str, type(None)),
            "provenance": (str, type(None)),
            "challenge_of": (str, type(None)),
            "challenge_type": (str, type(None)),
        },
        "/state": {
            "focus": (str, type(None)),
            "patterns": (list, type(None)),
            "services": (list, type(None)),
            "session_id": (str, type(None)),
        },
        "/register": {
            "name": (str, type(None)),
            "description": (str, type(None)),
            "values": (list, type(None)),
            "goals": (list, type(None)),
            "capabilities": (list, type(None)),
            "interests": (list, type(None)),
            "listens_to": (list, type(None)),
        },
        "/heartbeat": {
            "focus": (str, type(None)),
        },
        "/challenge": {
            "reason": (str, type(None)),
            "confidence": (int, float, type(None)),
            "challenge_type": (str, type(None)),
        },
        "/support": {
            "reason": (str, type(None)),
            "confidence": (int, float, type(None)),
        },
        "/observe": {
            "type": (str, type(None)),
            "value": (int, float, str, type(None)),
            "baseline": (int, float, type(None)),
            "sigma": (int, float, type(None)),
            "source": (str, type(None)),
            "notes": (str, type(None)),
            "source_name": (str, type(None)),
            "source_url": (str, type(None)),
            "half_life_days": (int, float, type(None)),
        },
        "/observation": {
            "old_obs_id": (str, type(None)),
        },
        "/ask": {
            "question": str,
            "agent": (str, type(None)),
            "depth": (int, type(None)),
            "include_inference": (bool, type(None)),
            "limit": (int, type(None)),
        },
    }

    def _validate_body(self, path: str, body: dict) -> dict:
        """Validate request body fields for a given endpoint path.

        Checks: body is a dict, required fields present, field types correct,
        and no unexpected fields for known endpoints.

        Returns the validated body dict.
        Raises ValidationError on invalid input.
        """
        if not isinstance(body, dict):
            raise ValidationError(f"Request body must be a JSON object, got {type(body).__name__}")

        # Normalize path prefixes for sub-resource endpoints
        # /challenge/xxx → /challenge, /support/xxx → /support, /observe/xxx → /observe
        validation_path = path
        for prefix in ("/challenge/", "/support/", "/observe/", "/webhook/"):
            if path.startswith(prefix):
                validation_path = prefix.rstrip("/")
                break

        # Pre-normalize /edge field aliases before required-field check
        if validation_path == "/edge":
            if "from_node" in body and "from" not in body:
                body["from"] = body.pop("from_node")
            if "to_node" in body and "to" not in body:
                body["to"] = body.pop("to_node")
            if "edge_type" in body and "type" not in body:
                body["type"] = body.pop("edge_type")

        # Check required fields
        required = self._REQUIRED_FIELDS.get(validation_path, [])
        missing = [f for f in required if f not in body]
        if missing:
            raise ValidationError(f"Missing required fields: {', '.join(missing)}")

        # Check field types
        field_types = self._FIELD_TYPES.get(validation_path, {})
        for field, value in body.items():
            if field in field_types:
                expected = field_types[field]
                if not isinstance(value, expected):
                    if isinstance(expected, tuple):
                        type_names = " / ".join(t.__name__ if hasattr(t, "__name__") else str(t) for t in expected)
                    else:
                        type_names = expected.__name__ if hasattr(expected, "__name__") else str(expected)
                    raise ValidationError(f"Field '{field}' must be {type_names}, got {type(value).__name__}")

        # Validate specific field values
        if validation_path == "/node":
            from ohm.validation import validate_identifier

            try:
                validate_identifier(body["id"], name="id")
            except ValueError as e:
                raise ValidationError(str(e))
            if "type" in body and body["type"] not in self.schema_config.node_types:
                raise ValidationError(f"Invalid node type: '{body['type']}' — must be one of: {', '.join(sorted(self.schema_config.node_types))}")
            if "visibility" in body and body["visibility"] not in VALID_VISIBILITIES:
                raise ValidationError(f"Invalid visibility: '{body['visibility']}' — must be private, team, or public")
            if "confidence" in body:
                from ohm.validation import validate_confidence

                try:
                    validate_confidence(float(body["confidence"]))
                except ValueError as e:
                    raise ValidationError(str(e))
            # Task-specific field validation
            if "task_status" in body and body["task_status"] is not None:
                from ohm.schema import VALID_TASK_STATUSES

                if body["task_status"] not in VALID_TASK_STATUSES:
                    raise ValidationError(f"Invalid task_status: '{body['task_status']}' — must be one of: {', '.join(sorted(VALID_TASK_STATUSES))}")
            if "priority" in body and body["priority"] is not None:
                from ohm.schema import VALID_PRIORITY

                if body["priority"] not in VALID_PRIORITY:
                    raise ValidationError(f"Invalid priority: '{body['priority']}' — must be one of: {', '.join(sorted(VALID_PRIORITY))}")

        elif validation_path == "/edge":
            from ohm.validation import validate_identifier, validate_layer

            try:
                validate_identifier(body["from"], name="from_node")
                validate_identifier(body["to"], name="to_node")
            except ValueError as e:
                raise ValidationError(str(e))
            if body["type"] not in self.schema_config.all_edge_types:
                raise ValidationError(f"Invalid edge type: '{body['type']}' — must be one of: {', '.join(sorted(self.schema_config.all_edge_types))}")
            if "layer" in body:
                try:
                    validate_layer(body["layer"])
                except ValueError as e:
                    raise ValidationError(str(e))
            if "confidence" in body and body["confidence"] is not None:
                from ohm.validation import validate_confidence

                try:
                    validate_confidence(float(body["confidence"]))
                except ValueError as e:
                    raise ValidationError(str(e))

        elif validation_path == "/register":
            # Don't validate the display 'name' as an identifier — it's a human-readable
            # label that may contain Unicode (e.g., "Métis"). The agent identifier comes
            # from the auth token, not the body.
            pass

        elif validation_path in ("/challenge", "/support"):
            if "confidence" in body and body["confidence"] is not None:
                from ohm.validation import validate_confidence

                try:
                    validate_confidence(float(body["confidence"]))
                except ValueError as e:
                    raise ValidationError(str(e))

        return body

    def do_GET(self):
        """Handle GET requests with error mapping and correlation IDs."""
        self._correlation_id = str(uuid.uuid4())
        start = time.time()
        with _metrics_lock:
            _metrics["requests_total"] += 1
            _metrics["requests_get"] += 1
        try:
            if not self._check_rate_limit():
                with _metrics_lock:
                    _metrics["rate_limited"] += 1
                self._json_response(
                    429,
                    {
                        "error": "rate_limited",
                        "message": "Too many requests. Try again later.",
                        "correlation_id": self._correlation_id,
                    },
                )
                return
            from urllib.parse import urlparse as _up

            _path = _up(self.path).path.rstrip("/") or "/"
            _ok, _allowed = _ROUTER.check("GET", _path)
            if not _ok:
                self._method_not_allowed(_allowed)
                return
            self._do_GET()
        except OHMError as e:
            self._error_response(e)
        except ValueError as e:
            self._error_response(ValidationError(str(e)))
        except Exception as e:
            self._error_response(OHMError(str(e)))
        finally:
            elapsed = (time.time() - start) * 1000
            code = getattr(self, "_response_code", 0)
            with _metrics_lock:
                if 400 <= code < 500:
                    _metrics["errors_4xx"] += 1
                elif code >= 500:
                    _metrics["errors_5xx"] += 1
                _request_latencies.append(elapsed)
            _record_endpoint_latency("GET", self.path, elapsed, code)
            self.log_message(
                "GET %s → %s (%.1fms)",
                self.path,
                code,
                elapsed,
            )

    def do_POST(self):
        """Handle POST requests with error mapping and correlation IDs."""
        self._correlation_id = str(uuid.uuid4())
        start = time.time()
        with _metrics_lock:
            _metrics["requests_total"] += 1
            _metrics["requests_post"] += 1
        try:
            if not self._check_rate_limit():
                with _metrics_lock:
                    _metrics["rate_limited"] += 1
                self._json_response(
                    429,
                    {
                        "error": "rate_limited",
                        "message": "Too many requests. Try again later.",
                        "correlation_id": self._correlation_id,
                    },
                )
                return
            from urllib.parse import urlparse as _up

            _path = _up(self.path).path.rstrip("/") or "/"
            _ok, _allowed = _ROUTER.check("POST", _path)
            if not _ok:
                self._method_not_allowed(_allowed)
                return
            self._do_POST()
        except OHMError as e:
            self._error_response(e)
        except ValueError as e:
            self._error_response(ValidationError(str(e)))
        except Exception as e:
            # OHM-hf7u: map DuckDB exceptions to appropriate HTTP codes and
            # log the full traceback so transient 500s are diagnosable.
            import logging as _logging

            _logger = _logging.getLogger("ohm.server")
            _type = type(e).__name__
            if "Constraint" in _type or "constraint" in str(e).lower():
                _logger.warning("POST %s → 409 Conflict: %s", self.path, e)
                self._error_response(ConflictError(f"Constraint violation: {e}"))
            elif "Catalog" in _type or "Transaction" in _type or "IO" in _type:
                _logger.error("POST %s → 500 (transient DuckDB %s): %s", self.path, _type, e, exc_info=True)
                self._error_response(OHMError(f"Database error ({_type}): {e}"))
            else:
                _logger.error("POST %s → 500 (unhandled %s): %s", self.path, _type, e, exc_info=True)
                self._error_response(OHMError(str(e)))
        finally:
            elapsed = (time.time() - start) * 1000
            code = getattr(self, "_response_code", 0)
            with _metrics_lock:
                if 400 <= code < 500:
                    _metrics["errors_4xx"] += 1
                elif code >= 500:
                    _metrics["errors_5xx"] += 1
                _request_latencies.append(elapsed)
            _record_endpoint_latency("POST", self.path, elapsed, code)
            self.log_message(
                "POST %s → %s (%.1fms)",
                self.path,
                code,
                elapsed,
            )

    def do_DELETE(self):
        """Handle DELETE requests with error mapping and correlation IDs."""
        self._correlation_id = str(uuid.uuid4())
        start = time.time()
        with _metrics_lock:
            _metrics["requests_total"] += 1
        try:
            if not self._check_rate_limit():
                with _metrics_lock:
                    _metrics["rate_limited"] += 1
                self._json_response(
                    429,
                    {
                        "error": "rate_limited",
                        "message": "Too many requests. Try again later.",
                        "correlation_id": self._correlation_id,
                    },
                )
                return
            from urllib.parse import urlparse as _up

            _path = _up(self.path).path.rstrip("/") or "/"
            _ok, _allowed = _ROUTER.check("DELETE", _path)
            if not _ok:
                self._method_not_allowed(_allowed)
                return
            self._do_DELETE()
        except OHMError as e:
            self._error_response(e)
        except ValueError as e:
            self._error_response(ValidationError(str(e)))
        except Exception as e:
            import logging as _logging

            _logger = _logging.getLogger("ohm.server")
            _type = type(e).__name__
            if "Constraint" in _type or "constraint" in str(e).lower():
                _logger.warning("DELETE %s → 409 Conflict: %s", self.path, e)
                self._error_response(ConflictError(f"Constraint violation: {e}"))
            else:
                _logger.error("DELETE %s → 500 (unhandled %s): %s", self.path, _type, e, exc_info=True)
                self._error_response(OHMError(str(e)))
        finally:
            elapsed = (time.time() - start) * 1000
            code = getattr(self, "_response_code", 0)
            with _metrics_lock:
                if 400 <= code < 500:
                    _metrics["errors_4xx"] += 1
                elif code >= 500:
                    _metrics["errors_5xx"] += 1
                _request_latencies.append(elapsed)
            _record_endpoint_latency("DELETE", self.path, elapsed, code)
            self.log_message(
                "DELETE %s → %s (%.1fms)",
                self.path,
                code,
                elapsed,
            )

    def do_PUT(self):
        """Handle PUT requests with error mapping and correlation IDs (OHM-801)."""
        self._correlation_id = str(uuid.uuid4())
        start = time.time()
        with _metrics_lock:
            _metrics["requests_total"] += 1
        try:
            if not self._check_rate_limit():
                with _metrics_lock:
                    _metrics["rate_limited"] += 1
                self._json_response(
                    429,
                    {
                        "error": "rate_limited",
                        "message": "Too many requests. Try again later.",
                        "correlation_id": self._correlation_id,
                    },
                )
                return
            from urllib.parse import urlparse as _up

            _path = _up(self.path).path.rstrip("/") or "/"
            _ok, _allowed = _ROUTER.check("PUT", _path)
            if not _ok:
                self._method_not_allowed(_allowed)
                return
            self._do_PUT()
        except OHMError as e:
            self._error_response(e)
        except ValueError as e:
            self._error_response(ValidationError(str(e)))
        except Exception as e:
            import logging as _logging

            _logger = _logging.getLogger("ohm.server")
            _type = type(e).__name__
            if "Constraint" in _type or "constraint" in str(e).lower():
                _logger.warning("PUT %s - 409 Conflict: %s", self.path, e)
                self._error_response(ConflictError(f"Constraint violation: {e}"))
            else:
                _logger.error("PUT %s - 500 (unhandled %s): %s", self.path, _type, e, exc_info=True)
                self._error_response(OHMError(str(e)))
        finally:
            elapsed = (time.time() - start) * 1000
            code = getattr(self, "_response_code", 0)
            with _metrics_lock:
                if 400 <= code < 500:
                    _metrics["errors_4xx"] += 1
                elif code >= 500:
                    _metrics["errors_5xx"] += 1
                _request_latencies.append(elapsed)
            _record_endpoint_latency("PUT", self.path, elapsed, code)
            self.log_message(
                "PUT %s - %s (%.1fms)",
                self.path,
                code,
                elapsed,
            )

    def do_PATCH(self):
        """Handle PATCH requests with error mapping and correlation IDs."""
        self._correlation_id = str(uuid.uuid4())
        start = time.time()
        with _metrics_lock:
            _metrics["requests_total"] += 1
        try:
            if not self._check_rate_limit():
                with _metrics_lock:
                    _metrics["rate_limited"] += 1
                self._json_response(
                    429,
                    {
                        "error": "rate_limited",
                        "message": "Too many requests. Try again later.",
                        "correlation_id": self._correlation_id,
                    },
                )
                return
            from urllib.parse import urlparse as _up

            _path = _up(self.path).path.rstrip("/") or "/"
            _ok, _allowed = _ROUTER.check("PATCH", _path)
            if not _ok:
                self._method_not_allowed(_allowed)
                return
            self._do_PATCH()
        except OHMError as e:
            self._error_response(e)
        except ValueError as e:
            self._error_response(ValidationError(str(e)))
        except Exception as e:
            self._error_response(OHMError(str(e)))
        finally:
            elapsed = (time.time() - start) * 1000
            code = getattr(self, "_response_code", 0)
            with _metrics_lock:
                if 400 <= code < 500:
                    _metrics["errors_4xx"] += 1
                elif code >= 500:
                    _metrics["errors_5xx"] += 1
                _request_latencies.append(elapsed)
            _record_endpoint_latency("PATCH", self.path, elapsed, code)
            self.log_message(
                "%s %s %.3fms",
                self.path,
                code,
                elapsed,
            )

    def _do_PATCH(self):
        """Handle PATCH /node/{id} or PATCH /edge/{id} — partial update."""
        from datetime import datetime, timezone
        from urllib.parse import urlparse
        from .boundary import enforce_write_boundary, enforce_l2_immutability
        from ohm.validation import validate_identifier
        from ohm.exceptions import NodeNotFoundError

        if self.no_auth:
            agent = self._authenticate() or "ohm"
        else:
            agent = self._require_write_auth()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()

        if path.startswith("/tasks/"):
            path = "/node/" + path[7:]

        if path.startswith("/node/"):
            node_id = path[6:]
            node_id = validate_identifier(node_id, name="node_id")
            node = self.current_store.get_node(node_id)
            if not node:
                raise NodeNotFoundError(f"Node not found: {node_id}")

            # Enforce L2 immutability — source nodes cannot be updated (OHM-k5wk)
            enforce_l2_immutability(self.current_store.conn, agent, node_id)

            # Validate type change against schema
            if "type" in body and body["type"] not in self.schema_config.node_types:
                raise ValidationError(f"Invalid node type: '{body['type']}' — must be one of: {', '.join(sorted(self.schema_config.node_types))}")

            now = datetime.now(timezone.utc).isoformat()
            patchable = [
                "label",
                "type",
                "content",
                "confidence",
                "visibility",
                "provenance",
                "tags",
                "metadata",
                "priority",
                "url",
                "task_status",
                "assigned_to",
                "due_date",
                "utility_scale",
                "current_best_action",
                "action_alternatives",
                "utility_usd_per_day",
                "utility_currency",
                "expected_claim",
                "success_criteria",
            ]
            update_fields = []
            update_params = []
            for field in patchable:
                if field in body:
                    update_fields.append(f"{field} = ?")
                    update_params.append(body[field])

            if not update_fields:
                raise ValidationError("No updatable fields provided")

            update_fields.append("updated_at = ?")
            update_params.append(now)
            update_fields.append("updated_by = ?")
            update_params.append(agent)
            update_params.append(node_id)

            self.current_store.conn.execute(
                f"UPDATE ohm_nodes SET {', '.join(update_fields)} WHERE id = ?",
                update_params,
            )
            self.current_store._log_change("ohm_nodes", node_id, "UPDATE", "L3", agent_name=agent)
            self.current_store._increment_graph_generation()
            updated = self.current_store.get_node(node_id)
            _trigger_webhooks({"type": "node.updated", "agent": agent, "node": updated}, customer_id=self._customer_id)
            self._json_response(200, updated)

        elif path.startswith("/edge/"):
            edge_id = path[6:]
            edge_id = validate_identifier(edge_id, name="edge_id")
            edge = self.current_store.get_edge(edge_id)
            if not edge:
                raise NodeNotFoundError(f"Edge not found: {edge_id}")

            # Enforce write boundary — only owner can update their edge (OHM-w9pj)
            enforce_write_boundary(self.current_store.conn, agent, edge_id)

            now = datetime.now(timezone.utc).isoformat()
            pert_fields = [
                "probability",
                "probability_p05",
                "probability_p50",
                "probability_p95",
                "confidence",
                "confidence_p05",
                "confidence_p50",
                "confidence_p95",
                "condition",
                "provenance",
                "urgency",
            ]
            update_fields = []
            update_params = []
            for field in pert_fields:
                if field in body:
                    update_fields.append(f"{field} = ?")
                    update_params.append(body[field])

            # Allow edge_type updates for causal restructuring (ADR-023)
            if "edge_type" in body:
                from ohm.validation import validate_identifier as _validate_id

                new_type = _validate_id(body["edge_type"], name="edge_type")
                update_fields.append("edge_type = ?")
                update_params.append(new_type)

            # Recompute PERT mean if p50 provided and probability not explicitly set
            if "probability_p50" in body and "probability" not in body:
                from ohm.pert import compute_pert_mean

                p05 = body.get("probability_p05", edge.get("probability_p05") or body["probability_p50"])
                p95 = body.get("probability_p95", edge.get("probability_p95") or body["probability_p50"])
                pert_mean = compute_pert_mean(p05, body["probability_p50"], p95)
                update_fields.append("probability = ?")
                update_params.append(pert_mean)

            if not update_fields:
                raise ValidationError("No updatable fields provided")

            update_fields.append("updated_at = ?")
            update_params.append(now)
            update_fields.append("updated_by = ?")
            update_params.append(agent)
            update_params.append(edge_id)

            self.current_store.conn.execute(
                f"UPDATE ohm_edges SET {', '.join(update_fields)} WHERE id = ?",
                update_params,
            )
            self.current_store._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"], agent_name=agent)
            self.current_store._increment_graph_generation()  # Invalidate Bayesian network cache on edge update
            updated = self.current_store.get_edge(edge_id)
            _trigger_webhooks({"type": "edge.updated", "agent": agent, "edge": updated}, customer_id=self._customer_id)
            self._json_response(200, updated)

        elif path == "/edges" or path.startswith("/edges/"):
            edges = body.get("edges", [])
            if not edges:
                raise ValidationError("No edges provided in 'edges' array")

            now = datetime.now(timezone.utc).isoformat()
            results = []
            errors = []
            _pert_fields = [
                "probability",
                "probability_p05",
                "probability_p50",
                "probability_p95",
                "confidence",
                "confidence_p05",
                "confidence_p50",
                "confidence_p95",
                "condition",
                "provenance",
                "urgency",
            ]

            for item in edges:
                edge_id = item.get("id")
                if not edge_id:
                    errors.append({"error": "missing id field", "item": item})
                    continue
                try:
                    edge_id = validate_identifier(edge_id, name="edge_id")
                except Exception:
                    errors.append({"error": f"Invalid edge id: {edge_id}"})
                    continue
                edge = self.current_store.get_edge(edge_id)
                if not edge:
                    errors.append({"error": f"Edge not found: {edge_id}"})
                    continue

                enforce_write_boundary(self.current_store.conn, agent, edge_id)

                update_fields = []
                update_params = []
                for field in _pert_fields:
                    if field in item:
                        update_fields.append(f"{field} = ?")
                        update_params.append(item[field])

                if "probability_p50" in item and "probability" not in item:
                    from ohm.pert import compute_pert_mean

                    p05 = item.get("probability_p05", edge.get("probability_p05") or item["probability_p50"])
                    p95 = item.get("probability_p95", edge.get("probability_p95") or item["probability_p50"])
                    pert_mean = compute_pert_mean(p05, item["probability_p50"], p95)
                    update_fields.append("probability = ?")
                    update_params.append(pert_mean)

                if not update_fields:
                    errors.append({"error": "No updatable fields provided", "edge_id": edge_id})
                    continue

                update_fields.append("updated_at = ?")
                update_params.append(now)
                update_fields.append("updated_by = ?")
                update_params.append(agent)
                update_params.append(edge_id)

                try:
                    self.current_store.conn.execute(
                        f"UPDATE ohm_edges SET {', '.join(update_fields)} WHERE id = ?",
                        update_params,
                    )
                    self.current_store._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"], agent_name=agent)
                    self.current_store._increment_graph_generation()
                    updated = self.current_store.get_edge(edge_id)
                    results.append(updated)
                except Exception as e:
                    errors.append({"error": str(e), "edge_id": edge_id})

            response = {"updated": results, "count": len(results)}
            if errors:
                response["errors"] = errors
            self._json_response(200 if not errors else 207, response)
        else:
            raise ValidationError(f"Unknown PATCH path: {path}")

    def _do_GET(self):
        """Handle GET requests — queries.

        /health and /ready are always open (infrastructure endpoints).
        Other GET endpoints: open when no tokens or --no-auth; require auth
        when tokens are configured.
        """
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        # Infrastructure endpoints bypass auth — dispatch directly
        infra_method = self._GET_EXACT.get(path) or self._GET_EXACT.get(path or "/")
        if infra_method and infra_method.startswith("_get_infra_"):
            getattr(self, infra_method)(path, qs)
            return

        # SSE endpoint — has its own auth logic
        if path == "/events" or path.startswith("/events/"):
            self._handle_sse_events(path, qs)
            return

        # Auth for all non-infrastructure GET endpoints
        # OHM-gwg: Public-read model (default) vs. require-read-auth
        #   - --no-auth: all requests allowed (dev mode)
        #   - require_read_auth=True: all requests require valid Bearer token
        #   - require_read_auth=False (default): reads are public, writes need auth
        #   - no tokens configured: GET allowed (transition mode for fresh installs)

        # OHM-856: Gate /admin/* GET endpoints behind admin auth, mirroring
        # the existing POST-side gate at line 2521. Admin diagnostics
        # (health, duplicates, islands, snapshots, etc.) must not be
        # accessible anonymously, even in public-read mode.
        if path.startswith("/admin/"):
            admin_agent = self._require_admin("administrative maintenance endpoints")
            agent = admin_agent or "admin"
        else:
            agent = self._authenticate()
            if agent is None:
                if self.no_auth or not self.tokens:
                    agent = "ohm"
                elif self.require_read_auth:
                    raise AuthenticationError("Authentication required — provide Bearer token")
                else:
                    # Public-read model: unauthenticated reads allowed
                    agent = "ohm"

        self._current_agent = agent

        # pre_query hooks — can modify qs or block with 403
        try:
            qs = self._run_pre_query_hooks(agent, path, qs)
        except Exception:
            logging.getLogger(__name__).exception("pre_query hooks failed, continuing without modification")
        if qs is None:
            return

        method_name = self._GET_EXACT.get(path)
        if method_name is None:
            for prefix, mn in self._GET_PREFIXES:
                if path.startswith(prefix):
                    method_name = mn
                    break
        if method_name:
            has_post_query = self._has_post_query_hooks()
            if has_post_query:
                self._buffer_json_response = True
                self._json_response_buffer = None
            getattr(self, method_name)(path, qs)
            if has_post_query:
                buf = getattr(self, "_json_response_buffer", None)
                if buf is not None:
                    code, data = buf
                    decorated = self._run_post_query_hooks(agent, path, qs, data)
                    self._buffer_json_response = False
                    self._json_response(code, decorated)
                else:
                    self._buffer_json_response = False
            else:
                pass
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def _run_pre_query_hooks(self, agent: str, path: str, qs: dict) -> dict | None:
        """Run pre_query hooks. Returns modified qs, or None if blocked (403 sent)."""
        from ohm.hooks import HookRunner

        runner = HookRunner(self.current_store.conn)
        results = runner.run_hooks("pre_query", {"agent": agent, "path": path, "query_params": qs})
        for r in results:
            if not r.success:
                self._json_response(
                    403,
                    {
                        "error": "hook_rejected",
                        "hook_id": r.hook_id,
                        "exit_code": r.exit_code,
                        "message": r.stderr or "Query blocked by hook",
                        "timed_out": r.timed_out,
                    },
                )
                return None
            if r.success and r.stdout.strip():
                try:
                    override = json.loads(r.stdout.strip())
                    if isinstance(override, dict) and "query_params" in override:
                        qs.update(override["query_params"])
                except json.JSONDecodeError:
                    pass
        return qs

    def _has_post_query_hooks(self) -> bool:
        """Check if any post_query hooks are registered."""
        from ohm.hooks import HookRunner

        try:
            runner = HookRunner(self.current_store.conn)
            return len(runner.get_hooks("post_query")) > 0
        except Exception:
            return False

    def _run_post_query_hooks(self, agent: str, path: str, qs: dict, data: dict) -> dict:
        """Run post_query hooks. Merge JSON stdout into response under hook_decorations."""
        from ohm.hooks import HookRunner

        runner = HookRunner(self.current_store.conn)
        results = runner.run_hooks("post_query", {"agent": agent, "path": path, "query_params": qs, "response_body": data})
        decorations = {}
        for r in results:
            if r.success and r.stdout.strip():
                try:
                    merge = json.loads(r.stdout.strip())
                    if isinstance(merge, dict):
                        decorations.update(merge)
                except json.JSONDecodeError:
                    pass
            elif not r.success:
                logging.getLogger(__name__).warning(
                    "post_query hook %s failed (exit_code=%d): %s",
                    r.hook_id,
                    r.exit_code,
                    r.stderr,
                )
        if decorations:
            data["hook_decorations"] = decorations
        return data

    # ── Authenticated GET handlers ────────────────────────────

    def _do_POST(self):
        """Handle POST requests — writes. Requires auth + write access.

        Fail-closed by default: writes are denied unless a valid token is
        provided. Use --no-auth or OHM_NO_AUTH=1 for dev mode.
        """
        if self.no_auth:
            agent = self._authenticate() or "ohm"
        else:
            agent = self._authenticate()
            if agent is None:
                raise AuthenticationError("Authentication required — provide Bearer token")
        self._check_write_access(agent)

        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Administrative maintenance endpoints (/admin/*) mutate global graph
        # state — purge/merge/decay/backfill/eviction/beads-sync — and must
        # require the admin role, not merely write access. Normal agent writes
        # go through /node, /edge, /observe, etc., never /admin/*. Gating here
        # (rather than per-handler) covers the whole surface, including any
        # future admin endpoints. _require_admin() bypasses in no_auth mode.
        if path.startswith("/admin/"):
            self._require_admin("administrative maintenance endpoints")

        qs = parse_qs(parsed.query)
        body = self._read_body(allow_raw=path == "/documents/upload")
        content_type = self.headers.get("Content-Type", "")

        # Multipart file uploads are dispatched before JSON validation so the
        # handler can parse the raw request body that _read_body returned.
        if path == "/documents/upload" and content_type.startswith("multipart/form-data"):
            getattr(self, "_post_documents_upload")(path, qs, body, agent)
            return

        body = self._validate_body(path, body)

        method_name = self._POST_EXACT.get(path)
        if method_name is None:
            for prefix, mn in self._POST_PREFIXES:
                if path.startswith(prefix):
                    method_name = mn
                    break
        if method_name:
            # OHM-cwrc: serialise ALL writes through this server instance. DuckDB
            # is single-writer per connection; without this, concurrent POSTs
            # against the same store race and corrupt the graph (50 writes → 55
            # nodes, ConstraintException on agent_name PK, etc.). The per-tenant
            # lock (when present) nests inside this one as a re-entrant lock.
            with self._write_lock:
                # Acquire the per-tenant write lock for multi-tenant customer
                # requests. DuckDB is single-writer; serializing at this layer
                # prevents concurrent write conflicts within the same tenant's
                # isolated DuckDB file. Agent token requests (customer_id=None)
                # and single-tenant mode rely solely on the per-instance lock
                # above.
                customer_id = self._customer_id
                if customer_id and self.tenant_manager:
                    from ohm.tenant import TenantNotFoundError

                    try:
                        write_lock = self.tenant_manager.get_write_lock(customer_id)
                    except TenantNotFoundError:
                        raise NodeNotFoundError("Tenant not found — provision this tenant before use")
                    with write_lock:
                        getattr(self, method_name)(path, qs, body, agent)
                else:
                    getattr(self, method_name)(path, qs, body, agent)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def _do_DELETE(self):
        """Handle DELETE requests — remove nodes or edges.

        DELETE /node/{id} — removes a node and its associated edges.
        DELETE /edge/{id} — removes an edge.

        Requires auth. Agents can only delete their own nodes/edges.
        Idempotent: returns 404 on second call (not 500).
        """
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Auth required for DELETE
        agent = self._authenticate()
        if agent is None:
            if self.no_auth:
                agent = "ohm"
            else:
                raise AuthenticationError("Authentication required — provide Bearer token")

        method_name = None
        for prefix, mn in self._DELETE_PREFIXES:
            if path.startswith(prefix):
                method_name = mn
                break
        if method_name:
            self._check_write_access(agent)
            # OHM-cwrc: serialise writes (see _do_POST).
            with self._write_lock:
                customer_id = self._customer_id
                if customer_id and self.tenant_manager:
                    from ohm.tenant import TenantNotFoundError

                    try:
                        write_lock = self.tenant_manager.get_write_lock(customer_id)
                    except TenantNotFoundError:
                        raise NodeNotFoundError("Tenant not found — provision this tenant before use")
                    with write_lock:
                        getattr(self, method_name)(path, agent)
                else:
                    getattr(self, method_name)(path, agent)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def _do_PUT(self):
        """Handle PUT requests - config updates (OHM-801).

        Requires auth. Dispatches to _PUT_EXACT handler table.
        """
        if self.no_auth:
            agent = self._authenticate() or "ohm"
        else:
            agent = self._authenticate()
            if agent is None:
                raise AuthenticationError("Authentication required - provide Bearer token")

        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        body = self._read_body()
        body = self._validate_body(path, body)

        method_name = self._PUT_EXACT.get(path)
        if method_name:
            with self._write_lock:
                customer_id = self._customer_id
                if customer_id and self.tenant_manager:
                    from ohm.tenant import TenantNotFoundError

                    try:
                        write_lock = self.tenant_manager.get_write_lock(customer_id)
                    except TenantNotFoundError:
                        raise NodeNotFoundError("Tenant not found - provision this tenant before use")
                    with write_lock:
                        getattr(self, method_name)(path, qs, body, agent)
                else:
                    getattr(self, method_name)(path, qs, body, agent)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})


# ── Dispatch table population ─────────────────────────────────────────────────
# Populated after class body so method names are valid references.

OhmHandler._DELETE_PREFIXES = [
    ("/node/", "_delete_node"),
    ("/edge/", "_delete_edge"),
    ("/hooks/", "_delete_hook"),
    ("/tenant/", "_delete_tenant_prefix"),
]

# OHM-801: PUT dispatch table
OhmHandler._PUT_EXACT = {
    "/config": "_put_config",
}

OhmHandler._POST_EXACT = {
    "/tenant/provision": "_post_tenant_provision",
    "/scratch": "_post_scratch",
    "/node": "_post_node",
    "/node/find_or_create": "_post_node_find_or_create",
    "/edge": "_post_edge",
    "/observations": "_post_observations",
    "/outcome": "_post_outcome",
    "/data-products": "_post_data_product",
    "/agent/synthesis": "_post_synthesis",
    "/skill": "_post_skill",
    "/runbook": "_post_runbook",
    "/ask": "_post_ask",
    "/batch": "_post_batch",
    "/webhook": "_post_webhook",
    "/state": "_post_state",
    "/register": "_post_register",
    "/heartbeat": "_post_heartbeat",
    "/vault/promote": "_post_vault_promote",
    "/sync": "_post_sync",
    "/documents/upload": "_post_documents_upload",
    "/documents/bedrock/retrieve": "_post_document_bedrock_retrieve",
    "/documents/bedrock/retrieve-and-generate": "_post_document_bedrock_retrieve_and_generate",
    "/tasks": "_post_task",
    "/deduplicate": "_post_deduplicate",
    "/admin/checkpoint": "_post_admin_checkpoint",
    "/admin/cleanup-hooks": "_post_admin_cleanup_hooks",
    "/admin/observation-source-urls": "_post_admin_observation_source_urls",
    "/admin/source-node-urls": "_post_admin_source_node_urls",
    "/admin/edge-layer-fix": "_post_admin_edge_layer_fix",
    "/admin/pert-backfill": "_post_admin_pert_backfill",
    "/admin/backfill-relational-tags": "_post_admin_backfill_relational_tags",
    "/admin/verification-decay": "_post_admin_verification_decay",
    "/admin/apply-decay": "_post_admin_apply_decay",
    "/admin/merge": "_post_admin_merge",
    "/admin/backfill-aliases": "_post_admin_backfill_aliases",
    "/admin/backfill-content-hashes": "_post_admin_backfill_content_hashes",
    "/admin/backfill-source-urls": "_post_admin_backfill_source_urls",
    "/admin/vacuum-lake": "_post_admin_vacuum_lake",
    "/admin/evict-fragments": "_post_admin_evict_fragments",
    "/admin/purge-orphans": "_post_admin_purge_orphans",
    "/admin/repair-dangling": "_post_admin_repair_dangling",
    "/admin/sync-beads": "_post_admin_sync_beads",
    "/bootstrap": "_post_bootstrap",
    "/discover/queue/review": "_post_discovery_review",
    "/metrics/semantic/actions": "_post_metrics_semantic_actions",
    "/hooks": "_post_hooks",
}

OhmHandler._POST_PREFIXES = [
    ("/tenant/", "_post_tenant_export"),
    ("/challenge/", "_post_challenge"),
    ("/support/", "_post_support"),
    ("/observe/", "_post_observe"),
    ("/fragments/", "_post_fragment_action"),
    ("/observation/", "_post_observation_supersede"),
    ("/node/sign/", "_post_node_sign"),
    ("/node/verify/", "_post_node_verify"),
    ("/edge/sign/", "_post_edge_sign"),
    ("/edge/verify/", "_post_edge_verify"),
    ("/nudges/", "_post_nudge_accept"),
    ("/documents/", "_post_document_sync_to_bedrock"),
    ("/tasks/", "_post_task_action"),
    ("/admin/hooks/", "_post_admin_hooks_stage"),
]

OhmHandler._GET_EXACT = {
    "/": "_get_infra_root",
    "": "_get_infra_root",
    "/openapi.json": "_get_infra_openapi",
    "/health": "_get_infra_health",
    "/instance": "_get_instance",
    "/ready": "_get_infra_ready",
    "/metrics": "_get_infra_metrics",
    "/perf": "_get_perf",
    "/stats": "_get_stats",
    "/status": "_get_status",
    "/schema": "_get_schema",
    "/schema/node-types": "_get_schema_node_types",
    "/templates": "_get_templates",
    "/queries": "_get_queries",
    "/plans": "_get_plans",
    "/reports": "_get_reports",
    "/runs": "_get_runs",
    "/rul": "_get_rul",
    "/layers": "_get_layers",
    "/agents": "_get_agents",
    "/nodes": "_get_nodes",
    "/tasks": "_get_tasks",
    "/data-products": "_get_data_products",
    "/listen": "_get_listen",
    "/search": "_get_search",
    "/semantic_search": "_get_semantic_search",
    "/vault": "_get_vault",
    "/health/graph": "_get_health_graph",
    "/health/agents": "_get_health_agents",
    "/health/sync": "_get_health_sync",
    "/contradictions": "_get_contradictions",
    "/anomalies": "_get_anomalies",
    "/stale": "_get_stale",
    "/decay": "_get_decay",
    "/duplicates": "_get_duplicates",
    "/fragments": "_get_fragments",
    "/orphans": "_get_orphans",
    "/islands": "_get_islands",
    "/welcome": "_get_welcome",
    "/orient": "_get_orient",
    "/contributions": "_get_contributions",
    "/changes": "_get_changes",
    "/hubs": "_get_hubs",
    "/dead_ends": "_get_dead_ends",
    "/centrality": "_get_centrality",
    "/communities": "_get_communities",
    "/bridges": "_get_bridges",
    "/granger": "_get_granger",
    "/edge_stability": "_get_edge_stability",
    "/suggest": "_get_suggest",
    "/suggest_connections": "_get_suggest",
    "/graph/stats": "_get_graph_stats",
    "/lint": "_get_lint",
    "/doctor": "_get_doctor",
    "/contract": "_get_contract",
    "/inference": "_get_inference",
    "/belief": "_get_belief",
    "/intervene": "_get_intervene",
    "/ate": "_get_ate",
    "/sensitivity": "_get_sensitivity",
    "/adjustment": "_get_adjustment",
    "/voi": "_get_voi",
    "/markov/absorbing": "_get_markov_absorbing",
    "/markov/expected_steps": "_get_markov_expected_steps",
    "/voi/tasks": "_get_voi_tasks",
    "/suggest_causes": "_get_suggest_causes",
    "/deduplicate": "_get_deduplicate",
    "/refute": "_get_refute",
    "/regime": "_get_regime",
    "/game": "_get_game",
    "/nash": "_get_nash",
    "/policy": "_get_policy",
    "/discover": "_get_discover",
    "/discover/queue": "_get_discovery_queue",
    "/hooks": "_get_hooks",
    "/admin/checkpoint": "_get_admin_checkpoint",
    "/admin/embeddings": "_get_admin_embeddings",
    "/admin/embeddings/status": "_get_admin_embeddings_status",
    "/admin/islands": "_get_admin_islands",
    "/admin/verification-scan": "_get_admin_verification_scan",
    "/admin/constraint-report": "_get_admin_constraint_report",
    "/admin/health": "_get_admin_health",
    "/admin/orphan-triage": "_get_admin_orphan_triage",
    "/admin/learned-half-lives": "_get_admin_learned_half_lives",
    "/admin/snapshots": "_get_admin_snapshots",
    "/admin/nudges/quality": "_get_nudge_quality",
    "/config": "_get_config",
    "/bootstrap": "_get_bootstrap",
    "/graph/at": "_get_graph_at",
    "/graph/changes": "_get_graph_changes",
    "/observations": "_get_observations",
    "/source_reliability": "_get_source_reliability",
    "/webhooks/dead-letter": "_get_webhooks_dead_letter",
    "/webhooks/outbox": "_get_webhooks_outbox",
    "/metrics/semantic": "_get_metrics_semantic",
    "/resolve": "_get_resolve",
    "/admin/alias-duplicates": "_get_alias_duplicates",
    "/admin/duplicates": "_get_admin_duplicates",
    "/admin/fragment-resonance": "_get_fragment_resonance",
    "/tenants": "_get_tenants",
    "/edges": "_get_edges",
}


OhmHandler._GET_PREFIXES = [
    ("/tenant/", "_get_tenant_prefix"),
    ("/context-gate/", "_get_context_gate"),
    ("/decision/", "_get_decision_recommendation"),
    ("/documents/", "_get_document_prefix"),
    ("/node/", "_get_node"),
    ("/deep/", "_get_deep"),
    ("/edge/", "_get_edge"),
    ("/data-products/", "_get_data_product"),
    ("/neighborhood/", "_get_neighborhood"),
    ("/path/", "_get_path"),
    ("/impact/", "_get_impact"),
    ("/confidence/", "_get_confidence"),
    ("/agent/", "_get_agent"),
    ("/aggregate/", "_get_aggregate"),
    ("/gap/", "_get_gap"),
    ("/trajectory/", "_get_trajectory"),
    ("/provenance/", "_get_provenance"),
    ("/runbook/", "_get_runbook_steps"),
    ("/monte-carlo/", "_get_monte_carlo"),
    ("/calibration/", "_get_calibration"),
    ("/agent_profile/", "_get_agent_profile"),
    ("/reliability/", "_get_reliability"),
    ("/compound_confidence/", "_get_compound_confidence"),
    ("/observation/", "_get_observation"),
    ("/synthesis/", "_get_synthesis_validity"),
    ("/narrative/", "_get_narrative"),
    ("/lineage/", "_get_lineage"),
    ("/contradiction/", "_get_contradiction_summary"),
    ("/task-context/", "_get_task_context"),
    ("/timeline/", "_get_timeline_rollup"),
    ("/report/", "_get_report"),
    ("/run/", "_get_run"),
]

# Exact GET routes that aren't in the prefix list
OhmHandler._GET_EXACT["/confidence-report"] = "_get_confidence_report"
OhmHandler._POST_EXACT["/scenario"] = "_post_scenario"
OhmHandler._POST_EXACT["/propose-action"] = "_post_propose_action"
OhmHandler._POST_EXACT["/execute-action"] = "_post_execute_action"
OhmHandler._GET_EXACT["/loop-status"] = "_get_loop_status"
OhmHandler._POST_EXACT["/twin/register"] = "_post_register_twin"
OhmHandler._POST_EXACT["/twin/register-with-bindings"] = "_post_register_twin_with_bindings"
OhmHandler._POST_EXACT["/twin/assemble"] = "_post_assemble_twin"
OhmHandler._POST_EXACT["/twin/design/start"] = "_post_twin_design_start"
OhmHandler._POST_PREFIXES.append(("/twin/design/", "_route_twin_design_post"))
OhmHandler._GET_PREFIXES.append(("/twin/design/", "_route_twin_design_get"))
OhmHandler._POST_PREFIXES.append(("/twin/", "_post_validate_action"))
OhmHandler._GET_PREFIXES.append(("/twin/", "_route_twin_get"))
OhmHandler._POST_EXACT["/twin-template"] = "_post_create_twin_template"
OhmHandler._POST_PREFIXES.append(("/twin-template/", "_post_instantiate_twin"))
OhmHandler._GET_EXACT["/twin-templates"] = "_get_twin_templates"
OhmHandler._GET_PREFIXES.append(("/twin-template/", "_route_twin_template_get"))
OhmHandler._POST_EXACT["/model/register"] = "_post_register_model_candidate"
OhmHandler._POST_EXACT["/model/shadow"] = "_post_register_shadow_model"
OhmHandler._POST_PREFIXES.append(("/model/", "_route_model_post"))
OhmHandler._GET_PREFIXES.append(("/model/", "_route_model_get"))
OhmHandler._GET_EXACT["/model/compare"] = "_get_compare_models"

OhmHandler._POST_EXACT["/temporal/freshness"] = "_post_set_freshness_threshold"
OhmHandler._POST_EXACT["/temporal/feed-investment"] = "_post_compute_feed_investment"
OhmHandler._POST_EXACT["/temporal/mode-switch"] = "_post_record_mode_switch"
OhmHandler._GET_EXACT["/verifications/detect"] = "_get_detect_verifications"
OhmHandler._POST_EXACT["/verifications/nudge"] = "_post_create_nudge"
OhmHandler._POST_EXACT["/verifications/outcome"] = "_post_record_verification_outcome"
OhmHandler._GET_EXACT["/verifications/pending"] = "_get_list_verifications"
OhmHandler._GET_EXACT["/edge/suggest-type"] = "_get_edge_suggest_type"
OhmHandler._GET_PREFIXES.append(("/temporal/", "_route_temporal_get"))

# ── Prospect lifecycle (OHM-844) ──────────────────────────────────────────
OhmHandler._POST_EXACT["/prospect"] = "_post_prospect"
OhmHandler._POST_PREFIXES.append(("/prospect/transition/", "_post_prospect_transition"))
OhmHandler._GET_EXACT["/prospects"] = "_get_prospects"
OhmHandler._GET_PREFIXES.append(("/prospect/", "_get_prospect_detail"))

# ── Monte Carlo prospect simulation (OHM-843) ──────────────────────────────
OhmHandler._POST_PREFIXES.append(("/simulate/", "_post_simulate"))

# ── Decision hypothesis autoresearch (OHM-845) ────────────────────────────
OhmHandler._POST_PREFIXES.append(("/decision/", "_post_decision_autoresearch"))

# ── Type-level promotion autoresearch (OHM-846) ───────────────────────────
OhmHandler._POST_PREFIXES.append(("/type-proposal/", "_route_type_proposal_post"))
OhmHandler._GET_EXACT["/type-proposals"] = "_get_type_proposals"

# ── Nudge-message optimization autoresearch (OHM-847) ──────────────────────
OhmHandler._POST_EXACT["/nudge/evaluate"] = "_post_nudge_evaluate"
OhmHandler._POST_EXACT["/nudge/promote"] = "_post_nudge_promote"
OhmHandler._POST_EXACT["/nudge/demote"] = "_post_nudge_demote"

# ── Skill maintenance loop (OHM-854) ─────────────────────────────────────
OhmHandler._POST_EXACT["/admin/skill-maintenance/run"] = "_post_skill_maintenance_run"


def make_configured_handler(store: OhmStore):
    """OHM-1s14.1: bind `store` via handler closure rather than mutating
    ``OhmHandler`` as a class-level global.

    Each handler instance receives its own snapshot of ``store`` in
    ``__init__``, eliminating the cross-thread bleed risk identified in the
    OHM-ym2f audit: even if a future caller mutates ``OhmHandler.store``
    mid-flight, in-flight handlers keep their own bound copy.

    All other configuration (config, tokens, roles, schema_config,
    multi_tenant, tenant_manager, ...) continues to live on the class via
    ``install_globals`` — those will move to instance scope in follow-up
    issues (OHM-1s14.2 / OHM-1s14.3 / OHM-1s14.4).
    """

    class _ConfiguredHandler(OhmHandler):
        def __init__(self, *args, **kwargs):
            # OHM-fix-handler-store: bind store BEFORE super().__init__().
            # BaseHTTPRequestHandler.__init__() may call self.handle() before
            # returning, which can route to an endpoint that accesses
            # self.current_store. If store is set after super(), endpoints see
            # None and fail with "'NoneType' object has no attribute 'conn'".
            self.store = store
            super().__init__(*args, **kwargs)
            # Also store on the server for fallback access (safe after super).
            if hasattr(self, "server") and self.server is not None:
                self.server._ohm_store = store

    _ConfiguredHandler.__name__ = "OhmHandler"
    _ConfiguredHandler.__qualname__ = "OhmHandler"
    return _ConfiguredHandler


def _apply_ohm_meta_config(conn, config: dict, file_config: dict | None = None) -> None:
    """Merge behavioral config from ohm_meta into the in-memory config dict (OHM-796).

    Merge order: DEFAULT_CONFIG < ohm_meta (DB) < ohmd.json (file) < env < CLI.
    Only fills in a key if it is NOT already set in the file config.
    File/env/CLI values always win over DB values.

    Args:
        conn: DuckDB connection (for reading ohm_meta)
        config: The already-merged config dict (DEFAULT_CONFIG + file + env + CLI)
        file_config: The raw file config dict (before DEFAULT_CONFIG merge).
            If None, ohm_meta values apply whenever the config still matches
            the default (best-effort fallback).
    """
    from ohm.graph.schema import get_meta

    if file_config is None:
        file_config = {}

    def _bool(val: str | None) -> bool | None:
        if val is None:
            return None
        return val.lower() in ("true", "1", "yes")

    def _int(val: str | None) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except ValueError:
            return None

    file_sl = file_config.get("semantic_layer", {})
    sl = config.setdefault("semantic_layer", dict(DEFAULT_CONFIG["semantic_layer"]))
    if "auto_actions_enabled" not in file_sl:
        v = _bool(get_meta(conn, "semantic_layer.enabled"))
        if v is not None:
            sl["auto_actions_enabled"] = v
    if "auto_actions_interval_seconds" not in file_sl:
        v = _int(get_meta(conn, "semantic_layer.interval_sec"))
        if v is not None:
            sl["auto_actions_interval_seconds"] = v
    if "auto_actions_rate_limit_seconds" not in file_sl:
        v = _int(get_meta(conn, "semantic_layer.rate_limit_sec"))
        if v is not None:
            sl["auto_actions_rate_limit_seconds"] = v

    file_bs = file_config.get("beads_sync", {})
    bs = config.setdefault("beads_sync", dict(DEFAULT_CONFIG["beads_sync"]))
    if "enabled" not in file_bs:
        v = _bool(get_meta(conn, "beads_sync.enabled"))
        if v is not None:
            bs["enabled"] = v
    if "interval_seconds" not in file_bs:
        v = _int(get_meta(conn, "beads_sync.interval_sec"))
        if v is not None:
            bs["interval_seconds"] = v
    if "startup_sync" not in file_bs:
        v = _bool(get_meta(conn, "beads_sync.startup_sync"))
        if v is not None:
            bs["startup_sync"] = v

    file_dl = file_config.get("ducklake", {})
    dl = config.setdefault("ducklake", {})
    if "sync_interval_seconds" not in file_dl:
        v = _int(get_meta(conn, "ducklake.sync_interval_sec"))
        if v is not None:
            dl["sync_interval_seconds"] = v

    file_br = file_config.get("bedrock", {})
    br = config.setdefault("bedrock", {})
    for key, meta_key in [
        ("knowledge_base_id", "bedrock.kb_id"),
        ("data_source_id", "bedrock.data_source_id"),
        ("region", "bedrock.region"),
    ]:
        if key not in file_br:
            v = get_meta(conn, meta_key)
            if v is not None:
                br[key] = v

    if "onboarding_node_id" not in file_config:
        v = get_meta(conn, "onboarding_node_id")
        if v is not None:
            config["onboarding_node_id"] = v

    if "agent_onboarding_enabled" not in file_config:
        v = _bool(get_meta(conn, "agent_onboarding_enabled"))
        if v is not None:
            config["agent_onboarding_enabled"] = v


def run_server(config: dict, store: OhmStore, schema_config: SchemaConfig | None = None, file_config: dict | None = None):
    """Run the HTTP server.

    When Quack is enabled and available, starts a Quack server on the
    DuckDB connection for concurrent multi-writer access. The HTTP
    handler continues to serve OHM-specific endpoints (auth, boundary
    enforcement, challenge semantics) regardless.

    If Quack is not available, falls back to the HTTP-only mode.

    Args:
        config: Server configuration dict.
        store: OhmStore instance for database access.
        schema_config: SchemaConfig to use (default: DEFAULT_SCHEMA).
            Pass TOPO_SCHEMA for the industrial knowledge graph variant.
    """
    if schema_config is None:
        schema_config = DEFAULT_SCHEMA

    # OHM-796: Apply behavioral config from ohm_meta (DB-sourced).
    # Merge order: DEFAULT_CONFIG < ohm_meta < ohmd.json < env vars < CLI.
    # Only fills in keys NOT already set in the file config.
    _apply_ohm_meta_config(store.conn, config, file_config=file_config)

    OhmHandler.config = config
    token_hashes, config_roles = _build_token_lookup(config.get("tokens", {}))
    OhmHandler.tokens = token_hashes
    OhmHandler.customer_tokens = _build_customer_token_lookup(config.get("customer_tokens", {}))
    resolved_roles = config_roles if config_roles else config.get("roles", {})
    # OHM-1s14.2: scope roles by customer_id in multi-tenant mode to prevent
    # collisions when two tenants reuse the same agent name (e.g., both have
    # an "admin" agent with different privileges). The "" key is the operator
    # scope (global agents like metis); tenant-specific roles live under their
    # customer_id. The _lookup_role helper transparently supports both flat
    # and scoped formats, so legacy single-tenant configs keep working.
    if config.get("multi_tenant", False) and resolved_roles:
        sample = next(iter(resolved_roles.values()), None)
        if not isinstance(sample, dict):
            OhmHandler.roles = {"": resolved_roles}
        else:
            OhmHandler.roles = resolved_roles
    else:
        OhmHandler.roles = resolved_roles
    OhmHandler.no_auth = config.get("no_auth", False)
    OhmHandler.require_read_auth = config.get("require_read_auth", False)
    OhmHandler.schema_config = schema_config
    OhmHandler.multi_tenant = config.get("multi_tenant", False)
    # OHM-rlfw: trust X-Forwarded-For only from explicitly configured proxies.
    trusted = config.get("trusted_proxies")
    if trusted is None:
        trusted = os.environ.get("OHM_TRUSTED_PROXIES", "")
    OhmHandler.TRUSTED_PROXIES = frozenset(p.strip() for p in trusted.split(",") if p.strip())

    # OHM-whbk: hydrate the in-memory webhook registry from DuckDB so
    # registrations survive restarts. Single-tenant mode keys on "".
    persisted = store.load_webhook_subscriptions()
    for cid, agents in persisted.items():
        cid_key: str | None = cid or None
        OhmHandler._webhook_registry[cid_key] = dict(agents)

    # ── TenantManager (OHM-tss4.4) ────────────────────────────────────────
    if OhmHandler.multi_tenant:
        from ohm.tenant import TenantManager

        tenants_dir = config.get("tenants_dir", str(Path(config.get("db_path", "ohm.duckdb")).parent / "tenants"))
        templates_dir = config.get("templates_dir", None)
        max_cached = int(config.get("tenant_cache_size", 100))
        OhmHandler.tenant_manager = TenantManager(
            tenants_dir=tenants_dir,
            templates_dir=templates_dir,
            max_cached=max_cached,
        )
        print(f"TenantManager: {tenants_dir} (cache={max_cached})", file=sys.stderr)
        if "require_read_auth" not in config:
            OhmHandler.require_read_auth = True

    # ── pgmpy pre-warm (OHM-a689): avoid cold-import penalty on first inference ──
    _prewarm_pgmpy_async()

    # ── Register built-in hooks (OHM-aznh.11) ────────────────────────────
    _register_builtin_hooks(store)

    # ── Quack integration ──────────────────────────────────────────────
    quack_info: dict[str, Any] | None = None
    if config.get("quack", False):
        from ohm.quack import is_available, start_server

        if is_available(store.conn):
            quack_uri = config.get("quack_uri", "quack:localhost")
            quack_token_env = config.get("quack_token_env", "QUACK_TOKEN")
            try:
                quack_info = start_server(
                    store.conn,
                    uri=quack_uri,
                    token_env=quack_token_env,
                )
                print(f"Quack server started: {quack_uri}", file=sys.stderr)
            except Exception as e:
                print(f"Quack server failed to start: {e}", file=sys.stderr)
                print("Falling back to HTTP-only mode", file=sys.stderr)
                quack_info = None
        else:
            print("Quack extension not available — using HTTP-only mode", file=sys.stderr)

    server = ThreadedHTTPServer((config["host"], config["port"]), make_configured_handler(store))
    server._ohm_store = store  # Fallback for current_store if handler.store is None

    # OHM-860: Warn when binding to non-loopback with public-read auth and no tokens
    _host = config.get("host", "127.0.0.1")
    _is_loopback = _host in ("127.0.0.1", "localhost", "::1") or _host.startswith("127.")
    if not _is_loopback and not config.get("require_read_auth", False) and not config.get("no_auth", False):
        _has_tokens = bool(config.get("tokens") or config.get("customer_tokens"))
        if not _has_tokens:
            import warnings as _warnings
            _warnings.warn(
                f"OHM is binding to {_host}:{config['port']} with public-read auth and no tokens configured — "
                "the entire graph is readable by anyone who can reach this port. "
                "Set require_read_auth=True or configure tokens to secure this instance.",
                stacklevel=2,
            )
            print(
                f"WARNING: binding to {_host}:{config['port']} with public-read auth and no tokens — "
                "the entire graph is readable by anyone who can reach this port.",
                file=sys.stderr,
            )

    print(f"OHM daemon listening on {config['host']}:{config['port']}", file=sys.stderr)

    # Background DuckLake sync thread (OHM-1rwl): sync every sync_interval_seconds
    # so data is not lost between heartbeats on hard shutdown (SIGKILL/OOM).
    # OHM-v40d: read from ducklake sub-config first, fall back to top-level for compat.
    _top_level_sync = config.get("sync_interval_seconds")
    ducklake_sync_interval = config.get("ducklake", {}).get("sync_interval_seconds", _top_level_sync if _top_level_sync is not None else 60)
    if _top_level_sync is not None and config.get("ducklake", {}).get("sync_interval_seconds") is not None and _top_level_sync != config["ducklake"]["sync_interval_seconds"]:
        logger.warning(
            "Conflicting sync_interval_seconds: top-level=%s, ducklake=%s — using ducklake value",
            _top_level_sync,
            config["ducklake"]["sync_interval_seconds"],
        )
    ducklake_sync_max_retries = config.get("ducklake", {}).get("sync_max_retries", config.get("sync_max_retries", 20))
    _sync_stop = threading.Event()
    _sync_thread: threading.Thread | None = None  # OHM-inq1: declared so the shutdown join sees a defined name.

    def _ducklake_sync_loop():
        consecutive_sync_errors = 0
        while not _sync_stop.wait(ducklake_sync_interval):
            try:
                store.sync_heartbeat()
                consecutive_sync_errors = 0  # Reset on success (OHM-nnu2)
            except Exception:
                consecutive_sync_errors += 1
                logger.exception("DuckLake sync failed (attempt %d): ", consecutive_sync_errors)
                if consecutive_sync_errors >= ducklake_sync_max_retries:
                    # Circuit breaker: after max consecutive errors, stop retrying.
                    # Prevents infinite retry loops when DuckLake is permanently
                    # unavailable (corrupted catalog, disk full, etc.).
                    logger.critical(
                        "DuckLake sync circuit breaker opened after %d consecutive errors — sync thread stopped. Restart the daemon or manually trigger sync via POST /sync to re-enable.",
                        consecutive_sync_errors,
                    )
                    break
                if consecutive_sync_errors >= 3:
                    # Exponential backoff after 3 consecutive errors (OHM-nnu2, same pattern as OHM-s8sg)
                    backoff = min(60, 5 * (2 ** (consecutive_sync_errors - 3)))
                    logger.warning("DuckLake sync backing off %ds after %d consecutive errors", backoff, consecutive_sync_errors)
                    _sync_stop.wait(backoff)  # Interruptible sleep

    if hasattr(store, "sync_heartbeat") and ducklake_sync_interval > 0:
        _sync_thread = threading.Thread(target=_ducklake_sync_loop, daemon=True, name="ducklake-sync")
        _sync_thread.start()

    # OHM-776: Periodic checkpoint thread — bounds the worst-case data-loss
    # window to the checkpoint interval. Without this, writes between
    # checkpoints are lost on non-graceful daemon exit (SIGKILL, crash).
    #
    # OHM-785: DuckDB connections are NOT safe for concurrent use from
    # multiple threads. The checkpoint thread must acquire the store's
    # write lock before issuing CHECKPOINT, otherwise it races with
    # request handlers executing queries on the same connection.
    checkpoint_interval = config.get("checkpoint_interval_seconds", 60)
    _checkpoint_stop = threading.Event()
    _checkpoint_thread: threading.Thread | None = None

    def _safe_checkpoint():
        """Run CHECKPOINT while holding the store's write lock (OHM-785)."""
        write_lock = getattr(store, "_lock", None)
        if write_lock is not None:
            with write_lock:
                store.conn.execute("CHECKPOINT")
        else:
            store.conn.execute("CHECKPOINT")

    def _checkpoint_loop():
        # OHM-776: Fire first checkpoint shortly after startup (5s) rather
        # than waiting a full interval — bounds the data-loss window from
        # startup, not just from the first periodic tick.
        startup_delay = min(5, checkpoint_interval)
        if _checkpoint_stop.wait(startup_delay):
            return
        try:
            _safe_checkpoint()
        except Exception:
            logger.exception("Startup CHECKPOINT failed")
        while not _checkpoint_stop.wait(checkpoint_interval):
            try:
                _safe_checkpoint()
            except Exception:
                logger.exception("Periodic CHECKPOINT failed")

    if checkpoint_interval > 0:
        _checkpoint_thread = threading.Thread(target=_checkpoint_loop, daemon=True, name="periodic-checkpoint")
        _checkpoint_thread.start()

    # OHM-a5rz.27: Background fragment eviction thread
    eviction_config = config.get("eviction", {})
    eviction_interval = eviction_config.get("interval_seconds", 3600)
    eviction_ttl_days = eviction_config.get("ttl_days", 30)
    _eviction_stop = threading.Event()
    _eviction_thread: threading.Thread | None = None  # OHM-inq1: declared so the shutdown join sees a defined name.

    def _eviction_loop():
        while not _eviction_stop.wait(eviction_interval):
            try:
                from ohm.queries import evict_expired_fragments

                result = evict_expired_fragments(store.conn, ttl_days=eviction_ttl_days)
                if result["evicted"] or result["extended"]:
                    logger.info(
                        "Fragment eviction: %d evicted, %d extended, %d promoted skipped (of %d candidates)",
                        len(result["evicted"]),
                        len(result["extended"]),
                        len(result["skipped_promoted"]),
                        result["candidate_count"],
                    )
            except Exception:
                logger.exception("Fragment eviction failed")

    if eviction_interval > 0:
        _eviction_thread = threading.Thread(target=_eviction_loop, daemon=True, name="fragment-eviction")
        _eviction_thread.start()

    # OHM-wx42: Background semantic-layer metric-actions thread.
    # Runs only when enabled in config; respects rate-limit window in
    # ohm_metric_action_log to avoid duplicate tasks.
    semantic_layer_config = config.get("semantic_layer", DEFAULT_CONFIG["semantic_layer"])
    _metric_actions_stop = threading.Event()

    def _metric_actions_loop():
        from ohm.semantic_layer import run_metrics_and_actions

        interval = float(semantic_layer_config.get("auto_actions_interval_seconds", 3600))
        rate_limit = float(semantic_layer_config.get("auto_actions_rate_limit_seconds", 86400))
        repo = config.get("repo_path", "/root/olympus/OHM")
        while not _metric_actions_stop.wait(interval):
            try:
                result = run_metrics_and_actions(
                    store.conn,
                    repo_path=repo,
                    execute=True,
                    use_ibis=False,
                    rate_limit_window_seconds=rate_limit,
                )
                created = [e for e in result.get("executed", []) if e.get("status") == "created"]
                skipped = [e for e in result.get("executed", []) if e.get("status") == "skipped"]
                errors = [e for e in result.get("executed", []) if e.get("status") == "error"]
                if created or skipped or errors:
                    logger.info(
                        "Semantic-layer auto actions: %d created, %d skipped, %d errors",
                        len(created),
                        len(skipped),
                        len(errors),
                    )
            except Exception:
                logger.exception("Semantic-layer auto actions heartbeat failed")

    _metric_actions_thread: threading.Thread | None = None
    if semantic_layer_config.get("auto_actions_enabled", False):
        metric_actions_interval = float(semantic_layer_config.get("auto_actions_interval_seconds", 3600))
        if metric_actions_interval > 0:
            _metric_actions_thread = threading.Thread(target=_metric_actions_loop, daemon=True, name="semantic-metric-actions")
            _metric_actions_thread.start()
            logger.info(
                "Semantic-layer auto actions enabled (interval=%ss, rate_limit=%ss)",
                metric_actions_interval,
                semantic_layer_config.get("auto_actions_rate_limit_seconds", 86400),
            )

    # OHM-sbtz: Background Beads->OHM task sync thread. Mirrors assigned
    # Beads issues into OHM task nodes so /tasks?assigned_to=<agent>
    # returns the right work without the operator having to POST
    # /admin/sync-beads on every change. Configurable via
    # beads_sync.{enabled, interval_seconds, startup_sync}. Default
    # enabled at 60s interval with a one-shot startup sync so the
    # first /tasks call after boot is populated.
    beads_sync_config = config.get("beads_sync", DEFAULT_CONFIG["beads_sync"])
    beads_sync_enabled = beads_sync_config.get("enabled", True)
    beads_sync_interval = float(beads_sync_config.get("interval_seconds", 60))
    beads_startup_sync = beads_sync_config.get("startup_sync", True)
    _beads_sync_stop = threading.Event()
    _beads_sync_thread: threading.Thread | None = None

    def _beads_sync_loop():
        while not _beads_sync_stop.wait(beads_sync_interval):
            _do_beads_sync(store.conn, actor="system")

    if beads_sync_enabled and beads_sync_interval > 0:
        # One-shot startup sync so the first agent query after boot
        # sees the assigned work without waiting a full interval.
        if beads_startup_sync:
            _do_beads_sync(store.conn, actor="system")
        _beads_sync_thread = threading.Thread(target=_beads_sync_loop, daemon=True, name="beads-sync")
        _beads_sync_thread.start()
        logger.info(
            "Beads sync enabled (interval=%ss, startup_sync=%s)",
            beads_sync_interval,
            beads_startup_sync,
        )

    print(f"Schema: {schema_config.name}", file=sys.stderr)
    if OhmHandler.multi_tenant:
        print("Multi-tenancy: ENABLED", file=sys.stderr)
    else:
        print("Multi-tenancy: disabled (single-tenant mode)", file=sys.stderr)
    if quack_info:
        print("Concurrent access: Quack (multi-writer)", file=sys.stderr)
    else:
        print("Concurrent access: HTTP (single-writer)", file=sys.stderr)

    # Graceful shutdown — CHECKPOINT before exit (OHM-8n9, OHM-xfqp).
    #
    # CRITICAL: server.shutdown() must be called from a thread OTHER than the
    # one running server.serve_forever() — socketserver.BaseServer.shutdown()
    # blocks on __is_shut_down.wait(), which is only set when serve_forever()
    # exits its loop. Calling shutdown() from the same thread deadlocks
    # (Python socketserver docs). Signal handlers always run on the main
    # thread, which is also running serve_forever(), so we MUST dispatch
    # shutdown() to a separate thread. The daemon thread sets __shutdown_request
    # (stopping serve_forever's accept loop on its next iteration), then waits
    # for __is_shut_down to be set. The main thread returns from this handler,
    # serve_forever sees the flag, exits the loop, sets __is_shut_down, the
    # daemon unblocks, the main thread falls through to the post-serve_forever
    # cleanup below, and the process exits cleanly. Without this hop,
    # SIGTERM/SIGINT are silently ignored (OHM-k79z — systemctl restart|stop|
    # kill all fail; PID never changes) because the signal handler never
    # returns.
    #
    # CRITICAL: this handler must also be FAST and NON-BLOCKING (OHM-inq1).
    # Earlier versions ran store.sync_heartbeat() and store.conn.execute(
    # "CHECKPOINT") synchronously here. Both touch store.conn, which a
    # background worker thread (ducklake-sync, fragment-eviction,
    # semantic-metric-actions, beads-sync) may be holding in a long DuckDB
    # query. Result: the main thread blocks inside the handler waiting for
    # the connection, serve_forever() never re-enters its selector loop,
    # __is_shut_down never gets set, the daemon-thread that called
    # server.shutdown() waits indefinitely, and the process hangs.
    # All real cleanup (worker joins, sync_heartbeat, CHECKPOINT,
    # tenant_manager.shutdown, store.close) now runs AFTER serve_forever
    # returns, with bounded join(timeout=...) waits so a stuck worker
    # thread can't hang shutdown beyond _WORKER_THREAD_JOIN_TIMEOUT seconds.
    def shutdown_handler(signum, frame):
        print("Shutting down...", file=sys.stderr)
        _sync_stop.set()
        _eviction_stop.set()
        _metric_actions_stop.set()
        _beads_sync_stop.set()
        _checkpoint_stop.set()
        # Dispatch shutdown() to a daemon thread so it does not deadlock
        # against serve_forever() running on this (main) thread (OHM-k79z).
        threading.Thread(target=server.shutdown, daemon=True, name="ohmd-shutdown").start()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    # Ignore SIGPIPE to prevent crashes on broken client connections
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    # Worker-thread join budget after serve_forever exits (OHM-inq1). Each
    # background loop body is bounded work and should respond to its stop
    # event within a few seconds; capping the wait means a stuck thread
    # can't hang shutdown indefinitely.
    _WORKER_THREAD_JOIN_TIMEOUT = 5.0

    def _join_worker(thread: threading.Thread | None, name: str) -> None:
        if thread is None:
            return
        thread.join(timeout=_WORKER_THREAD_JOIN_TIMEOUT)
        if thread.is_alive():
            logger.warning(
                "Worker thread %s did not exit within %.1fs of stop signal",
                name,
                _WORKER_THREAD_JOIN_TIMEOUT,
            )

    try:
        server.serve_forever()
    finally:
        # Post-shutdown cleanup. Runs on the main thread after serve_forever
        # returns (whether from a clean shutdown signal or from any exception).
        # Bounded timeouts ensure we always make it to store.close().
        _join_worker(_sync_thread, "ducklake-sync")
        _join_worker(_eviction_thread, "fragment-eviction")
        _join_worker(_metric_actions_thread, "semantic-metric-actions")
        _join_worker(_beads_sync_thread, "beads-sync")
        _join_worker(_checkpoint_thread, "periodic-checkpoint")
        try:
            store.sync_heartbeat()
        except Exception:
            logger.exception("Shutdown: sync_heartbeat failed")
        try:
            store.conn.execute("CHECKPOINT")
        except Exception:
            logger.exception("Shutdown: CHECKPOINT failed")
        if OhmHandler.tenant_manager is not None:
            try:
                OhmHandler.tenant_manager.shutdown()
            except Exception:
                logger.exception("Shutdown: tenant_manager.shutdown failed")
        try:
            store.close()
        except Exception:
            logger.exception("Shutdown: store.close failed")


def main(schema_config: SchemaConfig | None = None):
    """CLI entry point for ohmd.

    Args:
        schema_config: SchemaConfig to use. Defaults to DEFAULT_SCHEMA.
            Pass TOPO_SCHEMA for the topod entry point.
    """
    if schema_config is None:
        schema_config = DEFAULT_SCHEMA

    # OHM-sbtz: configure logging so logger.info/warning/exception calls
    # (beads sync, DuckLake sync, etc.) are visible in journald/stderr.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="OHM daemon — multi-agent knowledge graph server")
    parser.add_argument("--host", default=None, help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port (default: 8710)")
    parser.add_argument("--db", default=None, help="Path to DuckDB file")
    parser.add_argument("--config", default=None, help="Path to config file")
    parser.add_argument("--init-token", default=None, help="Create a token for an agent (agent_name)")
    parser.add_argument("--init-customer-token", default=None, metavar="CUSTOMER_ID", help="Generate a customer API key for CUSTOMER_ID and save hash to config")
    parser.add_argument("--no-auth", action="store_true", help="Disable authentication (dev mode)")
    parser.add_argument(
        "--require-read-auth",
        action="store_true",
        help="Require authentication for read endpoints (default: public reads)",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Domain schema name (e.g., ohm, topo, beef_herd, devsecops). Loads from bundled templates or --templates-dir. Resolved at startup; DB-stored schema takes precedence.",
    )
    parser.add_argument(
        "--templates-dir",
        default=None,
        help="Directory containing custom domain schema templates ({domain}.json files)",
    )
    parser.add_argument(
        "--quack",
        action="store_true",
        help="Enable Quack protocol for concurrent multi-writer access",
    )
    parser.add_argument(
        "--quack-uri",
        default=None,
        help="Quack server URI (default: quack:localhost)",
    )
    parser.add_argument(
        "--quack-token-env",
        default=None,
        help="Environment variable for Quack token (default: QUACK_TOKEN)",
    )
    parser.add_argument(
        "--check-migrations",
        action="store_true",
        help="Report pending schema migrations without applying them, then exit",
    )
    parser.add_argument(
        "--multi-tenant",
        action="store_true",
        help="Enable multi-tenancy mode (per-tenant isolated DuckDB instances)",
    )
    parser.add_argument(
        "--extra-schema",
        action="append",
        default=None,
        metavar="FILE",
        help="Extra schema JSON file to layer on top of --schema (repeatable). "
        "Tables and vocabulary are additively merged; name collisions raise an error.",
    )
    args = parser.parse_args()

    # Canonical config path: CLI --config wins, then OHM_CONFIG, then default.
    config_path = Path(args.config if args.config is not None else os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json")))

    config = load_config(str(config_path))

    # OHM-796: Load raw file config (before DEFAULT_CONFIG merge) so
    # _apply_ohm_meta_config can tell which keys were explicitly set
    # in the file vs inherited from defaults.
    file_config: dict[str, Any] = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                file_config = json.load(f)
        except Exception:
            file_config = {}

    # OHM-795: Resolve schema config - priority: --schema <name> -> DEFAULT_SCHEMA
    # After store creation, SchemaConfig.from_db() takes precedence (see below).
    from ohm.graph.schema import resolve_schema_by_name

    templates_dir = args.templates_dir or config.get("templates_dir")
    if args.schema:
        schema_config = resolve_schema_by_name(args.schema, templates_dir=templates_dir)
    else:
        schema_config = DEFAULT_SCHEMA

    # OHM-835: Apply any --extra-schema files (additive merge on top of base).
    if args.extra_schema:
        from ohm.graph.schema import SchemaConfig as _SC

        for extra_path in args.extra_schema:
            try:
                extra_schema = _SC.from_json_path(extra_path)
                schema_config = schema_config.extend(extra_schema)
                print(f"Extra schema applied: {extra_path} ({len(extra_schema.domain_tables)} tables)", file=sys.stderr)
            except (ValueError, FileNotFoundError) as e:
                print(f"ERROR: Failed to load extra schema '{extra_path}': {e}", file=sys.stderr)
                sys.exit(1)

    # CLI overrides
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port
    if args.db:
        config["db_path"] = args.db
    if args.no_auth or os.environ.get("OHM_NO_AUTH", "").lower() in ("1", "true", "yes"):
        config["no_auth"] = True
    if args.require_read_auth or os.environ.get("OHM_REQUIRE_READ_AUTH", "").lower() in ("1", "true", "yes"):
        config["require_read_auth"] = True

    # Quack configuration
    if args.quack or os.environ.get("OHM_QUACK", "").lower() in ("1", "true", "yes"):
        config["quack"] = True
    if args.quack_uri:
        config["quack_uri"] = args.quack_uri
    if args.quack_token_env:
        config["quack_token_env"] = args.quack_token_env

    # Multi-tenancy configuration (OHM-l31g)
    if args.multi_tenant or os.environ.get("OHM_MULTI_TENANT", "").lower() in ("1", "true", "yes"):
        config["multi_tenant"] = True

    # Handle token generation
    if args.init_token:
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        # Store hashed token in config — plaintext is never persisted
        config.setdefault("tokens", {})[args.init_token] = {
            "hash": token_hash,
            "role": "read-write",
        }
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Token for {args.init_token}: {token}")
        print(f"Config saved to {config_path}")
        print("WARNING: Store this token securely — it will not be shown again.")
        return

    # Handle customer token generation (OHM-tss4.3)
    if args.init_customer_token:
        from ohm.framework.validation import validate_customer_id

        customer_id = validate_customer_id(args.init_customer_token)
        token, token_hash = _generate_customer_token(customer_id)
        config.setdefault("customer_tokens", {})[customer_id] = {"hash": token_hash}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Customer token for {customer_id}: {token}")
        print(f"Config saved to {config_path}")
        print("WARNING: Store this token securely — it will not be shown again.")
        return

    # Migration dry-run: report pending migrations and exit without applying
    if args.check_migrations:
        from ohm.schema import MIGRATIONS
        import duckdb as _duckdb

        db_path_check = config.get("db_path", str(Path.home() / ".ohm" / "ohm.duckdb"))
        try:
            _check_conn = _duckdb.connect(db_path_check, read_only=True)
            current = _check_conn.execute("SELECT value FROM ohm_meta WHERE key = 'schema_version'").fetchone()
            current_version = current[0] if current else "0.1.0"
            _check_conn.close()
        except Exception:
            current_version = "0.1.0"

        def _vtuple(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split("."))

        current_key = _vtuple(current_version)
        pending = [(v, desc) for v, desc, _ in MIGRATIONS if current_key < _vtuple(v)]

        print(f"Current schema version: {current_version}")
        if pending:
            print(f"{len(pending)} pending migration(s):")
            for v, desc in pending:
                print(f"  {v}: {desc}")
        else:
            print("No pending migrations.")
        return

    # Initialize store
    # Set DuckLake paths as env vars for OhmStore recovery
    ducklake_config = config.get("ducklake", {})
    ducklake_path = ducklake_config.get("path", "")
    if ducklake_path:
        os.environ["OHM_DUCKLAKE_PATH"] = ducklake_path
        data_path = ducklake_config.get("data_path", "")
        if data_path:
            os.environ["OHM_DUCKLAKE_DATA"] = data_path

    # OHM-vl8o: pass schema to OhmStore so domain DDL (e.g. topo_rul_assessments) is
    # created during _init_schema(). Without this, the store uses DEFAULT_SCHEMA
    # and any domain tables are silently skipped even when the daemon was
    # started with `ohmd --schema topo`.
    store = OhmStore(db_path=config["db_path"], agent_name="ohmd", schema=schema_config)
    print(f"OHM database: {config['db_path']}", file=sys.stderr)

    # OHM-795: After store creation, check if a schema is already persisted
    # in ohm_meta. If so, it takes precedence over the provisional schema
    # used to bootstrap the store. This makes the DB the source of truth.
    #
    # OHM-835: Always prefer the persisted schema when present, and
    # additively merge in whatever this invocation additionally requests
    # (e.g. --extra-schema). The previous name-equality check silently
    # discarded extended tables on restart because .extend() keeps the
    # same domain name.
    from ohm.graph.schema import SchemaConfig as _SC
    from ohm.graph.schema import _create_domain_tables, _seed_domain_agents

    db_schema = _SC.from_db(store.conn)
    if db_schema is not None:
        schema_config = db_schema.extend(schema_config) if schema_config else db_schema
        OhmHandler.schema_config = schema_config
        print(f"Schema loaded from DB: {schema_config.name}", file=sys.stderr)
        # Ensure domain tables are created for the reloaded/extended schema
        _create_domain_tables(store.conn, schema_config)
        _seed_domain_agents(store.conn, schema_config)
    print(f"Status: {store.status()}", file=sys.stderr)

    # Attach DuckLake if configured (OHM-kdk.1)
    ducklake_config = config.get("ducklake", {})
    ducklake_path = ducklake_config.get("path", "")
    if ducklake_path:
        from ohm.db import attach_ducklake

        data_path = ducklake_config.get("data_path", "")
        attached = attach_ducklake(
            store.conn,
            catalog_path=ducklake_path,
            data_path=data_path or None,
            schema=schema_config,
        )
        if attached:
            print(f"DuckLake attached: {ducklake_path}", file=sys.stderr)
        else:
            print("DuckLake extension not available — lakehouse features disabled", file=sys.stderr)

    # OHM-fix-3: Create read-only connection AFTER DuckLake ATTACH
    # so both connections have matching configuration.
    store._ensure_read_conn()

    # Pre-warm pgmpy to avoid cold-import penalty on first inference call (OHM-a689)
    _prewarm_pgmpy_async()

    # Run server
    try:
        run_server(config, store, schema_config=schema_config, file_config=file_config)
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
    finally:
        store.close()


def topod_main():
    """CLI entry point for topod — TOPO industrial knowledge graph daemon.

    .. deprecated::
        Use ``ohmd --schema topo`` or provision a multi-tenant instance with
        domain='topo'. topod will be removed in a future release.

    Same as ohmd but defaults to the TOPO schema configuration.
    """
    import warnings

    from ohm.schema import TOPO_SCHEMA

    warnings.warn(
        "topod is deprecated. Use 'ohmd --schema topo' or provision a multi-tenant instance with domain='topo'. See docs/upgrade_multi_tenancy.md#topo-migration for migration steps. topod will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    logger.warning("topod is deprecated. Use 'ohmd --schema topo' or multi-tenant provisioning with domain='topo'.")
    main(schema_config=TOPO_SCHEMA)


if __name__ == "__main__":
    main()
