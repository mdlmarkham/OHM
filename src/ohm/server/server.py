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
from datetime import datetime, timedelta, timezone
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
    "ducklake": {
        "path": "",  # DuckLake catalog path (e.g., /var/lib/ohm/ohm_lake.ducklake)
        "data_path": "",  # Parquet data path (e.g., /var/lib/ohm/ohm_lake_data)
        "sync_interval_seconds": 60,  # How often to sync to DuckLake
    },
}

_START_TIME = time.time()
logger = logging.getLogger(__name__)

# ── Security Constants ─────────────────────────────────────

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB — reject bodies larger than this
MAX_BATCH_SIZE = 500  # Maximum nodes + edges per /batch request
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 1000  # per window per IP

# Simple in-memory rate limiter: {ip: [(timestamp, ...)]}
# Keyed by client IP — provides DDoS/abuse protection at the network level.
# Per-customer quota enforcement (keyed by customer_id) is tracked separately
# in OHM-982m once tss4.3 customer API keys are available.
_rate_limit_store: dict[str, list[float]] = {}
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


def _validate_webhook_url(url: str) -> None:
    """Reject webhook URLs that could enable SSRF attacks.

    Raises ValidationError for non-http(s) schemes and private/loopback targets.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(f"Webhook URL must use http or https scheme, got: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValidationError("Webhook URL missing host")
    try:
        addr = socket.getaddrinfo(host, None)[0][4][0]
        ip = ipaddress.ip_address(addr)
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                raise ValidationError(f"Webhook URL targets a private/loopback address ({addr}) — SSRF not allowed")
    except ValidationError:
        raise
    except Exception:
        raise ValidationError(f"Cannot resolve webhook host: {host!r}")


def _deliver_webhook(url: str, event: dict, timeout: float = 5.0) -> bool:
    """Deliver a webhook event to a callback URL.

    Uses HTTP POST with JSON body. Returns True on success, False on failure.
    Failures are logged but not raised — webhooks are fire-and-forget.
    """
    import urllib.request
    import urllib.error

    body = json.dumps(event).encode("utf-8")
    try:
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
    for _p in ("/", "/openapi.json", "/health", "/ready", "/metrics"):
        r.add("GET", _p)
    r.add("GET", "/events")
    r.add("GET", "/events/")

    # Read endpoints (GET exact)
    for _p in (
        "/stats",
        "/status",
        "/schema",
        "/layers",
        "/agents",
        "/nodes",
        "/listen",
        "/search",
        "/semantic_search",
        "/inference",
        "/intervene",
        "/ate",
        "/sensitivity",
        "/adjustment",
        "/voi",
        "/voi/tasks",
        "/suggest_causes",
        "/refute",
        "/lint",
        "/contract",
        "/duplicates",
        "/stale",
        "/admin/embeddings",
        "/admin/snapshots",
        "/graph/at",
        "/graph/changes",
        "/markov/absorbing",
        "/markov/expected_steps",
    ):
        r.add("GET", _p)

    # GET prefix routes (parameterised paths like /node/{id})
    for _p in (
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
        "/reliability/",
        "/compound_confidence/",
    ):
        r.add("GET", _p)

    # /source_reliability: alias for /reliability/{source} with ?source= param
    r.add("GET", "/source_reliability")

    # Multi-method: /observations supports both GET (list) and POST (bulk upload)
    r.add("GET", "/observations")
    r.add("POST", "/observations")

    # /deduplicate and /admin/checkpoint: POST is canonical; GET kept for compat
    r.add("GET", "/deduplicate")
    r.add("POST", "/deduplicate")
    r.add("GET", "/admin/checkpoint")
    r.add("POST", "/admin/checkpoint")

    # /decay: write-in-GET (legacy); registered as GET to avoid spurious 405
    r.add("GET", "/decay")

    # POST-only write endpoints (exact)
    for _p in (
        "/node",
        "/node/find_or_create",
        "/edge",
        "/outcome",
        "/agent/synthesis",
        "/batch",
        "/webhook",
        "/state",
        "/register",
        "/heartbeat",
        "/sync",
    ):
        r.add("POST", _p)

    # Webhook outbox routes (OHM-ufjk)
    r.add("GET", "/webhooks/dead-letter")
    r.add("GET", "/webhooks/outbox")

    # /tasks: GET (list) and POST (create)
    r.add("GET", "/tasks")
    r.add("POST", "/tasks")

    # POST prefix routes
    for _p in ("/challenge/", "/support/", "/observe/", "/webhook/"):
        r.add("POST", _p)

    # PATCH
    r.add("PATCH", "/node/")
    r.add("PATCH", "/edge/")
    r.add("PATCH", "/tasks/")

    # DELETE
    r.add("DELETE", "/node/")
    r.add("DELETE", "/edge/")

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


from ohm.server.handlers.admin import AdminHandlerMixin
from ohm.server.handlers.analysis import AnalysisHandlerMixin
from ohm.server.handlers.graph import GraphHandlerMixin
from ohm.server.handlers.infra import InfraHandlerMixin
from ohm.server.handlers.inference import InferenceHandlerMixin
from ohm.server.handlers.markov import MarkovHandlerMixin
from ohm.server.handlers.tenant import TenantHandlerMixin


class OhmHandler(AdminHandlerMixin, AnalysisHandlerMixin, GraphHandlerMixin, InfraHandlerMixin, InferenceHandlerMixin, MarkovHandlerMixin, TenantHandlerMixin, BaseHTTPRequestHandler):
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

    # ── Dispatch tables (set after class body) ────────────────
    # Maps path → method_name (string) for runtime getattr dispatch.
    _GET_EXACT: dict = {}
    _GET_PREFIXES: list = []
    _POST_EXACT: dict = {}
    _POST_PREFIXES: list = []
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
            role = self.roles.get(agent, "read-write")
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
            return self.store
        customer_id = self._customer_id
        if customer_id is None or self.tenant_manager is None:
            return self.store
        from ohm.tenant import TenantNotFoundError

        try:
            return self.tenant_manager.get_store(customer_id)
        except TenantNotFoundError:
            raise NodeNotFoundError(
                f"Tenant not found — provision this tenant before use"
            )

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
        """
        from urllib.parse import unquote

        # Reset per-request state (OHM-tss4.19)
        self._authenticated_agent = None
        self._resolved_customer_id = None

        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = unquote(auth[7:])
            for token_hash, agent_name in self.tokens.items():
                if _verify_token(token, token_hash):
                    self._authenticated_agent = agent_name
                    return agent_name
            for token_hash, customer_id in self.customer_tokens.items():
                if _verify_token(token, token_hash):
                    self._resolved_customer_id = customer_id
                    return f"customer:{customer_id}"

        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        if "token" in qs:
            token = qs["token"][0]
            for token_hash, agent_name in self.tokens.items():
                if _verify_token(token, token_hash):
                    self._authenticated_agent = agent_name
                    return agent_name
            for token_hash, customer_id in self.customer_tokens.items():
                if _verify_token(token, token_hash):
                    self._resolved_customer_id = customer_id
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
        role = self.roles.get(agent, "read-write")
        if role == "read-only":
            raise PermissionDeniedError(f"Agent '{agent}' has read-only access — writes are not permitted")
        return None

    def _require_write_auth(self) -> str:
        """Authenticate and verify write access. Returns agent name or raises."""
        agent = self._require_auth()
        self._check_write_access(agent)
        return agent

    def _json_response(self, code: int, data):
        """Send a JSON response."""
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _check_rate_limit(self) -> bool:
        """Check if the requesting IP is within rate limits. Returns True if allowed."""
        client_ip = self.client_address[0]
        now = time.time()
        with _rate_limit_lock:
            if client_ip not in _rate_limit_store:
                _rate_limit_store[client_ip] = [now]
                return True

            # Prune old entries
            window_start = now - RATE_LIMIT_WINDOW
            _rate_limit_store[client_ip] = [ts for ts in _rate_limit_store[client_ip] if ts > window_start]

            # OHM-41g: Prune stale IP keys that have no recent timestamps.
            # Without this, unique IPs accumulate forever.
            if not _rate_limit_store[client_ip]:
                del _rate_limit_store[client_ip]
                # Periodically prune other stale keys (every ~100 requests)
                if len(_rate_limit_store) > 100:
                    stale = [ip for ip, timestamps in _rate_limit_store.items() if not timestamps or timestamps[-1] < window_start]
                    for ip in stale:
                        del _rate_limit_store[ip]
                _rate_limit_store[client_ip] = [now]
                return True

            if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
                return False

            _rate_limit_store[client_ip].append(now)
            return True

    def _read_body(self):
        """Read and parse JSON request body. Enforces size limit."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        if length > MAX_BODY_SIZE:
            raise ValidationError(f"Request body too large: {length} bytes (max {MAX_BODY_SIZE})")
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
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
            self.log_message(
                "DELETE %s → %s (%.1fms)",
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
            self.log_message(
                "PATCH %s → %s (%.1fms)",
                self.path,
                code,
                elapsed,
            )

    def _do_PATCH(self):
        """Handle PATCH /node/{id} or PATCH /edge/{id} — partial update."""
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

            now = datetime.now(timezone.utc).isoformat()
            patchable = [
                "label",
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
        agent = self._authenticate()
        if agent is None:
            if self.no_auth or not self.tokens:
                agent = "ohm"
            elif self.require_read_auth:
                raise AuthenticationError("Authentication required — provide Bearer token")
            else:
                # Public-read model: unauthenticated reads allowed
                agent = "ohm"

        method_name = self._GET_EXACT.get(path)
        if method_name is None:
            for prefix, mn in self._GET_PREFIXES:
                if path.startswith(prefix):
                    method_name = mn
                    break
        if method_name:
            getattr(self, method_name)(path, qs)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    # ── Authenticated GET handlers ────────────────────────────

    def _get_stats(self, path: str, qs: dict) -> None:
        """GET /stats — graph statistics."""
        from ohm.queries import query_stats

        stats = query_stats(self.current_store.conn)
        stats["uptime"] = round(time.time() - _START_TIME, 1)
        self._json_response(200, stats)

    def _get_status(self, path: str, qs: dict) -> None:
        """GET /status — daemon status."""
        status = self.current_store.status()
        status["uptime"] = round(time.time() - _START_TIME, 1)
        status["version"] = "0.2.0"
        status["schema"] = self.schema_config.name
        status["quack"] = self.config.get("quack", False)
        status["multi_tenant"] = self.multi_tenant
        self._json_response(200, status)

    def _get_schema(self, path: str, qs: dict) -> None:
        """GET /schema — schema description."""
        schema = self.schema_config
        # Flatten edge types: collect all unique edge type names across layers
        all_edge_types: set[str] = set()
        for types in schema.layer_edge_types.values():
            all_edge_types.update(types)
        self._json_response(
            200,
            {
                "schema": schema.name,
                "node_types": sorted(schema.node_types),
                "edge_types": sorted(all_edge_types),
                "edge_types_by_layer": {k: sorted(v) for k, v in schema.layer_edge_types.items()},
                "layers": dict(schema.layer_descriptions),
            },
        )

    def _get_layers(self, path: str, qs: dict) -> None:
        """GET /layers — layer descriptions."""
        self._json_response(200, dict(self.schema_config.layer_descriptions))

    def _get_node(self, path: str, qs: dict) -> None:
        """GET /node/<id> — fetch a node."""
        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        node = self.current_store.get_node(node_id)
        if node:
            self._json_response(200, node)
        else:
            raise NodeNotFoundError(f"Node {node_id} not found")

    def _get_deep(self, path: str, qs: dict) -> None:
        """GET /deep/<id> — deep content retrieval with connected edges (OHM-7299)."""
        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        try:
            result = self.current_store.deep_content(node_id)
            # Include connected edges so agents get graph context alongside file content
            edges = self.current_store.execute(
                "SELECT * FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL ORDER BY created_at DESC",
                [node_id, node_id],
            )
            result["edges"] = edges
            result["edge_count"] = len(edges)
            self._json_response(200, result)
        except NodeNotFoundError:
            raise
        except Exception as e:
            self._json_response(500, {"error": "deep_content_failed", "message": str(e)})

    def _get_edge(self, path: str, qs: dict) -> None:
        """GET /edge/<id> — fetch an edge."""
        edge_id = path[6:]
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")
        edge = self.current_store.get_edge(edge_id)
        if edge:
            self._json_response(200, edge)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _get_neighborhood(self, path: str, qs: dict) -> None:
        """GET /neighborhood/<id> — node neighborhood."""
        node_id = path[14:]  # strip "/neighborhood/"
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        depth = int(qs.get("depth", [3])[0])
        layer = qs.get("layer", [None])[0]
        from ohm.queries import query_neighborhood

        edges = query_neighborhood(self.current_store.conn, node_id, depth=depth, layer=layer)
        # Collect unique node IDs from all edges plus the seed node
        node_ids = {node_id}
        for e in edges:
            node_ids.add(e["from_node"])
            node_ids.add(e["to_node"])
        # Fetch node details for all referenced nodes
        placeholders = ", ".join("?" * len(node_ids))
        node_rows = self.current_store.execute(
            f"SELECT id, label, type, created_by, created_at FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            list(node_ids),
        )
        self._json_response(200, {"nodes": node_rows, "edges": edges})

    def _get_path(self, path: str, qs: dict) -> None:
        """GET /path/<from>/<to> — shortest path."""
        parts = path[6:].split("/")
        if len(parts) >= 2:
            from ohm.validation import validate_identifier

            from_node = validate_identifier(parts[0], name="from_node")
            to_node = validate_identifier(parts[1], name="to_node")
            from ohm.queries import query_path

            results = query_path(self.current_store.conn, from_node, to_node)
            self._json_response(200, results)
        else:
            raise ValidationError("Path requires /path/from/to")

    def _get_impact(self, path: str, qs: dict) -> None:
        """GET /impact/<id> — impact analysis."""
        node_id = path[8:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        depth = int(qs.get("depth", [5])[0])
        from ohm.queries import query_impact

        results = query_impact(self.current_store.conn, node_id, depth=depth)
        self._json_response(200, results)

    def _get_confidence(self, path: str, qs: dict) -> None:
        """GET /confidence/<id> — confidence breakdown."""
        target_id = path[12:]
        from ohm.validation import validate_identifier

        target_id = validate_identifier(target_id, name="target_id")
        from ohm.queries import query_confidence

        # Check if target_id is a node or an edge
        is_node = self.current_store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE id = ?",
            [target_id],
        ).fetchone()
        is_edge = self.current_store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE id = ?",
            [target_id],
        ).fetchone()

        if is_node and is_node[0] > 0:
            # Node: find all challenge/support/refine edges pointing TO this node.
            # Use SELECT * so challenge_of, challenge_type, provenance, and PERT
            # percentile fields (probability_p05/p50/p95, confidence_p05/p50/p95)
            # are all included in the response.
            refs_result = self.current_store.conn.execute(
                """SELECT *
                   FROM ohm_edges
                   WHERE to_node = ?
                     AND edge_type IN ('CHALLENGED_BY', 'SUPPORTS', 'REFINES')
                     AND deleted_at IS NULL
                   ORDER BY created_at DESC""",
                [target_id],
            )
            ref_columns = [desc[0] for desc in refs_result.description]
            refs = [dict(zip(ref_columns, row)) for row in refs_result.fetchall()]
            # Add convenience aliases
            for r in refs:
                r["from"] = r.get("from_node")
                r["to"] = r.get("to_node")
                r["type"] = r.get("edge_type")

            challenges = [r for r in refs if r["edge_type"] == "CHALLENGED_BY"]
            supports = [r for r in refs if r["edge_type"] == "SUPPORTS"]
            refinements = [r for r in refs if r["edge_type"] == "REFINES"]

            self._json_response(
                200,
                {
                    "node_id": target_id,
                    "challenges": challenges,
                    "supports": supports,
                    "refinements": refinements,
                },
            )
        elif is_edge and is_edge[0] > 0:
            # Edge: use existing query_confidence
            results = query_confidence(self.current_store.conn, target_id)
            self._json_response(200, results)
        else:
            raise NodeNotFoundError(f"Neither node nor edge found with id: {target_id}")

    def _get_agent(self, path: str, qs: dict) -> None:
        """GET /agent/<name> — agent state."""
        agent_name = path[7:]
        from ohm.validation import validate_identifier

        agent_name = validate_identifier(agent_name, name="agent_name")
        state = self.current_store.get_agent_state(agent_name)
        if state:
            self._json_response(200, state)
        else:
            self._json_response(404, {"error": f"Agent {agent_name} not found"})

    def _get_agents(self, path: str, qs: dict) -> None:
        """GET /agents — list all agent states."""
        results = self.current_store.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name")
        self._json_response(200, results)

    def _get_nodes(self, path: str, qs: dict) -> None:
        """GET /nodes — list nodes with pagination and filtering."""
        node_type = qs.get("type", [None])[0]
        label = qs.get("label", [None])[0]
        label_contains = qs.get("label_contains", [None])[0]
        label_prefix = qs.get("label_prefix", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL"]
        params = []
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        if label:
            conditions.append("label ILIKE ?")
            params.append(f"%{label}%")
        if label_contains:
            conditions.append("label ILIKE ?")
            params.append(f"%{label_contains}%")
        if label_prefix:
            conditions.append("label ILIKE ?")
            params.append(f"{label_prefix}%")
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        # Also return total count for pagination
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
        count_params = params[:-2]  # Remove limit and offset
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0]["cnt"] if total_result else len(results)
        self._json_response(
            200,
            {
                "nodes": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )

    def _get_tasks(self, path: str, qs: dict) -> None:
        """GET /tasks — list task nodes with filtering."""
        task_status = qs.get("status", [None])[0]
        assigned_to = qs.get("assigned_to", [None])[0]
        priority = qs.get("priority", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL", "type = 'task'"]
        params = []
        if task_status:
            conditions.append("task_status = ?")
            params.append(task_status)
        if assigned_to:
            conditions.append("assigned_to = ?")
            params.append(assigned_to)
        if priority:
            conditions.append("priority = ?")
            params.append(priority)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END, due_date ASC NULLS LAST, created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        # Also return total count
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0]["cnt"] if total_result else len(results)
        self._json_response(
            200,
            {
                "tasks": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )

    def _get_listen(self, path: str, qs: dict) -> None:
        """GET /listen — poll change feed since last sync."""
        agent = self._authenticate()
        if agent is None:
            if self.no_auth or not self.tokens:
                agent = "ohm"
            elif self.require_read_auth:
                raise AuthenticationError("Authentication required — provide Bearer token")
            else:
                agent = "ohm"
        since = qs.get("since", [None])[0]
        agent_name = qs.get("agent", [agent or "ohm"])[0]
        enrich = qs.get("enrich", ["false"])[0].lower() == "true"
        if not since:
            state = self.current_store.get_agent_state(agent_name)
            if state and state.get("last_sync"):
                since = state["last_sync"]
                # last_sync is a TIMESTAMP column — DuckDB returns datetime, not string
                if isinstance(since, datetime):
                    since = since.isoformat()
            else:
                # Default to 24 hours ago (OHM-4oc)
                since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        from ohm.queries import query_change_feed

        results = query_change_feed(self.current_store.conn, since=since, agent_name=agent_name, enrich=enrich)
        self._json_response(200, results)

    def _get_search(self, path: str, qs: dict) -> None:
        """GET /search — text search over nodes."""
        query_text = qs.get("q", [""])[0]
        node_type = qs.get("type", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [20])[0])
        if not query_text:
            raise ValidationError("Search requires ?q=QUERY")
        conditions = ["deleted_at IS NULL", "(label ILIKE ? OR content ILIKE ?)"]
        params = [f"%{query_text}%", f"%{query_text}%"]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        # Column names hardcoded, values parameterized
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
        results = self.current_store.execute(sql, params)
        self._json_response(200, results)

    def _get_semantic_search(self, path: str, qs: dict) -> None:
        """GET /semantic_search — vector similarity search."""
        # Semantic search via VSS/HNSW index (OHM-o9f)
        query_text = qs.get("q", [""])[0]
        if not query_text:
            raise ValidationError("Semantic search requires ?q=QUERY")
        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [10])[0])
        min_confidence = qs.get("min_confidence", [None])[0]
        if min_confidence is not None:
            try:
                min_confidence = float(min_confidence)
            except ValueError:
                raise ValidationError("?min_confidence must be a number")
        try:
            from ohm.queries import semantic_search

            results = semantic_search(
                self.current_store.conn,
                query=query_text,
                limit=limit,
                node_type=node_type,
                min_confidence=min_confidence,
            )
            self._json_response(200, {"results": results, "count": len(results)})
        except ValueError as e:
            # Ollama not available
            self._json_response(
                503,
                {
                    "error": "service_unavailable",
                    "message": str(e),
                },
            )

    def _get_health_graph(self, path: str, qs: dict) -> None:
        """GET /health/graph — graph health check."""
        from ohm.queries import query_graph_health

        result = query_graph_health(self.current_store.conn)
        self._json_response(200, result)

    def _get_health_agents(self, path: str, qs: dict) -> None:
        """GET /health/agents — agent health check."""
        from ohm.methods import query_agent_health

        result = query_agent_health(self.current_store.conn)
        self._json_response(200, result)

    def _get_health_sync(self, path: str, qs: dict) -> None:
        """GET /health/sync — DuckLake sync health check (OHM-qiio)."""
        store = self.current_store
        if not hasattr(store, "check_ducklake_health"):
            self._json_response(503, {"healthy": False, "errors": ["DuckLake health check not available"]})
            return
        result = store.check_ducklake_health()
        status = 200 if result.get("healthy") else 503
        self._json_response(status, result)

    def _get_contradictions(self, path: str, qs: dict) -> None:
        """GET /contradictions — detect contradictions."""
        from ohm.methods import detect_contradictions

        conf_thresh = float(qs.get("confidence", [0.5])[0])
        result = detect_contradictions(self.current_store.conn, confidence_threshold=conf_thresh)
        self._json_response(200, result)

    def _get_anomalies(self, path: str, qs: dict) -> None:
        """GET /anomalies — detect anomalies."""
        from ohm.methods import detect_anomalies

        sigma = float(qs.get("sigma", [2.0])[0])
        layer = qs.get("layer", [None])[0]
        limit = int(qs.get("limit", [50])[0])
        result = detect_anomalies(self.current_store.conn, sigma_threshold=sigma, layer=layer, limit=limit)
        self._json_response(200, result)

    def _get_aggregate(self, path: str, qs: dict) -> None:
        """GET /aggregate/<id> — aggregate observations."""
        node_id = path[11:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        method = qs.get("method", ["weighted"])[0]
        from ohm.methods import aggregate_observations

        result = aggregate_observations(self.current_store.conn, node_id, method=method)
        self._json_response(200, result)

    def _get_provenance(self, path: str, qs: dict) -> None:
        """GET /provenance/<id> — provenance trace."""
        node_id = path[12:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        max_depth = int(qs.get("depth", [10])[0])
        from ohm.queries import query_provenance

        result = query_provenance(self.current_store.conn, node_id, max_depth=max_depth)
        self._json_response(200, result)

    def _get_stale(self, path: str, qs: dict) -> None:
        """GET /stale — list stale edges."""
        from ohm.queries import query_stale_edges

        threshold = float(qs.get("threshold", [0.1])[0])
        result = query_stale_edges(self.current_store.conn, stale_threshold=threshold)
        self._json_response(200, result)

    def _get_decay(self, path: str, qs: dict) -> None:
        """GET /decay — apply confidence decay."""
        self._require_write_auth()
        from ohm.queries import apply_confidence_decay

        threshold = float(qs.get("threshold", [0.1])[0])
        layer = qs.get("layer", [None])[0]
        dry_run = qs.get("dry_run", ["false"])[0].lower() == "true"
        result = apply_confidence_decay(
            self.current_store.conn,
            stale_threshold=threshold,
            layer=layer,
            dry_run=dry_run,
        )
        self._json_response(200, result)

    def _get_monte_carlo(self, path: str, qs: dict) -> None:
        """GET /monte-carlo/<id> — Monte Carlo impact simulation."""
        node_id = path[13:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        from ohm.methods import monte_carlo_impact

        sims = int(qs.get("simulations", [1000])[0])
        depth = int(qs.get("depth", [3])[0])
        default_prob = float(qs.get("default_probability", [0.5])[0])
        seed_val = qs.get("seed", [None])[0]
        seed = int(seed_val) if seed_val is not None else None
        result = monte_carlo_impact(
            self.current_store.conn,
            node_id,
            simulations=sims,
            depth=depth,
            default_probability=default_prob,
            seed=seed,
        )
        self._json_response(200, result)

    def _get_duplicates(self, path: str, qs: dict) -> None:
        """GET /duplicates — detect near-duplicate nodes."""
        from ohm.methods import detect_near_duplicates

        threshold = float(qs.get("similarity", [0.8])[0])
        result = detect_near_duplicates(self.current_store.conn, similarity_threshold=threshold)
        self._json_response(200, result)

    def _get_calibration(self, path: str, qs: dict) -> None:
        """GET /calibration/<agent> — confidence calibration."""
        agent_name = path[13:]
        from ohm.validation import validate_identifier

        agent_name = validate_identifier(agent_name, name="agent_name")
        from ohm.methods import compute_confidence_calibration

        result = compute_confidence_calibration(self.current_store.conn, agent_name)
        self._json_response(200, result)

    def _get_orphans(self, path: str, qs: dict) -> None:
        """GET /orphans — find disconnected nodes."""
        # Find nodes with zero edges — completely disconnected from the graph
        from ohm.methods import find_orphans

        node_type = qs.get("type", [None])[0]
        exclude_system = qs.get("exclude_system", ["true"])[0].lower() == "true"
        limit = int(qs.get("limit", [50])[0])
        result = find_orphans(self.current_store.conn, node_type=node_type, exclude_system=exclude_system, limit=limit)
        self._json_response(200, result)

    def _post_orphans_purge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /orphans/purge — soft-delete orphan nodes (admin-only).

        Body (all optional):
          type: node type filter (e.g. "concept")
          older_than_days: only purge orphans older than N days
          exclude_system: bool, default true — skip agent/skill/value/goal nodes
          dry_run: bool, default false — list candidates without deleting
        """
        from ohm.methods import purge_orphans

        node_type = body.get("type") if body else None
        older_than_days = body.get("older_than_days") if body else None
        if older_than_days is not None:
            older_than_days = int(older_than_days)
        exclude_system = bool(body.get("exclude_system", True)) if body else True
        dry_run = bool(body.get("dry_run", False)) if body else False

        result = purge_orphans(
            self.current_store.conn,
            node_type=node_type,
            older_than_days=older_than_days,
            exclude_system=exclude_system,
            dry_run=dry_run,
        )
        self._json_response(200, result)

    def _get_hubs(self, path: str, qs: dict) -> None:
        """GET /hubs — find most-connected nodes."""
        # Find most-connected nodes — anchors of the graph
        from ohm.methods import find_hubs

        node_type = qs.get("type", [None])[0]
        min_connections = int(qs.get("min_connections", [3])[0])
        limit = int(qs.get("limit", [20])[0])
        result = find_hubs(self.current_store.conn, node_type=node_type, min_connections=min_connections, limit=limit)
        self._json_response(200, result)

    def _get_dead_ends(self, path: str, qs: dict) -> None:
        """GET /dead_ends — find sink nodes."""
        # Find nodes with only incoming edges — sinks that don't lead anywhere
        from ohm.methods import find_dead_ends

        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [50])[0])
        result = find_dead_ends(self.current_store.conn, node_type=node_type, limit=limit)
        self._json_response(200, result)

    def _get_suggest(self, path: str, qs: dict) -> None:
        """GET /suggest — suggest connections."""
        # Suggest connections between nodes that share context but aren't linked
        from ohm.methods import suggest_connections

        method = qs.get("method", ["shared_provenance"])[0]
        min_shared = int(qs.get("min_shared", [2])[0])
        limit = int(qs.get("limit", [20])[0])
        result = suggest_connections(self.current_store.conn, method=method, min_shared=min_shared, limit=limit)
        self._json_response(200, result)

    def _get_graph_stats(self, path: str, qs: dict) -> None:
        """GET /graph/stats — extended graph statistics."""
        # Extended graph statistics (orphans, hubs, density, etc.)
        from ohm.methods import graph_stats

        result = graph_stats(self.current_store.conn)
        self._json_response(200, result)

    def _get_lint(self, path: str, qs: dict) -> None:
        """GET /lint — lint graph against contract."""
        # Contract layer linting: validate graph against naming conventions and required fields
        from ohm.contract import ContractConfig, lint_graph

        node_type_filter = qs.get("node_types", [None])[0]
        node_types = node_type_filter.split(",") if node_type_filter else None
        limit = int(qs.get("limit", ["1000"])[0])
        contract = ContractConfig()
        result = lint_graph(self.current_store.conn, contract, limit=limit, node_types=node_types)
        self._json_response(200, result)

    def _get_contract(self, path: str, qs: dict) -> None:
        """GET /contract — return current contract configuration."""
        from ohm.contract import ContractConfig

        contract = ContractConfig()
        self._json_response(200, contract.to_dict())

    def _get_inference(self, path: str, qs: dict) -> None:
        """GET /inference — Bayesian inference."""
        # Bayesian inference: compute posterior probabilities given evidence
        # Uses pgmpy Variable Elimination (optional dependency)
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        # Parse evidence from query params: ?evidence=node1:0,node2:1
        evidence_str = qs.get("evidence", [""])[0]
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        evidence = {}
        if evidence_str:
            for pair in evidence_str.split(","):
                if ":" in pair:
                    node_id, state = pair.split(":", 1)
                    evidence[validate_identifier(node_id.strip(), name="evidence_node")] = int(state.strip())
        # Parse optional layers filter: ?layers=L3,L4
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import bayesian_inference

        result = bayesian_inference(self.current_store.conn, target, evidence, edge_types=None, layers=layers, leak_probability=leak_probability)
        self._json_response(200, result)

    def _get_intervene(self, path: str, qs: dict) -> None:
        """GET /intervene — causal intervention (do-operator)."""
        # Causal intervention using Pearl's do-operator (graph surgery)
        # Differs from /inference: severs incoming edges to target, sets value externally
        # This isolates direct causal effect by removing confounder influence
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        # Parse intervention state: ?state=0 (force bad) or ?state=1 (force good)
        state_str = qs.get("state", [None])[0]
        if state_str is None:
            self._json_response(400, {"error": "missing_parameter", "message": "?state=0 (bad) or ?state=1 (good) required"})
            return
        try:
            intervention_state = int(state_str)
        except ValueError:
            self._json_response(400, {"error": "invalid_parameter", "message": "state must be 0 or 1"})
            return
        # Parse optional query nodes: ?query=node1,node2
        query_str = qs.get("query", [""])[0]
        query_nodes = None
        if query_str:
            query_nodes = [validate_identifier(q.strip(), name="query_node") for q in query_str.split(",") if q.strip()]
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        # Parse optional layers filter: ?layers=L3,L4
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        # Parse optional preferred_edges: ?preferred_edges=A:B,C:D (colon-separated pairs)
        pe_str = qs.get("preferred_edges", [""])[0]
        preferred_edges: set[tuple[str, str]] | None = None
        if pe_str:
            preferred_edges = set()
            for pair in pe_str.split(","):
                parts = pair.strip().split(":")
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    preferred_edges.add((parts[0].strip(), parts[1].strip()))
        from ohm.bayesian import causal_intervention

        result = causal_intervention(
            self.current_store.conn,
            target,
            intervention_state,
            query_nodes=query_nodes,
            layers=layers,
            leak_probability=leak_probability,
            preferred_edges=preferred_edges,
        )
        self._json_response(200, result)

    def _get_ate(self, path: str, qs: dict) -> None:
        """GET /ate — average treatment effect."""
        # Average Treatment Effect: model-based ATE from noisy-OR CPDs
        # ATE = P(effect=bad|do(cause=bad)) - P(effect=bad|do(cause=good))
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        # Parse optional layers filter: ?layers=L3,L4
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import compute_ate

        result = compute_ate(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
        self._json_response(200, result)

    def _get_sensitivity(self, path: str, qs: dict) -> None:
        """GET /sensitivity — sensitivity analysis (E-value)."""
        # Sensitivity analysis: E-value for causal robustness
        # "How much unmeasured confounding would overturn this conclusion?"
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        # Parse optional layers filter: ?layers=L3,L4
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import compute_sensitivity

        result = compute_sensitivity(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
        self._json_response(200, result)

    def _get_adjustment(self, path: str, qs: dict) -> None:
        """GET /adjustment — find adjustment sets."""
        # Find valid backdoor/frontdoor adjustment sets for causal identification
        # Uses pgmpy's CausalInference for formal identification
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        # Parse optional layers filter: ?layers=L3,L4
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import find_adjustment_sets

        result = find_adjustment_sets(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
        self._json_response(200, result)

    def _get_voi(self, path: str, qs: dict) -> None:
        """GET /voi — value of information ranking."""
        # Value of Information: rank nodes by research priority
        # VoI = uncertainty × sensitivity_to_decision
        # ?decision=node1,node2 to specify decision nodes (auto-detects if omitted)
        # ?top=10 to limit results
        # ?layers=L3,L4 to scope by layer
        # ?edge_types=CAUSES,INFLUENCES,ENABLES,DEPENDS_ON to filter edge types
        decision_str = qs.get("decision", [None])[0]
        decision_nodes = [d.strip() for d in decision_str.split(",") if d.strip()] if decision_str else None
        top = int(qs.get("top", ["10"])[0])
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        root_prior = float(qs.get("root_prior", ["0.3"])[0])
        # Parse optional layers filter: ?layers=L3,L4
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        # Parse optional edge_types filter: ?edge_types=CAUSES,DEPENDS_ON
        edge_types_str = qs.get("edge_types", [""])[0]
        edge_types = [e.strip() for e in edge_types_str.split(",") if e.strip()] if edge_types_str else None
        timeout = float(qs.get("timeout", ["0"])[0]) or None
        min_observations = int(qs.get("min_observations", ["0"])[0])
        from ohm.bayesian import compute_voi

        result = compute_voi(
            self.current_store.conn,
            decision_nodes=decision_nodes,
            edge_types=edge_types,
            layers=layers,
            top=top,
            leak_probability=leak_probability,
            root_prior=root_prior,
            timeout=timeout,
            min_observations=min_observations,
        )
        self._json_response(200, result)


    def _get_voi_tasks(self, path: str, qs: dict) -> None:
        """GET /voi/tasks — VoI task assignments."""
        # OHM-8w2: Value of Information task assignment for agent routing.
        # Generates research tasks from VoI rankings, matched to agent expertise.
        # ?agent=metis to filter by agent
        # ?decision=node1,node2 to specify decision nodes
        # ?top=5 to limit results
        # ?layers=L3,L4 to scope by layer
        # ?leak=0.15&root_prior=0.3 for Bayesian parameters
        agent = qs.get("agent", [None])[0]
        decision_str = qs.get("decision", [None])[0]
        decision_nodes = [d.strip() for d in decision_str.split(",") if d.strip()] if decision_str else None
        top = int(qs.get("top", ["5"])[0])
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        root_prior = float(qs.get("root_prior", ["0.3"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import generate_voi_tasks

        result = generate_voi_tasks(
            self.current_store.conn,
            agent=agent,
            decision_nodes=decision_nodes,
            layers=layers,
            top=top,
            leak_probability=leak_probability,
            root_prior=root_prior,
        )
        self._json_response(200, result)

    def _get_suggest_causes(self, path: str, qs: dict) -> None:
        """GET /suggest_causes — suggest candidate causal edges."""
        # Suggest candidate CAUSES edges from existing non-causal relationships
        # Identifies DEPENDS_ON/APPLIES_TO/REFINES/INFLUENCES edges that might be causal
        min_confidence = float(qs.get("min_confidence", ["0.5"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import suggest_causes

        result = suggest_causes(self.current_store.conn, min_confidence=min_confidence, layers=layers)
        self._json_response(200, result)

    def _get_deduplicate(self, path: str, qs: dict) -> None:
        """GET /deduplicate — remove duplicate edges."""
        # Remove duplicate edges (same from→to, type, layer), keeping most recent
        self._require_write_auth()
        layer = qs.get("layer", [None])[0]
        if layer:
            from ohm.validation import validate_layer

            try:
                validate_layer(layer)
            except ValueError as e:
                raise ValidationError(str(e))
        removed = self.current_store.deduplicate_edges(layer=layer)
        self._json_response(200, {"removed": removed, "layer": layer})

    def _get_refute(self, path: str, qs: dict) -> None:
        """GET /refute — causal refutation tests."""
        # Causal refutation: test robustness of causal conclusions
        # Uses DoWhy refutation methods (requires dowhy package)
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        n_samples = int(qs.get("n_samples", ["1000"])[0])
        seed = int(qs.get("seed", ["42"])[0])
        methods_str = qs.get("methods", [None])[0]
        refutation_methods = methods_str.split(",") if methods_str else None
        from ohm.causal_refutation import refute_causal_effect

        result = refute_causal_effect(
            self.current_store.conn,
            cause,
            effect,
            n_samples=n_samples,
            seed=seed,
            refutation_methods=refutation_methods,
        )
        self._json_response(200, result)

    def _get_admin_checkpoint(self, path: str, qs: dict) -> None:
        """GET /admin/checkpoint — force DuckDB CHECKPOINT."""
        # Force DuckDB CHECKPOINT to flush WAL to main DB file
        self._require_write_auth()
        try:
            self.current_store.conn.execute("CHECKPOINT")
            self._json_response(200, {"status": "ok", "message": "WAL flushed to main database"})
        except Exception as e:
            self._json_response(500, {"error": "checkpoint_failed", "message": str(e)})

    def _get_admin_embeddings(self, path: str, qs: dict) -> None:
        """GET /admin/embeddings — batch generate embeddings."""
        # Batch generate embeddings for nodes missing them (OHM-emb)
        # Processes in small batches with delays to avoid OOM/timeout crashes
        try:
            from ohm.queries import update_node_embedding

            # Parse optional batch_size and delay_ms query params
            batch_size = 5  # Process N nodes per request (small to avoid OOM)
            delay_ms = 200  # Pause between each embedding (ms) to reduce memory pressure
            if qs.get("batch_size"):
                try:
                    batch_size = int(qs["batch_size"][0])
                    if batch_size < 1:
                        batch_size = 1
                    elif batch_size > 50:
                        batch_size = 50
                except ValueError:
                    pass
            if qs.get("delay_ms"):
                try:
                    delay_ms = int(qs["delay_ms"][0])
                    if delay_ms < 0:
                        delay_ms = 0
                    elif delay_ms > 5000:
                        delay_ms = 5000
                except ValueError:
                    pass

            # Find all nodes without embeddings
            rows = self.current_store.execute("SELECT id, label FROM ohm_nodes WHERE embedding IS NULL AND deleted_at IS NULL")
            if not rows:
                self._json_response(
                    200,
                    {
                        "status": "ok",
                        "updated": 0,
                        "failed": 0,
                        "total": 0,
                        "message": "All nodes already have embeddings",
                    },
                )
                return

            updated = 0
            failed = 0
            processed = 0
            for row in rows:
                # Stop after batch_size nodes — client can re-call for more
                if processed >= batch_size:
                    break
                try:
                    if update_node_embedding(self.current_store.conn, row["id"]):
                        updated += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                processed += 1
                # Small delay between embeddings to reduce memory pressure
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)

            total_missing = len(rows)
            remaining = total_missing - processed
            self._json_response(
                200,
                {
                    "status": "ok" if remaining == 0 else "partial",
                    "updated": updated,
                    "failed": failed,
                    "processed": processed,
                    "total": total_missing,
                    "remaining": remaining,
                    "message": f"Generated {updated} embeddings ({failed} failed). {remaining} remaining — re-call to continue.",
                },
            )
        except Exception as e:
            self._json_response(500, {"error": "embedding_backfill_failed", "message": str(e)})

    def _get_admin_snapshots(self, path: str, qs: dict) -> None:
        """GET /admin/snapshots — list DuckLake snapshots."""
        # DuckLake time-travel: list available snapshots (OHM-kdk.3)
        snapshots = self.current_store.list_snapshots()
        self._json_response(200, {"snapshots": snapshots, "count": len(snapshots)})

    def _get_graph_at(self, path: str, qs: dict) -> None:
        """GET /graph/at — query graph at snapshot version."""
        # DuckLake time-travel: query graph at specific snapshot version (OHM-kdk.3)
        version = qs.get("version", [None])[0]
        if not version:
            raise ValidationError("?version=N is required for /graph/at")
        try:
            version_int = int(version)
        except ValueError:
            raise ValidationError("?version must be an integer snapshot ID")
        result = self.current_store.graph_at_version(version_int)
        self._json_response(200, result)

    def _get_graph_changes(self, path: str, qs: dict) -> None:
        """GET /graph/changes — changes between snapshot versions."""
        # DuckLake time-travel: changes between two snapshot versions (OHM-kdk.3)
        from_version = qs.get("from_version", [None])[0]
        to_version = qs.get("to_version", [None])[0]
        if not from_version or not to_version:
            raise ValidationError("?from_version=M&to_version=N are required for /graph/changes")
        try:
            from_int = int(from_version)
            to_int = int(to_version)
        except ValueError:
            raise ValidationError("?from_version and ?to_version must be integers")
        result = self.current_store.graph_changes(from_int, to_int)
        self._json_response(200, result)

    def _get_reliability(self, path: str, qs: dict) -> None:
        """GET /reliability/<agent> — source reliability metrics."""
        source_agent = path[13:]  # strip /reliability/
        from ohm.validation import validate_identifier

        source_agent = validate_identifier(source_agent, name="source_agent")
        from ohm.queries import query_source_reliability

        result = query_source_reliability(self.current_store.conn, source_agent)
        self._json_response(200, result)

    def _get_source_reliability(self, path: str, qs: dict) -> None:
        """GET /source_reliability — alias for /reliability/{source} accepting ?source= param (OHM-7310)."""
        source_agent = qs.get("source", [None])[0]
        if not source_agent:
            raise ValidationError("?source=<agent_name> is required")
        from ohm.validation import validate_identifier

        source_agent = validate_identifier(source_agent, name="source_agent")
        from ohm.queries import query_source_reliability

        result = query_source_reliability(self.current_store.conn, source_agent)
        self._json_response(200, result)

    def _get_compound_confidence(self, path: str, qs: dict) -> None:
        """GET /compound_confidence/<node_id> — compound confidence from node observations (OHM-7311)."""
        node_id = path[21:]  # strip /compound_confidence/ (21 chars)
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        node = self.current_store.get_node(node_id)
        if not node:
            raise NodeNotFoundError(f"Node not found: {node_id}")
        correlation = float(qs.get("correlation", ["0.0"])[0])
        observations = self.current_store.execute(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC",
            [node_id],
        )
        from ohm.methods import compound_confidence

        # Derive confidence from sigma (lower sigma = higher confidence) or default 1.0
        def _obs_confidence(obs: dict) -> float:
            sigma = obs.get("sigma")
            if sigma is not None and sigma > 0:
                return max(0.0, min(1.0, 1.0 / (1.0 + float(sigma))))
            return 1.0

        obs_with_confidence = [{"confidence": _obs_confidence(obs), "source": obs.get("created_by")} for obs in observations]
        result = compound_confidence(obs_with_confidence, correlation=correlation)
        result["node_id"] = node_id
        result["observations"] = len(observations)
        self._json_response(200, result)

    def _get_observations(self, path: str, qs: dict) -> None:
        """GET /observations — list observations with filtering."""
        obs_type = qs.get("type", [None])[0]
        source = qs.get("source", [None])[0]
        node_id = qs.get("node_id", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL"]
        params = []
        if obs_type:
            conditions.append("type = ?")
            params.append(obs_type)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if node_id:
            conditions.append("node_id = ?")
            params.append(node_id)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_observations WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        # Count query
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_observations WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0]["cnt"] if total_result else len(results)
        self._json_response(200, {"observations": results, "total": total, "limit": limit, "offset": offset})

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
        qs = parse_qs(parsed.query)
        body = self._read_body()
        body = self._validate_body(path, body)

        method_name = self._POST_EXACT.get(path)
        if method_name is None:
            for prefix, mn in self._POST_PREFIXES:
                if path.startswith(prefix):
                    method_name = mn
                    break
        if method_name:
            # Acquire the per-tenant write lock for multi-tenant customer requests.
            # DuckDB is single-writer; serializing at this layer prevents concurrent
            # write conflicts within the same tenant's isolated DuckDB file.
            # Agent token requests (customer_id=None) and single-tenant mode skip this.
            customer_id = self._customer_id
            if customer_id and self.tenant_manager:
                from ohm.tenant import TenantNotFoundError

                try:
                    write_lock = self.tenant_manager.get_write_lock(customer_id)
                except TenantNotFoundError:
                    raise NodeNotFoundError(
                        f"Tenant not found — provision this tenant before use"
                    )
                with write_lock:
                    getattr(self, method_name)(path, qs, body, agent)
            else:
                getattr(self, method_name)(path, qs, body, agent)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def _post_node(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node — create or upsert a node."""
        # Support ?create_only=true to reject updates (upsert is the default — OHM-y2i.20)
        create_only = qs.get("create_only", ["false"])[0].lower() in ("true", "1", "yes")
        if create_only:
            existing = self.current_store.conn.execute(
                "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [body["id"]],
            ).fetchone()
            if existing:
                self._json_response(
                    409,
                    {
                        "error": "conflict",
                        "message": f"Node {body['id']} already exists. Use ?create_only=false for upsert.",
                    },
                )
                return

        result = self.current_store.write_node(
            id=body["id"],
            label=body["label"],
            type=body.get("type", "concept"),
            content=body.get("content"),
            confidence=body.get("confidence", 1.0),
            visibility=body.get("visibility", "team"),
            provenance=body.get("provenance"),
            tags=body.get("tags"),
            metadata=body.get("metadata"),
            priority=body.get("priority"),
            url=body.get("url"),
            task_status=body.get("task_status"),
            assigned_to=body.get("assigned_to"),
            due_date=body.get("due_date"),
            utility_scale=body.get("utility_scale"),
            current_best_action=body.get("current_best_action"),
            action_alternatives=body.get("action_alternatives"),
            utility_usd_per_day=body.get("utility_usd_per_day"),
            utility_currency=body.get("utility_currency"),
            agent_name=agent,
        )
        event_type = "node.created" if result.get("created") else "node.updated"
        _trigger_webhooks(
            {
                "type": event_type,
                "agent": agent,
                "node": result,
            },
            customer_id=self._customer_id,
        )
        if result.get("created", True):
            self._json_response(201, result)
        else:
            self._json_response(200, result)

    def _post_node_find_or_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /node/find_or_create — find existing node by label+type, or create new one."""
        from ohm.queries import find_or_create_node

        node = find_or_create_node(
            self.current_store.conn,
            label=body["label"],
            node_type=body.get("type", "concept"),
            content=body.get("content"),
            created_by=agent,
            visibility=body.get("visibility", "team"),
            provenance=body.get("provenance"),
            confidence=body.get("confidence", 1.0),
            priority=body.get("priority"),
            url=body.get("url"),
        )
        is_new = node.pop("created", False)
        self._json_response(201 if is_new else 200, node)

    def _post_edge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /edge — create an edge."""
        result = self.current_store.write_edge(
            from_node=body["from"],
            to_node=body["to"],
            edge_type=body["type"],
            layer=body.get("layer", "L3"),
            confidence=body.get("confidence"),
            condition=body.get("condition"),
            provenance=body.get("provenance"),
            challenge_of=body.get("challenge_of"),
            challenge_type=body.get("challenge_type"),
            urgency=body.get("urgency"),
            probability=body.get("probability"),
            probability_p05=body.get("probability_p05"),
            probability_p50=body.get("probability_p50"),
            probability_p95=body.get("probability_p95"),
            confidence_p05=body.get("confidence_p05"),
            confidence_p50=body.get("confidence_p50"),
            confidence_p95=body.get("confidence_p95"),
            agent_name=agent,
        )
        _trigger_webhooks(
            {
                "type": "edge.created",
                "agent": agent,
                "edge": result,
            },
            customer_id=self._customer_id,
        )
        self._json_response(201, result)

    def _post_challenge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /challenge/{id} — challenge an existing edge."""
        edge_id = path[11:]
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.5)
        challenge_type = body.get("challenge_type", "CHALLENGED_BY")
        result = self.current_store.challenge_edge(edge_id, reason, confidence, challenge_type, agent_name=agent)
        if result:
            _trigger_webhooks(
                {
                    "type": "edge.challenged",
                    "agent": agent,
                    "edge": result,
                    "challenge_type": challenge_type,
                },
                customer_id=self._customer_id,
            )
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _post_support(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /support/{id} — support an existing edge."""
        edge_id = path[9:]
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.8)
        result = self.current_store.challenge_edge(edge_id, reason, confidence, "SUPPORTS", agent_name=agent)
        if result:
            _trigger_webhooks(
                {
                    "type": "edge.supported",
                    "agent": agent,
                    "edge": result,
                },
                customer_id=self._customer_id,
            )
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _post_observe(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /observe/{id} — record an observation on a node."""
        node_id = path[9:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        # Validate node exists before writing observation (OHM-7302)
        if not self.current_store.get_node(node_id):
            raise NodeNotFoundError(f"Node not found: {node_id}")
        # Validate observation type against schema (OHM-jt98)
        obs_type = body.get("type", "measurement")
        if obs_type not in self.schema_config.observation_types:
            raise ValidationError(f"Invalid observation type '{obs_type}' — must be one of: {', '.join(sorted(self.schema_config.observation_types))}")
        result = self.current_store.write_observation(
            node_id=node_id,
            type=obs_type,
            value=body.get("value"),
            baseline=body.get("baseline"),
            sigma=body.get("sigma"),
            source=body.get("source"),
            notes=body.get("notes"),
            source_name=body.get("source_name"),
            source_url=body.get("source_url"),
            agent_name=agent,
        )
        _trigger_webhooks(
            {
                "type": "observation.created",
                "agent": agent,
                "observation": result,
            },
            customer_id=self._customer_id,
        )
        self._json_response(201, result)

    def _post_observations(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /observations — bulk observation upload (OHM-0lf)."""
        # Accepts an array of observation objects in the "observations" field.
        # Each observation: {node_id, value, sigma, obs_type, source}
        obs_list = body.get("observations", [])
        if not isinstance(obs_list, list):
            raise ValidationError("'observations' must be an array")
        if len(obs_list) > 1000:
            raise ValidationError(f"Too many observations: {len(obs_list)} (max 1000)")

        results = []
        errors = []
        for i, obs in enumerate(obs_list):
            node_id = obs.get("node_id")
            if not node_id:
                errors.append({"index": i, "error": "missing node_id"})
                continue
            from ohm.validation import validate_identifier

            try:
                node_id = validate_identifier(node_id, name="node_id")
            except ValueError as e:
                errors.append({"index": i, "error": str(e)})
                continue
            try:
                obs_type = obs.get("obs_type", obs.get("type", "measurement"))
                if obs_type not in self.schema_config.observation_types:
                    errors.append({"index": i, "error": f"Invalid observation type '{obs_type}' — must be one of: {', '.join(sorted(self.schema_config.observation_types))}"})
                    continue
                result = self.current_store.write_observation(
                    node_id=node_id,
                    type=obs_type,
                    value=obs.get("value"),
                    baseline=obs.get("baseline"),
                    sigma=obs.get("sigma"),
                    source=obs.get("source"),
                    notes=obs.get("notes"),
                    source_name=obs.get("source_name"),
                    source_url=obs.get("source_url"),
                    agent_name=agent,
                )
                results.append(result)
            except Exception as e:
                errors.append({"index": i, "node_id": node_id, "error": str(e)})

        self._json_response(
            201,
            {
                "created": len(results),
                "errors": errors,
                "observations": results,
            },
        )

    def _post_outcome(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /outcome — record whether a source agent's claim was correct."""
        source_agent = body.get("source_agent")
        claim_node = body.get("claim_node")
        outcome = body.get("outcome")
        notes = body.get("notes")
        if not source_agent or not claim_node or outcome is None:
            raise ValidationError("outcome requires source_agent, claim_node, and outcome fields")
        from ohm.queries import query_record_outcome

        result = query_record_outcome(
            self.current_store.conn,
            source_agent=source_agent,
            claim_node=claim_node,
            outcome=bool(outcome),
            recorded_by=agent,
            notes=notes,
        )
        self._json_response(201, result)

    def _post_synthesis(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /agent/synthesis — one-call L3 writing: concept node + edges + observation."""
        label = body.get("label")
        content = body.get("content")
        cluster_ids = body.get("cluster_ids", [])
        edge_type = body.get("edge_type", "SUPPORTS")
        confidence = body.get("confidence", 0.8)
        sigma = body.get("sigma", 0.1)
        provenance = body.get("provenance")
        tags = body.get("tags")

        if not label or not content or not cluster_ids:
            raise ValidationError("agent/synthesis requires label, content, and cluster_ids")

        from ohm.graph.schema import generate_node_id
        from ohm.validation import validate_identifier
        import json as _json

        # Create synthesis concept node
        node_id = generate_node_id(label)
        node_result = self.current_store.write_node(
            id=node_id,
            label=label,
            type="concept",
            content=content,
            confidence=confidence,
            agent_name=agent,
            provenance=provenance or f"{agent}_synthesis",
        )
        node_id = node_result["id"] if isinstance(node_result, dict) else node_id

        # Add tags if provided
        if tags:
            self.current_store.conn.execute(
                "UPDATE ohm_nodes SET tags = ? WHERE id = ?",
                [_json.dumps(tags), node_id],
            )

        # Create L3 edges to each cluster node
        edges_created = 0
        for cid in cluster_ids:
            try:
                safe_cid = validate_identifier(cid, name="cluster_id")
            except ValueError:
                continue
            try:
                self.current_store.write_edge(
                    from_node=node_id,
                    to_node=safe_cid,
                    edge_type=edge_type,
                    layer="L3",
                    confidence=confidence,
                    agent_name=agent,
                )
                edges_created += 1
            except Exception:
                continue

        # Record observation on the synthesis node
        from ohm.queries import create_observation

        obs_result = create_observation(
            self.current_store.conn,
            node_id=node_id,
            obs_type="pattern",
            value=confidence,
            sigma=sigma,
            source="synthesis",
            notes=content,
            created_by=agent,
        )

        self._json_response(
            201,
            {
                "node": node_result if isinstance(node_result, dict) else {"id": node_id, "label": label},
                "edges_created": edges_created,
                "observation": obs_result,
            },
        )

    def _post_batch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /batch — batch node and edge creation (all-or-nothing transaction)."""
        nodes = body.get("nodes", [])
        edges = body.get("edges", [])
        errors = []
        nodes_created = 0
        edges_created = 0

        if len(nodes) + len(edges) > MAX_BATCH_SIZE:
            raise ValidationError(f"Batch too large: {len(nodes)} nodes + {len(edges)} edges = {len(nodes) + len(edges)} items exceeds limit of {MAX_BATCH_SIZE}")

        # Validate all inputs first
        for i, node in enumerate(nodes):
            if "id" not in node or "label" not in node:
                errors.append({"index": i, "type": "node", "error": "Missing required field: id and label"})
        for i, edge in enumerate(edges):
            if "from" not in edge or "to" not in edge or "type" not in edge:
                errors.append({"index": i, "type": "edge", "error": "Missing required field: from, to, type"})

        if errors:
            raise ValidationError(f"Batch validation failed: {json.dumps(errors)}")

        # All-or-nothing: execute in a single transaction
        try:
            self.current_store.conn.execute("BEGIN TRANSACTION")
            for node in nodes:
                self.current_store.write_node(
                    id=node["id"],
                    label=node["label"],
                    type=node.get("type", "concept"),
                    content=node.get("content"),
                    confidence=node.get("confidence", 1.0),
                    visibility=node.get("visibility", "team"),
                    provenance=node.get("provenance"),
                    tags=node.get("tags"),
                    metadata=node.get("metadata"),
                    priority=node.get("priority"),
                    url=node.get("url"),
                    task_status=node.get("task_status"),
                    assigned_to=node.get("assigned_to"),
                    due_date=node.get("due_date"),
                    utility_scale=node.get("utility_scale"),
                    current_best_action=node.get("current_best_action"),
                    action_alternatives=node.get("action_alternatives"),
                    utility_usd_per_day=node.get("utility_usd_per_day"),
                    utility_currency=node.get("utility_currency"),
                    agent_name=agent,
                )
                nodes_created += 1
            for edge in edges:
                self.current_store.write_edge(
                    from_node=edge["from"],
                    to_node=edge["to"],
                    edge_type=edge["type"],
                    layer=edge.get("layer", "L3"),
                    confidence=edge.get("confidence"),
                    condition=edge.get("condition"),
                    provenance=edge.get("provenance"),
                    challenge_of=edge.get("challenge_of"),
                    challenge_type=edge.get("challenge_type"),
                    urgency=edge.get("urgency"),
                    probability=edge.get("probability"),
                    probability_p05=edge.get("probability_p05"),
                    probability_p50=edge.get("probability_p50"),
                    probability_p95=edge.get("probability_p95"),
                    confidence_p05=edge.get("confidence_p05"),
                    confidence_p50=edge.get("confidence_p50"),
                    confidence_p95=edge.get("confidence_p95"),
                    agent_name=agent,
                )
                edges_created += 1
            self.current_store.conn.execute("COMMIT")
        except Exception:
            self.current_store.conn.execute("ROLLBACK")
            raise

        self._json_response(
            201,
            {
                "nodes_created": nodes_created,
                "edges_created": edges_created,
                "errors": errors,
            },
        )

    def _post_webhook(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /webhook — register or update webhook callback URL for this agent."""
        url = body.get("url", "")
        events = body.get("events", ["node.created", "node.updated", "edge.created"])
        if not url:
            raise ValidationError("Webhook requires a 'url' field")
        _validate_webhook_url(url)
        with _webhook_lock:
            if self._customer_id not in _webhook_registry:
                _webhook_registry[self._customer_id] = {}
            _webhook_registry[self._customer_id][agent] = {"url": url, "events": events}
        self._json_response(
            200,
            {
                "status": "registered",
                "agent": agent,
                "url": url,
                "events": events,
            },
        )

    def _post_state(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /state — update agent state/focus."""
        result = self.current_store.update_agent_state(
            current_focus=body.get("focus"),
            active_patterns=body.get("patterns"),
            available_services=body.get("services"),
            session_id=body.get("session_id"),
            agent_name=agent,
        )
        self._json_response(200, result)

    def _post_register(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /register — agent registration (idempotent: creates or updates agent node + edges)."""
        # If an agent with the same name already exists, reuses its node and
        # refreshes its edges (deletes old, creates new).
        from ohm.queries import create_edge, find_or_create_node

        agent_label = body.get("name", agent)
        # Use deterministic ID for agent nodes to prevent duplicates
        import re

        agent_id = "agent_" + re.sub(r"[^a-zA-Z0-9]+", "_", agent_label.lower()).strip("_")

        # Check if agent node already exists (including soft-deleted)
        existing_active = self.current_store.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id]).fetchone()
        existing_soft_deleted = self.current_store.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [agent_id]).fetchone()

        if existing_active:
            # Update existing agent node (description may have changed)
            self.current_store.conn.execute(
                "UPDATE ohm_nodes SET content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                [body.get("description"), agent, agent_id],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
            # Soft-delete old registration edges
            reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
            placeholders = ",".join(["?"] * len(reg_edge_types))
            self.current_store.conn.execute(
                f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                [agent_id] + list(reg_edge_types),
            )
        elif existing_soft_deleted:
            # Reactivate soft-deleted agent node
            self.current_store.conn.execute(
                """UPDATE ohm_nodes SET
                    content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ?,
                    deleted_at = NULL
                WHERE id = ?""",
                [body.get("description"), agent, agent_id],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
            # Soft-delete old registration edges
            reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
            placeholders = ",".join(["?"] * len(reg_edge_types))
            self.current_store.conn.execute(
                f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                [agent_id] + list(reg_edge_types),
            )
        else:
            # Create new agent node with deterministic ID
            self.current_store.conn.execute(
                """INSERT INTO ohm_nodes
                   (id, label, type, content, created_by, confidence, visibility, created_at, updated_at)
                   VALUES (?, ?, 'agent', ?, ?, 1.0, 'team', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                [agent_id, agent_label, body.get("description"), agent],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]

        created_edges = []
        for v in body.get("values", []):
            value_node = find_or_create_node(
                self.current_store.conn,
                label=v,
                node_type="value",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=value_node["id"],
                edge_type="VALUES",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for g in body.get("goals", []):
            goal_node = find_or_create_node(
                self.current_store.conn,
                label=g,
                node_type="goal",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=goal_node["id"],
                edge_type="GOALS",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for c in body.get("capabilities", []):
            cap_node = find_or_create_node(
                self.current_store.conn,
                label=c,
                node_type="skill",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=cap_node["id"],
                edge_type="CAPABLE_OF",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for i in body.get("interests", []):
            topic_node = find_or_create_node(
                self.current_store.conn,
                label=i,
                node_type="topic",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=topic_node["id"],
                edge_type="INTERESTED_IN",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for a in body.get("listens_to", []):
            other = find_or_create_node(
                self.current_store.conn,
                label=a,
                node_type="agent",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=other["id"],
                edge_type="LISTENS_TO",
                layer="L3",
                created_by=agent,
                confidence=0.7,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        self._json_response(
            201,
            {
                "agent": me,
                "edges_created": len(created_edges),
            },
        )

    def _post_sync(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /sync — explicit DuckLake sync trigger (OHM-7301)."""
        sync_result = self.current_store.sync_heartbeat()
        self._json_response(200, sync_result)

    def _post_task(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /tasks — create a task node (OHM-7304)."""
        import re

        task_id = body.get("id") or ("task_" + re.sub(r"[^a-z0-9]+", "_", body["label"].lower()).strip("_")[:48] + "_" + str(uuid.uuid4())[:8])
        result = self.current_store.write_node(
            id=task_id,
            label=body["label"],
            type="task",
            content=body.get("content"),
            confidence=body.get("confidence", 1.0),
            visibility=body.get("visibility", "team"),
            provenance=body.get("provenance"),
            tags=body.get("tags"),
            metadata=body.get("metadata"),
            priority=body.get("priority"),
            url=body.get("url"),
            task_status=body.get("task_status", "open"),
            assigned_to=body.get("assigned_to"),
            due_date=body.get("due_date"),
            utility_usd_per_day=body.get("utility_usd_per_day"),
            utility_currency=body.get("utility_currency"),
            agent_name=agent,
        )
        _trigger_webhooks({"type": "task.created", "agent": agent, "node": result}, customer_id=self._customer_id)
        if result.get("created", True):
            self._json_response(201, result)
        else:
            self._json_response(200, result)

    def _post_heartbeat(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /heartbeat — agent heartbeat with sync."""
        from ohm.methods import agent_heartbeat

        result = agent_heartbeat(
            self.current_store.conn,
            agent,
            focus=body.get("focus"),
        )
        # Also sync with DuckLake if configured
        sync_result = self.current_store.sync_heartbeat()
        result["ducklake_sync"] = sync_result
        self._json_response(200, result)

    def _post_deduplicate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /deduplicate — remove duplicate edges (same from→to, type, layer), keeping most recent."""
        layer = qs.get("layer", [None])[0]
        if layer:
            from ohm.validation import validate_layer

            try:
                validate_layer(layer)
            except ValueError as e:
                raise ValidationError(str(e))
        removed = self.current_store.deduplicate_edges(layer=layer)
        self._json_response(200, {"removed": removed, "layer": layer})

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
            customer_id = self._customer_id
            if customer_id and self.tenant_manager:
                from ohm.tenant import TenantNotFoundError

                try:
                    write_lock = self.tenant_manager.get_write_lock(customer_id)
                except TenantNotFoundError:
                    raise NodeNotFoundError(
                        f"Tenant not found — provision this tenant before use"
                    )
                with write_lock:
                    getattr(self, method_name)(path, agent)
            else:
                getattr(self, method_name)(path, agent)
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

    def _delete_node(self, path: str, agent: str) -> None:
        """DELETE /node/{id} — removes a node and its associated edges."""
        node_id = path[6:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")

        # Verify node exists (idempotent 404)
        node = self.current_store.conn.execute(
            "SELECT id, created_by FROM ohm_nodes WHERE id = ?",
            [node_id],
        ).fetchone()
        if not node:
            raise NodeNotFoundError(f"Node not found: {node_id}")

        # Use store method — splits edge deletion to avoid DuckDB index issues (OHM-cpi)
        # Ownership enforced by OhmStore.delete_node() via boundary layer.
        result = self.current_store.delete_node(node_id, deleted_by=agent)
        self._json_response(200, result)

    def _delete_edge(self, path: str, agent: str) -> None:
        """DELETE /edge/{id} — removes an edge."""
        edge_id = path[6:]
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")

        # Verify edge exists (idempotent 404)
        edge = self.current_store.conn.execute(
            "SELECT id, created_by FROM ohm_edges WHERE id = ?",
            [edge_id],
        ).fetchone()
        if not edge:
            raise EdgeNotFoundError(f"Edge not found: {edge_id}")

        # Use store method — ownership enforced by OhmStore.delete_edge() via boundary layer.
        result = self.current_store.delete_edge(edge_id, deleted_by=agent)
        self._json_response(200, result)


# ── Dispatch table population ─────────────────────────────────────────────────
# Populated after class body so method names are valid references.

OhmHandler._DELETE_PREFIXES = [
    ("/node/", "_delete_node"),
    ("/edge/", "_delete_edge"),
    ("/tenant/", "_delete_tenant_prefix"),
]

OhmHandler._POST_EXACT = {
    "/tenant/provision": "_post_tenant_provision",
    "/node": "_post_node",
    "/node/find_or_create": "_post_node_find_or_create",
    "/edge": "_post_edge",
    "/observations": "_post_observations",
    "/outcome": "_post_outcome",
    "/agent/synthesis": "_post_synthesis",
    "/batch": "_post_batch",
    "/webhook": "_post_webhook",
    "/state": "_post_state",
    "/register": "_post_register",
    "/heartbeat": "_post_heartbeat",
    "/sync": "_post_sync",
    "/tasks": "_post_task",
    "/deduplicate": "_post_deduplicate",
    "/admin/checkpoint": "_post_admin_checkpoint",
    "/orphans/purge": "_post_orphans_purge",
}

OhmHandler._POST_PREFIXES = [
    ("/tenant/", "_post_tenant_export"),
    ("/challenge/", "_post_challenge"),
    ("/support/", "_post_support"),
    ("/observe/", "_post_observe"),
]

OhmHandler._GET_EXACT = {
    "/": "_get_infra_root",
    "": "_get_infra_root",
    "/openapi.json": "_get_infra_openapi",
    "/health": "_get_infra_health",
    "/ready": "_get_infra_ready",
    "/metrics": "_get_infra_metrics",
    "/stats": "_get_stats",
    "/status": "_get_status",
    "/schema": "_get_schema",
    "/layers": "_get_layers",
    "/agents": "_get_agents",
    "/nodes": "_get_nodes",
    "/tasks": "_get_tasks",
    "/listen": "_get_listen",
    "/search": "_get_search",
    "/semantic_search": "_get_semantic_search",
    "/health/graph": "_get_health_graph",
    "/health/agents": "_get_health_agents",
    "/health/sync": "_get_health_sync",
    "/contradictions": "_get_contradictions",
    "/anomalies": "_get_anomalies",
    "/stale": "_get_stale",
    "/decay": "_get_decay",
    "/duplicates": "_get_duplicates",
    "/orphans": "_get_orphans",
    "/hubs": "_get_hubs",
    "/dead_ends": "_get_dead_ends",
    "/suggest": "_get_suggest",
    "/graph/stats": "_get_graph_stats",
    "/lint": "_get_lint",
    "/contract": "_get_contract",
    "/inference": "_get_inference",
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
    "/admin/checkpoint": "_get_admin_checkpoint",
    "/admin/embeddings": "_get_admin_embeddings",
    "/admin/snapshots": "_get_admin_snapshots",
    "/graph/at": "_get_graph_at",
    "/graph/changes": "_get_graph_changes",
    "/observations": "_get_observations",
    "/source_reliability": "_get_source_reliability",
    "/webhooks/dead-letter": "_get_webhooks_dead_letter",
    "/webhooks/outbox": "_get_webhooks_outbox",
    "/tenants": "_get_tenants",
}


OhmHandler._GET_PREFIXES = [
    ("/tenant/", "_get_tenant_prefix"),
    ("/node/", "_get_node"),
    ("/deep/", "_get_deep"),
    ("/edge/", "_get_edge"),
    ("/neighborhood/", "_get_neighborhood"),
    ("/path/", "_get_path"),
    ("/impact/", "_get_impact"),
    ("/confidence/", "_get_confidence"),
    ("/agent/", "_get_agent"),
    ("/aggregate/", "_get_aggregate"),
    ("/provenance/", "_get_provenance"),
    ("/monte-carlo/", "_get_monte_carlo"),
    ("/calibration/", "_get_calibration"),
    ("/reliability/", "_get_reliability"),
    ("/compound_confidence/", "_get_compound_confidence"),
]


def run_server(config: dict, store: OhmStore, schema_config: SchemaConfig | None = None):
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

    OhmHandler.store = store
    OhmHandler.config = config
    token_hashes, config_roles = _build_token_lookup(config.get("tokens", {}))
    OhmHandler.tokens = token_hashes
    OhmHandler.customer_tokens = _build_customer_token_lookup(config.get("customer_tokens", {}))
    OhmHandler.roles = config_roles if config_roles else config.get("roles", {})
    OhmHandler.no_auth = config.get("no_auth", False)
    OhmHandler.require_read_auth = config.get("require_read_auth", False)
    OhmHandler.schema_config = schema_config
    OhmHandler.multi_tenant = config.get("multi_tenant", False)

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

    server = ThreadedHTTPServer((config["host"], config["port"]), OhmHandler)
    print(f"OHM daemon listening on {config['host']}:{config['port']}", file=sys.stderr)

    # Background DuckLake sync thread (OHM-1rwl): sync every sync_interval_seconds
    # so data is not lost between heartbeats on hard shutdown (SIGKILL/OOM).
    ducklake_sync_interval = config.get("sync_interval_seconds", 60)
    _sync_stop = threading.Event()

    def _ducklake_sync_loop():
        consecutive_sync_errors = 0
        while not _sync_stop.wait(ducklake_sync_interval):
            try:
                store.sync_heartbeat()
                consecutive_sync_errors = 0  # Reset on success (OHM-nnu2)
            except Exception:
                consecutive_sync_errors += 1
                logger.exception(
                    "DuckLake sync failed (attempt %d): ", consecutive_sync_errors
                )
                if consecutive_sync_errors >= 3:
                    # Exponential backoff after 3 consecutive errors (OHM-nnu2, same pattern as OHM-s8sg)
                    backoff = min(60, 5 * (2 ** (consecutive_sync_errors - 3)))
                    logger.warning("DuckLake sync backing off %ds after %d consecutive errors", backoff, consecutive_sync_errors)
                    _sync_stop.wait(backoff)  # Interruptible sleep

    if hasattr(store, "sync_heartbeat") and ducklake_sync_interval > 0:
        _sync_thread = threading.Thread(target=_ducklake_sync_loop, daemon=True, name="ducklake-sync")
        _sync_thread.start()
    print(f"Schema: {schema_config.name}", file=sys.stderr)
    if OhmHandler.multi_tenant:
        print("Multi-tenancy: ENABLED", file=sys.stderr)
    else:
        print("Multi-tenancy: disabled (single-tenant mode)", file=sys.stderr)
    if quack_info:
        print("Concurrent access: Quack (multi-writer)", file=sys.stderr)
    else:
        print("Concurrent access: HTTP (single-writer)", file=sys.stderr)

    # Graceful shutdown — CHECKPOINT before exit (OHM-8n9, OHM-xfqp)
    def shutdown_handler(signum, frame):
        print("Shutting down...", file=sys.stderr)
        _sync_stop.set()
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
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    # Ignore SIGPIPE to prevent crashes on broken client connections
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    server.serve_forever()
    store.close()


def main(schema_config: SchemaConfig | None = None):
    """CLI entry point for ohmd.

    Args:
        schema_config: SchemaConfig to use. Defaults to DEFAULT_SCHEMA.
            Pass TOPO_SCHEMA for the topod entry point.
    """
    if schema_config is None:
        schema_config = DEFAULT_SCHEMA

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
        choices=["ohm", "topo"],
        default=None,
        help="Schema configuration (default: determined by entry point)",
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
    args = parser.parse_args()

    # Allow CLI override of schema
    if args.schema == "topo":
        from ohm.schema import TOPO_SCHEMA

        schema_config = TOPO_SCHEMA

    config = load_config(args.config)

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
        config_path = Path(os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json")))
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
        config_path = Path(os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json")))
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

    store = OhmStore(db_path=config["db_path"], agent_name="ohmd")
    print(f"OHM database: {config['db_path']}", file=sys.stderr)
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
        )
        if attached:
            print(f"DuckLake attached: {ducklake_path}", file=sys.stderr)
        else:
            print("DuckLake extension not available — lakehouse features disabled", file=sys.stderr)

    # Run server
    try:
        run_server(config, store, schema_config=schema_config)
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
        "topod is deprecated. Use 'ohmd --schema topo' or provision a "
        "multi-tenant instance with domain='topo'. See "
        "docs/upgrade_multi_tenancy.md#topo-migration for migration steps. "
        "topod will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    logger.warning(
        "topod is deprecated. Use 'ohmd --schema topo' or multi-tenant "
        "provisioning with domain='topo'."
    )
    main(schema_config=TOPO_SCHEMA)


if __name__ == "__main__":
    main()
