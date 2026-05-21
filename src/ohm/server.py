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

from .exceptions import (
    AuthenticationError,
    ConflictError,
    EdgeNotFoundError,
    NodeNotFoundError,
    OHMError,
    PermissionDeniedError,
    ValidationError,
)
from .schema import DEFAULT_SCHEMA, SchemaConfig, VALID_VISIBILITIES
from .store import OhmStore


# ── Configuration ──────────────────────────────────────────

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8710,
    "db_path": str(Path.home() / ".ohm" / "ohm.duckdb"),
    "tokens": {
        # agent_name: token_string
        # Populated from config file or env vars
    },
    "log_level": "INFO",
    "ducklake": {
        "path": "",  # DuckLake catalog path (e.g., /var/lib/ohm/ohm_lake.ducklake)
        "data_path": "",  # Parquet data path (e.g., /var/lib/ohm/ohm_lake_data)
        "sync_interval_seconds": 60,  # How often to sync to DuckLake
    },
}

_START_TIME = time.time()

# ── Security Constants ─────────────────────────────────────

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB — reject bodies larger than this
MAX_BATCH_SIZE = 500  # Maximum nodes + edges per /batch request
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 1000  # per window per IP

# Simple in-memory rate limiter: {ip: [(timestamp, ...)]}
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

# In-memory registry: {agent_name: {"url": str, "events": list[str]}}
# Agents register their callback URL and the event types they want to receive.
_webhook_registry: dict[str, dict] = {}
_webhook_lock = threading.Lock()


# ── SSE Subscriber Registry ──────────────────────────────────────────────────

# In-memory registry: {subscription_id: {"agent_name": str, "since": str, "last_event_id": str}}
# SSE subscribers receive real-time change feed events as they occur.
_sse_subscribers: dict[str, dict] = {}
_sse_lock = threading.Lock()


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
                raise ValidationError(
                    f"Webhook URL targets a private/loopback address ({addr}) — SSRF not allowed"
                )
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


def _trigger_webhooks(event: dict) -> None:
    """Trigger webhooks for all registered agents matching the event.

    Events are delivered asynchronously to avoid blocking the request that
    triggered them. Each registered agent receives the event if they subscribed
    to that event type.
    """
    import concurrent.futures

    event_type = event.get("type", "")

    def deliver_to_agent(agent_name: str, config: dict) -> None:
        url = config.get("url", "")
        events = config.get("events", [])
        if url and (event_type in events or "*" in events):
            _deliver_webhook(url, event)

    # Snapshot registry under lock, then deliver without holding the lock
    with _webhook_lock:
        registry_snapshot = dict(_webhook_registry)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for agent_name, config in registry_snapshot.items():
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
        "/stats", "/status", "/schema", "/layers", "/agents", "/nodes",
        "/listen", "/search", "/semantic_search",
        "/inference", "/intervene", "/ate", "/sensitivity", "/adjustment",
        "/voi", "/voi/tasks", "/suggest_causes", "/refute",
        "/lint", "/contract", "/duplicates", "/stale",
        "/admin/embeddings", "/admin/snapshots", "/graph/at", "/graph/changes",
    ):
        r.add("GET", _p)

    # GET prefix routes (parameterised paths like /node/{id})
    for _p in (
        "/node/", "/deep/", "/edge/", "/neighborhood/", "/path/",
        "/impact/", "/confidence/", "/agent/", "/provenance/",
        "/monte-carlo/", "/calibration/", "/reliability/",
    ):
        r.add("GET", _p)

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
        "/node", "/node/find_or_create", "/edge",
        "/outcome", "/agent/synthesis", "/batch",
        "/webhook", "/state", "/register", "/heartbeat",
    ):
        r.add("POST", _p)

    # POST prefix routes
    for _p in ("/challenge/", "/support/", "/observe/", "/webhook/"):
        r.add("POST", _p)

    # PATCH
    r.add("PATCH", "/edge/")

    # DELETE
    r.add("DELETE", "/node/")
    r.add("DELETE", "/edge/")

    return r


_ROUTER = _build_router()
del _build_router


# ── HTTP Handler ───────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads for concurrent access."""
    daemon_threads = True


class OhmHandler(BaseHTTPRequestHandler):
    """HTTP request handler for OHM daemon."""

    store: Optional[OhmStore] = None
    config: dict = {}
    tokens: dict = {}  # token -> agent_name
    roles: dict = {}    # agent_name -> role (read-write, read-only)
    no_auth: bool = False  # --no-auth flag: bypass all auth (dev mode)
    require_read_auth: bool = False  # OHM-gwg: require auth for reads (default: public reads)
    schema_config: SchemaConfig = DEFAULT_SCHEMA  # configurable schema (OHM or TOPO)

    def log_message(self, format, *args):
        """Structured request logging with correlation ID."""
        import re
        corr_id = getattr(self, "_correlation_id", "-")
        timestamp = datetime.now(timezone.utc).isoformat()
        message = format % args
        message = re.sub(r'([?&]token=)[^&\s]+', r'\1[REDACTED]', message)
        sys.stderr.write(
            f"[{timestamp}] [{corr_id}] {message}\n"
        )
        sys.stderr.flush()

    def _authenticate(self) -> Optional[str]:
        """Validate bearer token using constant-time comparison, return agent name or None."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            from urllib.parse import unquote
            token = unquote(auth[7:])
            for token_hash, agent_name in self.tokens.items():
                if _verify_token(token, token_hash):
                    return agent_name
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        if "token" in qs:
            token = qs["token"][0]
            for token_hash, agent_name in self.tokens.items():
                if _verify_token(token, token_hash):
                    return agent_name
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
            raise PermissionDeniedError(
                f"Agent '{agent}' has read-only access — writes are not permitted"
            )
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
        body = json.dumps({
            "error": "method_not_allowed",
            "message": f"Method not allowed — use: {allow_header}",
            "allow": allow_header,
        }, indent=2).encode()
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
            assert self.store is not None
            state = self.store.get_agent_state(agent)
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
        from .queries import query_change_feed
        last_ts = since
        event_count = 0
        max_events = 1000  # Safety limit
        batch_size = 50  # Batch events for efficiency

        try:
            assert self.store is not None
            while event_count < max_events:
                # Query for new changes since last event
                changes = query_change_feed(
                    self.store.conn,
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
            _rate_limit_store[client_ip] = [
                ts for ts in _rate_limit_store[client_ip] if ts > window_start
            ]

            # OHM-41g: Prune stale IP keys that have no recent timestamps.
            # Without this, unique IPs accumulate forever.
            if not _rate_limit_store[client_ip]:
                del _rate_limit_store[client_ip]
                # Periodically prune other stale keys (every ~100 requests)
                if len(_rate_limit_store) > 100:
                    stale = [
                        ip for ip, timestamps in _rate_limit_store.items()
                        if not timestamps or timestamps[-1] < window_start
                    ]
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
        "/edge": ["from", "to", "type"],
        "/state": [],
        "/register": [],
        "/heartbeat": [],
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
            "id": str, "label": str,
            "type": str, "content": (str, type(None)),
            "confidence": (int, float), "visibility": str,
            "provenance": (str, type(None)), "tags": (list, type(None)),
            "metadata": (dict, type(None)),
        },
        "/edge": {
            "from": str, "to": str, "type": str,
            "layer": str, "confidence": (int, float, type(None)),
            "condition": (str, type(None)), "provenance": (str, type(None)),
            "challenge_of": (str, type(None)), "challenge_type": (str, type(None)),
        },
        "/state": {
            "focus": (str, type(None)), "patterns": (list, type(None)),
            "services": (list, type(None)), "session_id": (str, type(None)),
        },
        "/register": {
            "name": (str, type(None)), "description": (str, type(None)),
            "values": (list, type(None)), "goals": (list, type(None)),
            "capabilities": (list, type(None)), "interests": (list, type(None)),
            "listens_to": (list, type(None)),
        },
        "/heartbeat": {
            "focus": (str, type(None)),
        },
        "/challenge": {
            "reason": (str, type(None)), "confidence": (int, float, type(None)),
            "challenge_type": (str, type(None)),
        },
        "/support": {
            "reason": (str, type(None)), "confidence": (int, float, type(None)),
        },
        "/observe": {
            "type": (str, type(None)), "value": (int, float, str, type(None)),
            "baseline": (int, float, type(None)), "sigma": (int, float, type(None)),
            "source": (str, type(None)), "notes": (str, type(None)),
            "source_name": (str, type(None)), "source_url": (str, type(None)),
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
                        type_names = " / ".join(
                            t.__name__ if hasattr(t, "__name__") else str(t)
                            for t in expected
                        )
                    else:
                        type_names = expected.__name__ if hasattr(expected, "__name__") else str(expected)
                    raise ValidationError(
                        f"Field '{field}' must be {type_names}, "
                        f"got {type(value).__name__}"
                    )

        # Validate specific field values
        if validation_path == "/node":
            from .validation import validate_identifier
            try:
                validate_identifier(body["id"], name="id")
            except ValueError as e:
                raise ValidationError(str(e))
            if "type" in body and body["type"] not in self.schema_config.node_types:
                raise ValidationError(
                    f"Invalid node type: '{body['type']}'"
                    f" — must be one of: {', '.join(sorted(self.schema_config.node_types))}"
                )
            if "visibility" in body and body["visibility"] not in VALID_VISIBILITIES:
                raise ValidationError(f"Invalid visibility: '{body['visibility']}' — must be private, team, or public")
            if "confidence" in body:
                from .validation import validate_confidence
                try:
                    validate_confidence(float(body["confidence"]))
                except ValueError as e:
                    raise ValidationError(str(e))
            # Task-specific field validation
            if "task_status" in body and body["task_status"] is not None:
                from .schema import VALID_TASK_STATUSES
                if body["task_status"] not in VALID_TASK_STATUSES:
                    raise ValidationError(
                        f"Invalid task_status: '{body['task_status']}'"
                        f" — must be one of: {', '.join(sorted(VALID_TASK_STATUSES))}"
                    )
            if "priority" in body and body["priority"] is not None:
                from .schema import VALID_PRIORITY
                if body["priority"] not in VALID_PRIORITY:
                    raise ValidationError(
                        f"Invalid priority: '{body['priority']}'"
                        f" — must be one of: {', '.join(sorted(VALID_PRIORITY))}"
                    )

        elif validation_path == "/edge":
            from .validation import validate_identifier, validate_layer
            try:
                validate_identifier(body["from"], name="from_node")
                validate_identifier(body["to"], name="to_node")
            except ValueError as e:
                raise ValidationError(str(e))
            if body["type"] not in self.schema_config.all_edge_types:
                raise ValidationError(
                    f"Invalid edge type: '{body['type']}'"
                    f" — must be one of: {', '.join(sorted(self.schema_config.all_edge_types))}"
                )
            if "layer" in body:
                try:
                    validate_layer(body["layer"])
                except ValueError as e:
                    raise ValidationError(str(e))
            if "confidence" in body and body["confidence"] is not None:
                from .validation import validate_confidence
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
                from .validation import validate_confidence
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
                self._json_response(429, {
                    "error": "rate_limited",
                    "message": "Too many requests. Try again later.",
                    "correlation_id": self._correlation_id,
                })
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
                "GET %s → %s (%.1fms)", self.path, code, elapsed,
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
                self._json_response(429, {
                    "error": "rate_limited",
                    "message": "Too many requests. Try again later.",
                    "correlation_id": self._correlation_id,
                })
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
                "POST %s → %s (%.1fms)", self.path, code, elapsed,
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
                self._json_response(429, {
                    "error": "rate_limited",
                    "message": "Too many requests. Try again later.",
                    "correlation_id": self._correlation_id,
                })
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
                "DELETE %s → %s (%.1fms)", self.path, code, elapsed,
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
                self._json_response(429, {
                    "error": "rate_limited",
                    "message": "Too many requests. Try again later.",
                    "correlation_id": self._correlation_id,
                })
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
                "PATCH %s → %s (%.1fms)", self.path, code, elapsed,
            )

    def _do_PATCH(self):
        """Handle PATCH /edge/{id} — partial update of PERT fields on an existing edge."""
        from urllib.parse import urlparse
        from .validation import validate_identifier
        from .exceptions import NodeNotFoundError

        agent = self._require_write_auth()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()

        if path.startswith("/edge/"):
            edge_id = path[6:]
            edge_id = validate_identifier(edge_id, name="edge_id")
            edge = self.store.get_edge(edge_id)
            if not edge:
                raise NodeNotFoundError(f"Edge not found: {edge_id}")

            now = datetime.now(timezone.utc).isoformat()
            pert_fields = [
                "probability", "probability_p05", "probability_p50", "probability_p95",
                "confidence", "confidence_p05", "confidence_p50", "confidence_p95",
                "condition", "provenance", "urgency",
            ]
            update_fields = []
            update_params = []
            for field in pert_fields:
                if field in body:
                    update_fields.append(f"{field} = ?")
                    update_params.append(body[field])

            # Recompute PERT mean if p50 provided and probability not explicitly set
            if "probability_p50" in body and "probability" not in body:
                from .pert import compute_pert_mean
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

            self.store.conn.execute(
                f"UPDATE ohm_edges SET {', '.join(update_fields)} WHERE id = ?",
                update_params,
            )
            self.store._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"], agent_name=agent)
            self.store._increment_graph_generation()  # Invalidate Bayesian network cache on edge update
            updated = self.store.get_edge(edge_id)
            _trigger_webhooks({"type": "edge.updated", "agent": agent, "edge": updated})
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

        # Infrastructure endpoints bypass auth
        if path == "" or path == "/":
            # Root discovery endpoint — OpenAPI-style route listing (ADR-005)
            self._json_response(200, {
                "service": "ohmd",
                "version": "0.2.0",
                "schema": self.schema_config.name,
                "description": "Multi-agent knowledge graph daemon",
                "auth_model": "public-read" if not self.require_read_auth else "authenticated",
                "endpoints": {
                    "/": {"method": "GET", "description": "This discovery index (no auth required)"},
                    "/health": {"method": "GET", "description": "Health check (no auth required)"},
                    "/ready": {"method": "GET", "description": "Readiness check (no auth required)"},
                    "/metrics": {"method": "GET", "description": "Prometheus-style metrics"},
                    "/stats": {"method": "GET", "description": "Graph statistics (nodes, edges, layers)"},
                    "/inference": {"method": "GET", "description": "Bayesian inference: compute posterior probabilities given evidence (observation, includes confounders). ?layers=L3,L4 to scope by layer"},
                    "/intervene": {"method": "GET", "description": "Causal intervention using Pearl's do-operator: sever incoming edges to target, set value externally, propagate direct causal effect (no confounders). ?layers=L3,L4 to scope by layer"},
                    "/ate": {"method": "GET", "description": "Average Treatment Effect: model-based ATE from noisy-OR CPDs (ATE = P(effect=bad|do(cause=bad)) - P(effect=bad|do(cause=good))). ?layers=L3,L4 to scope by layer"},
                    "/sensitivity": {"method": "GET", "description": "Sensitivity analysis: E-value quantifying how much unmeasured confounding would overturn a causal conclusion. ?layers=L3,L4 to scope by layer"},
                    "/adjustment": {"method": "GET", "description": "Find valid backdoor/frontdoor adjustment sets for causal identification (Pearl's criteria). ?layers=L3,L4 to scope by layer"},
                    "/voi": {"method": "GET", "description": "Value of Information: rank nodes by research priority (uncertainty × sensitivity to decision). ?decision=node1,node2&top=10&layers=L3,L4&edge_types=CAUSES,DEPENDS_ON"},
                    "/voi/tasks": {"method": "GET", "description": "Generate research tasks from VoI rankings, matched to agent expertise. ?agent=metis&decision=node1,node2&top=5&layers=L3,L4"},
                    "/suggest_causes": {"method": "GET", "description": "Suggest candidate CAUSES edges from existing non-causal relationships (DEPENDS_ON, APPLIES_TO, etc.)"},
                    "/deduplicate": {"method": "POST", "description": "Remove duplicate edges (same from→to, type, layer), keeping the most recent"},
                    "/refute": {"method": "GET", "description": "Test robustness of causal conclusions using DoWhy refutation methods (random common cause, placebo, data subset, unobserved confounder)"},
                    "/lint": {"method": "GET", "description": "Contract layer linting: validate graph against naming conventions and required fields"},
                    "/contract": {"method": "GET", "description": "Current contract configuration (naming conventions, required fields, schema)"},
                    "/status": {"method": "GET", "description": "Daemon status and configuration"},
                    "/schema": {"method": "GET", "description": "Node types, edge types, layers"},
                    "/layers": {"method": "GET", "description": "L1-L4 layer descriptions"},
                    "/node/{id}": {"method": "GET", "description": "Get a single node by ID"},
                    "/edge/{id}": {"method": "GET", "description": "Get a single edge by ID"},
                    "/neighborhood/{id}": {"method": "GET", "description": "Bounded-depth graph traversal"},
                    "/path/{from}/{to}": {"method": "GET", "description": "Shortest path between two nodes"},
                    "/impact/{id}": {"method": "GET", "description": "Downstream failure impact analysis"},
                    "/confidence/{id}": {"method": "GET", "description": "Provenance and challenge audit"},
                    "/agent/{name}": {"method": "GET", "description": "Agent state and focus"},
                    "/agents": {"method": "GET", "description": "List all registered agents"},
                    "/nodes": {"method": "GET", "description": "List nodes with pagination and filtering"},
                    "/listen": {"method": "GET", "description": "Change feed since last check"},
                    "/events": {"method": "GET", "description": "SSE stream of real-time change feed events"},
                    "/node": {"method": "POST", "description": "Create a new node"},
                    "/edge": {"method": "POST", "description": "Create a new edge"},
                    "/challenge/{id}": {"method": "POST", "description": "Challenge an existing edge"},
                    "/support/{id}": {"method": "POST", "description": "Support an existing edge"},
                    "/observe/{id}": {"method": "POST", "description": "Record an observation on a node"},
                    "/observations": {"method": "GET", "description": "List observations with filtering by type, source, node_id. POST for bulk upload: {observations: [{node_id, value, sigma, obs_type, source}]}"},
                    "/outcome": {"method": "POST", "description": "Record whether a source agent's claim was correct"},
                    "/reliability/{source}": {"method": "GET", "description": "Compute source reliability metrics from historical outcomes"},
                    "/state": {"method": "POST", "description": "Update agent state/focus"},
                    "/register": {"method": "POST", "description": "Register a new agent"},
                    "/heartbeat": {"method": "POST", "description": "Agent heartbeat with sync"},
                    "/webhook/{agent}": {"method": "POST", "description": "Register a webhook callback"},
                    "/search": {"method": "GET", "description": "ILIKE text search (?q=QUERY)"},
                    "/semantic_search": {"method": "GET", "description": "Semantic vector search (requires Ollama)"},
                    "/admin/checkpoint": {"method": "POST", "description": "Force DuckDB CHECKPOINT (flush WAL to main DB)"},
                    "/admin/embeddings": {"method": "GET", "description": "Batch generate embeddings for nodes missing them (?batch_size=N&delay_ms=M)"},
                    "/admin/snapshots": {"method": "GET", "description": "List DuckLake snapshots (time-travel)"},
                    "/graph/at": {"method": "GET", "description": "Query graph at snapshot version (?version=N)"},
                    "/graph/changes": {"method": "GET", "description": "Changes between snapshots"},
                },
                "links": {
                    "schema": "/schema",
                    "layers": "/layers",
                    "health": "/health",
                    "docs": "https://github.com/mdlmarkham/OHM",
                },
            })
            return
        elif path == "/openapi.json":
            # OpenAPI 3.0 spec endpoint (ADR-005)
            self._json_response(200, {
                "openapi": "3.0.3",
                "info": {
                    "title": "OHM Daemon API",
                    "version": "0.2.0",
                    "description": "Multi-agent knowledge graph daemon — shared awareness, individual judgment.",
                },
                "servers": [{"url": (
                    f"http://{self.config.get('host', '127.0.0.1')}"
                    f":{self.config.get('port', 8710)}"
                )}],
                "paths": {
                    "/": {"get": {"summary": "Discovery index",
                               "responses": {"200": {"description": "Route listing"}}}},
                    "/health": {"get": {"summary": "Health check", "responses": {"200": {"description": "OK"}}}},
                    "/ready": {"get": {"summary": "Readiness check",
                                   "responses": {"200": {"description": "Ready"},
                                                 "503": {"description": "Not ready"}}}},
                    "/metrics": {"get": {"summary": "Prometheus-style metrics",
                                    "responses": {"200": {"description": "Metrics"}}}},
                    "/stats": {"get": {"summary": "Graph statistics", "responses": {"200": {"description": "Stats"}}}},
                    "/status": {"get": {"summary": "Daemon status", "responses": {"200": {"description": "Status"}}}},
                    "/schema": {"get": {"summary": "Node/edge types", "responses": {"200": {"description": "Schema"}}}},
                    "/layers": {"get": {"summary": "L1-L4 descriptions",
                                   "responses": {"200": {"description": "Layers"}}}},
                    "/node/{id}": {"get": {"summary": "Get node"}, "post": {"summary": "Create node"}},
                    "/edge/{id}": {"get": {"summary": "Get edge"}, "post": {"summary": "Create edge"}},
                    "/neighborhood/{id}": {"get": {"summary": "Graph traversal"}},
                    "/path/{from}/{to}": {"get": {"summary": "Shortest path"}},
                    "/impact/{id}": {"get": {"summary": "Impact analysis"}},
                    "/confidence/{id}": {"get": {"summary": "Confidence audit"}},
                    "/agent/{name}": {"get": {"summary": "Agent state"}},
                    "/agents": {"get": {"summary": "List agents"}},
                    "/nodes": {"get": {"summary": "List nodes with pagination and filtering"}},
                    "/listen": {"get": {"summary": "Change feed"}},
                    "/events": {"get": {"summary": "SSE event stream"}},
                    "/challenge/{id}": {"post": {"summary": "Challenge edge"}},
                    "/support/{id}": {"post": {"summary": "Support edge"}},
                    "/observe/{id}": {"post": {"summary": "Record observation"}},
                    "/observations": {"get": {"summary": "List observations"}, "post": {"summary": "Bulk upload observations"}},
                    "/state": {"post": {"summary": "Update agent state"}},
                    "/register": {"post": {"summary": "Register agent"}},
                    "/heartbeat": {"post": {"summary": "Agent heartbeat"}},
                    "/webhook/{agent}": {"post": {"summary": "Register webhook"}},
                    "/search": {"get": {"summary": "ILIKE text search",
                                   "parameters": [{"name": "q", "in": "query",
                                                   "required": True,
                                                   "schema": {"type": "string"}}]}},
                    "/semantic_search": {"get": {"summary": "Semantic vector search (requires Ollama)",
                                            "parameters": [
                                                {"name": "q", "in": "query", "required": True,
                                                 "schema": {"type": "string"}},
                                                {"name": "type", "in": "query", "required": False,
                                                 "schema": {"type": "string"}},
                                                {"name": "limit", "in": "query", "required": False,
                                                 "schema": {"type": "integer"}},
                                                {"name": "min_confidence", "in": "query",
                                                 "required": False, "schema": {"type": "number"}},
                                            ],
                                            "responses": {
                                                "200": {"description": "Search results"},
                                                "503": {"description": "Ollama not available"},
                                            }}},
                    "/admin/checkpoint": {"post": {"summary": "Force CHECKPOINT",
                                           "responses": {"200": {"description": "WAL flushed to main DB"}}}},
                    "/graph/at": {"get": {"summary": "Graph at snapshot version",
                                      "responses": {"200": {"description": "Historical graph state"}}}},
                    "/graph/changes": {"get": {"summary": "Changes between snapshots",
                                           "responses": {"200": {"description": "Insertions/deletions"}}}},
                    "/voi/tasks": {"get": {"summary": "VoI task assignment for agent routing",
                                       "responses": {"200": {"description": "Research tasks ranked by VoI"}}}},
                },
            })
            return
        elif path == "/health":
            self._json_response(200, {
                "status": "ok",
                "uptime": round(time.time() - _START_TIME, 1),
            })
            return
        elif path == "/ready":
            try:
                self.store.execute("SELECT 1")
                self._json_response(200, {
                    "status": "ready",
                    "database": str(self.store.db_path),
                })
            except Exception:
                self._json_response(503, {
                    "status": "not_ready",
                    "database": str(self.store.db_path),
                })
            return
        elif path == "/metrics":
            with _metrics_lock:
                metrics_snapshot = dict(_metrics)
                sorted_lats = sorted(_request_latencies) if _request_latencies else [0]
            n = len(sorted_lats)
            uptime = round(time.time() - _START_TIME, 1)
            p50 = sorted_lats[n // 2] if n > 0 else 0
            p95 = sorted_lats[int(n * 0.95)] if n > 1 else sorted_lats[0] if n > 0 else 0
            p99 = sorted_lats[int(n * 0.99)] if n > 1 else sorted_lats[0] if n > 0 else 0
            lat_max = sorted_lats[-1] if n > 0 else 0

            # Prometheus text format when requested via Accept header or ?format=prometheus
            accept = self.headers.get("Accept", "")
            fmt = qs.get("format", [""])[0]
            if fmt == "prometheus" or "text/plain" in accept:
                lines = [
                    "# HELP ohm_uptime_seconds Seconds since daemon started",
                    "# TYPE ohm_uptime_seconds gauge",
                    f"ohm_uptime_seconds {uptime}",
                    "# HELP ohm_requests_total Total HTTP requests",
                    "# TYPE ohm_requests_total counter",
                    f'ohm_requests_total{{method="all"}} {metrics_snapshot.get("requests_total", 0)}',
                    f'ohm_requests_total{{method="get"}} {metrics_snapshot.get("requests_get", 0)}',
                    f'ohm_requests_total{{method="post"}} {metrics_snapshot.get("requests_post", 0)}',
                    "# HELP ohm_errors_total Total HTTP errors",
                    "# TYPE ohm_errors_total counter",
                    f'ohm_errors_total{{code="4xx"}} {metrics_snapshot.get("errors_4xx", 0)}',
                    f'ohm_errors_total{{code="5xx"}} {metrics_snapshot.get("errors_5xx", 0)}',
                    "# HELP ohm_rate_limited_total Requests rejected by rate limiter",
                    "# TYPE ohm_rate_limited_total counter",
                    f'ohm_rate_limited_total {metrics_snapshot.get("rate_limited", 0)}',
                    "# HELP ohm_request_duration_ms Request latency in milliseconds",
                    "# TYPE ohm_request_duration_ms summary",
                    f'ohm_request_duration_ms{{quantile="0.5"}} {p50}',
                    f'ohm_request_duration_ms{{quantile="0.95"}} {p95}',
                    f'ohm_request_duration_ms{{quantile="0.99"}} {p99}',
                    f'ohm_request_duration_ms_count {n}',
                    "",
                ]
                body_bytes = "\n".join(lines).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
            else:
                self._json_response(200, {
                    "uptime_seconds": uptime,
                    "requests": metrics_snapshot,
                    "latency_ms": {
                        "p50": p50,
                        "p95": p95,
                        "p99": p99,
                        "max": lat_max,
                        "sample_count": n,
                    },
                })
            return
        elif path == "/events" or path.startswith("/events/"):
            # SSE endpoint — streams change feed events to connected clients
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

        if path == "/stats":
            from ohm.queries import query_stats
            stats = query_stats(self.store.conn)
            stats["uptime"] = round(time.time() - _START_TIME, 1)
            self._json_response(200, stats)
            return

        if path == "/status":
            status = self.store.status()
            status["uptime"] = round(time.time() - _START_TIME, 1)
            status["version"] = "0.2.0"
            status["schema"] = self.schema_config.name
            status["quack"] = self.config.get("quack", False)
            self._json_response(200, status)
        elif path == "/schema":
            schema = self.schema_config
            # Flatten edge types: collect all unique edge type names across layers
            all_edge_types: set[str] = set()
            for types in schema.layer_edge_types.values():
                all_edge_types.update(types)
            self._json_response(200, {
                "schema": schema.name,
                "node_types": sorted(schema.node_types),
                "edge_types": sorted(all_edge_types),
                "edge_types_by_layer": {k: sorted(v) for k, v in schema.layer_edge_types.items()},
                "layers": schema.layer_descriptions,
            })
        elif path == "/layers":
            self._json_response(200, self.schema_config.layer_descriptions)
        elif path.startswith("/node/"):
            node_id = path[6:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            node = self.store.get_node(node_id)
            if node:
                self._json_response(200, node)
            else:
                raise NodeNotFoundError(f"Node {node_id} not found")
        elif path.startswith("/deep/"):
            # Deep content retrieval — follows node URL to full source
            node_id = path[6:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            try:
                result = self.store.deep_content(node_id)
                self._json_response(200, result)
            except NodeNotFoundError:
                raise
            except Exception as e:
                self._json_response(500, {"error": "deep_content_failed", "message": str(e)})
        elif path.startswith("/edge/"):
            edge_id = path[6:]
            from .validation import validate_identifier
            edge_id = validate_identifier(edge_id, name="edge_id")
            edge = self.store.get_edge(edge_id)
            if edge:
                self._json_response(200, edge)
            else:
                raise EdgeNotFoundError(f"Edge {edge_id} not found")
        elif path.startswith("/neighborhood/"):
            node_id = path[14:]  # strip "/neighborhood/"
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            depth = int(qs.get("depth", [3])[0])
            layer = qs.get("layer", [None])[0]
            from .queries import query_neighborhood
            results = query_neighborhood(self.store.conn, node_id, depth=depth, layer=layer)
            self._json_response(200, results)
        elif path.startswith("/path/"):
            parts = path[6:].split("/")
            if len(parts) >= 2:
                from .validation import validate_identifier
                from_node = validate_identifier(parts[0], name="from_node")
                to_node = validate_identifier(parts[1], name="to_node")
                from .queries import query_path
                results = query_path(self.store.conn, from_node, to_node)
                self._json_response(200, results)
            else:
                raise ValidationError("Path requires /path/from/to")
        elif path.startswith("/impact/"):
            node_id = path[8:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            depth = int(qs.get("depth", [5])[0])
            from .queries import query_impact
            results = query_impact(self.store.conn, node_id, depth=depth)
            self._json_response(200, results)
        elif path.startswith("/confidence/"):
            target_id = path[12:]
            from .validation import validate_identifier
            target_id = validate_identifier(target_id, name="target_id")
            from .queries import query_confidence

            # Check if target_id is a node or an edge
            is_node = self.store.conn.execute(
                "SELECT COUNT(*) FROM ohm_nodes WHERE id = ?", [target_id],
            ).fetchone()
            is_edge = self.store.conn.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE id = ?", [target_id],
            ).fetchone()

            if is_node and is_node[0] > 0:
                # Node: find all challenge/support/refine edges pointing TO this node
                refs_result = self.store.conn.execute(
                    """SELECT id, edge_type, confidence, condition, created_by, created_at,
                              from_node, to_node, layer
                       FROM ohm_edges
                       WHERE to_node = ?
                         AND edge_type IN ('CHALLENGED_BY', 'SUPPORTS', 'REFINES')
                       ORDER BY created_at DESC""",
                    [target_id],
                )
                ref_columns = [desc[0] for desc in refs_result.description]
                refs = [dict(zip(ref_columns, row)) for row in refs_result.fetchall()]

                challenges = [r for r in refs if r["edge_type"] == "CHALLENGED_BY"]
                supports = [r for r in refs if r["edge_type"] == "SUPPORTS"]
                refinements = [r for r in refs if r["edge_type"] == "REFINES"]

                self._json_response(200, {
                    "node_id": target_id,
                    "challenges": challenges,
                    "supports": supports,
                    "refinements": refinements,
                })
            elif is_edge and is_edge[0] > 0:
                # Edge: use existing query_confidence
                results = query_confidence(self.store.conn, target_id)
                self._json_response(200, results)
            else:
                raise NodeNotFoundError(f"Neither node nor edge found with id: {target_id}")
        elif path.startswith("/agent/"):
            agent_name = path[7:]
            from .validation import validate_identifier
            agent_name = validate_identifier(agent_name, name="agent_name")
            state = self.store.get_agent_state(agent_name)
            if state:
                self._json_response(200, state)
            else:
                self._json_response(404, {"error": f"Agent {agent_name} not found"})
        elif path == "/agents":
            results = self.store.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name")
            self._json_response(200, results)
        elif path == "/nodes":
            # List nodes with pagination and optional type/label filtering
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
            sql = (
                "SELECT * FROM ohm_nodes WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            results = self.store.execute(sql, params)
            # Also return total count for pagination
            count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
            count_params = params[:-2]  # Remove limit and offset
            total_result = self.store.execute(count_sql, count_params)
            total = total_result[0]["cnt"] if total_result else len(results)
            self._json_response(200, {
                "nodes": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        elif path == "/tasks":
            # List task nodes with status/assignment filtering
            # Tasks are nodes of type='task' with optional filters
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
            sql = (
                "SELECT * FROM ohm_nodes WHERE "
                + " AND ".join(conditions)
                + " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END, due_date ASC NULLS LAST, created_at DESC LIMIT ? OFFSET ?"
            )
            results = self.store.execute(sql, params)
            # Also return total count
            count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
            count_params = params[:-2]
            total_result = self.store.execute(count_sql, count_params)
            total = total_result[0]["cnt"] if total_result else len(results)
            self._json_response(200, {
                "tasks": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        elif path == "/listen":
            since = qs.get("since", [None])[0]
            agent_name = qs.get("agent", [agent or "ohm"])[0]
            enrich = qs.get("enrich", ["false"])[0].lower() == "true"
            if not since:
                state = self.store.get_agent_state(agent_name)
                if state and state.get("last_sync"):
                    since = state["last_sync"]
                    # last_sync is a TIMESTAMP column — DuckDB returns datetime, not string
                    if isinstance(since, datetime):
                        since = since.isoformat()
                else:
                    # Default to 24 hours ago (OHM-4oc)
                    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            from .queries import query_change_feed
            results = query_change_feed(
                self.store.conn, since=since, agent_name=agent_name, enrich=enrich
            )
            self._json_response(200, results)
        elif path == "/search":
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
            sql = (
                "SELECT * FROM ohm_nodes WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC LIMIT ?"
            )
            results = self.store.execute(sql, params)
            self._json_response(200, results)
        elif path == "/semantic_search":
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
                from .queries import semantic_search
                results = semantic_search(
                    self.store.conn,
                    query=query_text,
                    limit=limit,
                    node_type=node_type,
                    min_confidence=min_confidence,
                )
                self._json_response(200, {"results": results, "count": len(results)})
            except ValueError as e:
                # Ollama not available
                self._json_response(503, {
                    "error": "service_unavailable",
                    "message": str(e),
                })
        elif path == "/health/graph":
            from .queries import query_graph_health
            result = query_graph_health(self.store.conn)
            self._json_response(200, result)
        elif path == "/health/agents":
            from .methods import query_agent_health
            result = query_agent_health(self.store.conn)
            self._json_response(200, result)
        elif path == "/contradictions":
            from .methods import detect_contradictions
            conf_thresh = float(qs.get("confidence", [0.5])[0])
            result = detect_contradictions(self.store.conn, confidence_threshold=conf_thresh)
            self._json_response(200, result)
        elif path == "/anomalies":
            from .methods import detect_anomalies
            sigma = float(qs.get("sigma", [2.0])[0])
            layer = qs.get("layer", [None])[0]
            limit = int(qs.get("limit", [50])[0])
            result = detect_anomalies(self.store.conn, sigma_threshold=sigma, layer=layer, limit=limit)
            self._json_response(200, result)
        elif path.startswith("/aggregate/"):
            node_id = path[11:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            method = qs.get("method", ["weighted"])[0]
            from .methods import aggregate_observations
            result = aggregate_observations(self.store.conn, node_id, method=method)
            self._json_response(200, result)
        elif path.startswith("/provenance/"):
            node_id = path[12:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            max_depth = int(qs.get("depth", [10])[0])
            from .queries import query_provenance
            result = query_provenance(self.store.conn, node_id, max_depth=max_depth)
            self._json_response(200, result)
        elif path == "/stale":
            from .queries import query_stale_edges
            threshold = float(qs.get("threshold", [0.1])[0])
            result = query_stale_edges(self.store.conn, stale_threshold=threshold)
            self._json_response(200, result)
        elif path == "/decay":
            self._require_write_auth()
            from .queries import apply_confidence_decay
            threshold = float(qs.get("threshold", [0.1])[0])
            layer = qs.get("layer", [None])[0]
            dry_run = qs.get("dry_run", ["false"])[0].lower() == "true"
            result = apply_confidence_decay(
                self.store.conn,
                stale_threshold=threshold,
                layer=layer,
                dry_run=dry_run,
            )
            self._json_response(200, result)
        elif path.startswith("/monte-carlo/"):
            node_id = path[13:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            from .methods import monte_carlo_impact
            sims = int(qs.get("simulations", [1000])[0])
            depth = int(qs.get("depth", [3])[0])
            default_prob = float(qs.get("default_probability", [0.5])[0])
            seed_val = qs.get("seed", [None])[0]
            seed = int(seed_val) if seed_val is not None else None
            result = monte_carlo_impact(
                self.store.conn, node_id,
                simulations=sims, depth=depth,
                default_probability=default_prob,
                seed=seed,
            )
            self._json_response(200, result)
        elif path == "/duplicates":
            from .methods import detect_near_duplicates
            threshold = float(qs.get("similarity", [0.8])[0])
            result = detect_near_duplicates(self.store.conn, similarity_threshold=threshold)
            self._json_response(200, result)
        elif path.startswith("/calibration/"):
            agent_name = path[13:]
            from .validation import validate_identifier
            agent_name = validate_identifier(agent_name, name="agent_name")
            from .methods import compute_confidence_calibration
            result = compute_confidence_calibration(self.store.conn, agent_name)
            self._json_response(200, result)
        elif path == "/orphans":
            # Find nodes with zero edges — completely disconnected from the graph
            from .methods import find_orphans
            node_type = qs.get("type", [None])[0]
            exclude_system = qs.get("exclude_system", ["true"])[0].lower() == "true"
            limit = int(qs.get("limit", [50])[0])
            result = find_orphans(self.store.conn, node_type=node_type, exclude_system=exclude_system, limit=limit)
            self._json_response(200, result)
        elif path == "/hubs":
            # Find most-connected nodes — anchors of the graph
            from .methods import find_hubs
            node_type = qs.get("type", [None])[0]
            min_connections = int(qs.get("min_connections", [3])[0])
            limit = int(qs.get("limit", [20])[0])
            result = find_hubs(self.store.conn, node_type=node_type, min_connections=min_connections, limit=limit)
            self._json_response(200, result)
        elif path == "/dead_ends":
            # Find nodes with only incoming edges — sinks that don't lead anywhere
            from .methods import find_dead_ends
            node_type = qs.get("type", [None])[0]
            limit = int(qs.get("limit", [50])[0])
            result = find_dead_ends(self.store.conn, node_type=node_type, limit=limit)
            self._json_response(200, result)
        elif path == "/suggest":
            # Suggest connections between nodes that share context but aren't linked
            from .methods import suggest_connections
            method = qs.get("method", ["shared_provenance"])[0]
            min_shared = int(qs.get("min_shared", [2])[0])
            limit = int(qs.get("limit", [20])[0])
            result = suggest_connections(self.store.conn, method=method, min_shared=min_shared, limit=limit)
            self._json_response(200, result)
        elif path == "/graph/stats":
            # Extended graph statistics (orphans, hubs, density, etc.)
            from .methods import graph_stats
            result = graph_stats(self.store.conn)
            self._json_response(200, result)
        elif path == "/lint":
            # Contract layer linting: validate graph against naming conventions and required fields
            from .contract import ContractConfig, lint_graph
            node_type_filter = qs.get("node_types", [None])[0]
            node_types = node_type_filter.split(",") if node_type_filter else None
            limit = int(qs.get("limit", ["1000"])[0])
            contract = ContractConfig()
            result = lint_graph(self.store.conn, contract, limit=limit, node_types=node_types)
            self._json_response(200, result)
        elif path == "/contract":
            # Return the current contract configuration
            from .contract import ContractConfig
            contract = ContractConfig()
            self._json_response(200, contract.to_dict())
        elif path == "/inference":
            # Bayesian inference: compute posterior probabilities given evidence
            # Uses pgmpy Variable Elimination (optional dependency)
            target = qs.get("target", [None])[0]
            if not target:
                self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
                return
            from .validation import validate_identifier
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
            from .bayesian import bayesian_inference
            result = bayesian_inference(self.store.conn, target, evidence, edge_types=None, layers=layers, leak_probability=leak_probability)
            self._json_response(200, result)
        elif path == "/intervene":
            # Causal intervention using Pearl's do-operator (graph surgery)
            # Differs from /inference: severs incoming edges to target, sets value externally
            # This isolates direct causal effect by removing confounder influence
            target = qs.get("target", [None])[0]
            if not target:
                self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
                return
            from .validation import validate_identifier
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
            from .bayesian import causal_intervention
            result = causal_intervention(
                self.store.conn, target, intervention_state,
                query_nodes=query_nodes,
                layers=layers,
                leak_probability=leak_probability,
            )
            self._json_response(200, result)
        elif path == "/ate":
            # Average Treatment Effect: model-based ATE from noisy-OR CPDs
            # ATE = P(effect=bad|do(cause=bad)) - P(effect=bad|do(cause=good))
            cause = qs.get("cause", [None])[0]
            effect = qs.get("effect", [None])[0]
            if not cause or not effect:
                self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
                return
            from .validation import validate_identifier
            cause = validate_identifier(cause, name="cause")
            effect = validate_identifier(effect, name="effect")
            leak_probability = float(qs.get("leak", ["0.15"])[0])
            # Parse optional layers filter: ?layers=L3,L4
            layers_str = qs.get("layers", [""])[0]
            layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
            from .bayesian import compute_ate
            result = compute_ate(self.store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
            self._json_response(200, result)
        elif path == "/sensitivity":
            # Sensitivity analysis: E-value for causal robustness
            # "How much unmeasured confounding would overturn this conclusion?"
            cause = qs.get("cause", [None])[0]
            effect = qs.get("effect", [None])[0]
            if not cause or not effect:
                self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
                return
            from .validation import validate_identifier
            cause = validate_identifier(cause, name="cause")
            effect = validate_identifier(effect, name="effect")
            leak_probability = float(qs.get("leak", ["0.15"])[0])
            # Parse optional layers filter: ?layers=L3,L4
            layers_str = qs.get("layers", [""])[0]
            layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
            from .bayesian import compute_sensitivity
            result = compute_sensitivity(self.store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
            self._json_response(200, result)
        elif path == "/adjustment":
            # Find valid backdoor/frontdoor adjustment sets for causal identification
            # Uses pgmpy's CausalInference for formal identification
            cause = qs.get("cause", [None])[0]
            effect = qs.get("effect", [None])[0]
            if not cause or not effect:
                self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
                return
            from .validation import validate_identifier
            cause = validate_identifier(cause, name="cause")
            effect = validate_identifier(effect, name="effect")
            leak_probability = float(qs.get("leak", ["0.15"])[0])
            # Parse optional layers filter: ?layers=L3,L4
            layers_str = qs.get("layers", [""])[0]
            layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
            from .bayesian import find_adjustment_sets
            result = find_adjustment_sets(self.store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
            self._json_response(200, result)
        elif path == "/voi":
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
            from .bayesian import compute_voi
            result = compute_voi(
                self.store.conn,
                decision_nodes=decision_nodes,
                edge_types=edge_types,
                layers=layers,
                top=top,
                leak_probability=leak_probability,
                root_prior=root_prior,
                timeout=timeout,
            )
            self._json_response(200, result)
        elif path == "/markov/absorbing":
            # Markov absorbing-state risk (OHM-9bom)
            # ?start=<node_id>&edge_types=TRANSITIONS_TO,LEADS_TO
            start_node = qs.get("start", [None])[0]
            if not start_node:
                raise ValidationError("?start=<node_id> is required")
            edge_types_str = qs.get("edge_types", [""])[0]
            markov_edge_types = [e.strip() for e in edge_types_str.split(",") if e.strip()] or None
            from .markov import markov_absorbing_risk
            result = markov_absorbing_risk(
                self.store.conn,
                start_node,
                edge_types=markov_edge_types,
            )
            self._json_response(200, result)
        elif path == "/markov/expected_steps":
            # Markov expected steps to absorption (OHM-9bom)
            # ?start=<node_id>&target=<node_id>&edge_types=TRANSITIONS_TO
            start_node = qs.get("start", [None])[0]
            if not start_node:
                raise ValidationError("?start=<node_id> is required")
            target_state = qs.get("target", [None])[0]
            edge_types_str = qs.get("edge_types", [""])[0]
            markov_edge_types = [e.strip() for e in edge_types_str.split(",") if e.strip()] or None
            from .markov import markov_expected_steps
            result = markov_expected_steps(
                self.store.conn,
                start_node,
                target_state=target_state,
                edge_types=markov_edge_types,
            )
            self._json_response(200, result)
        elif path == "/voi/tasks":
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
            from .bayesian import generate_voi_tasks
            result = generate_voi_tasks(
                self.store.conn,
                agent=agent,
                decision_nodes=decision_nodes,
                layers=layers,
                top=top,
                leak_probability=leak_probability,
                root_prior=root_prior,
            )
            self._json_response(200, result)
        elif path == "/suggest_causes":
            # Suggest candidate CAUSES edges from existing non-causal relationships
            # Identifies DEPENDS_ON/APPLIES_TO/REFINES/INFLUENCES edges that might be causal
            min_confidence = float(qs.get("min_confidence", ["0.5"])[0])
            from .bayesian import suggest_causes
            result = suggest_causes(self.store.conn, min_confidence=min_confidence)
            self._json_response(200, result)
        elif path == "/deduplicate":
            # Remove duplicate edges (same from→to, type, layer), keeping most recent
            self._require_write_auth()
            layer = qs.get("layer", [None])[0]
            if layer:
                from .validation import validate_layer
                try:
                    validate_layer(layer)
                except ValueError as e:
                    raise ValidationError(str(e))
            removed = self.store.deduplicate_edges(layer=layer)
            self._json_response(200, {"removed": removed, "layer": layer})
        elif path == "/refute":
            # Causal refutation: test robustness of causal conclusions
            # Uses DoWhy refutation methods (requires dowhy package)
            cause = qs.get("cause", [None])[0]
            effect = qs.get("effect", [None])[0]
            if not cause or not effect:
                self._json_response(400, {"error": "missing_parameter", "message": "?cause=X\u0026effect=Y required"})
                return
            from .validation import validate_identifier
            cause = validate_identifier(cause, name="cause")
            effect = validate_identifier(effect, name="effect")
            n_samples = int(qs.get("n_samples", ["1000"])[0])
            seed = int(qs.get("seed", ["42"])[0])
            methods_str = qs.get("methods", [None])[0]
            refutation_methods = methods_str.split(",") if methods_str else None
            from .causal_refutation import refute_causal_effect
            result = refute_causal_effect(
                self.store.conn, cause, effect,
                n_samples=n_samples,
                seed=seed,
                refutation_methods=refutation_methods,
            )
            self._json_response(200, result)
        elif path == "/admin/checkpoint":
            # Force DuckDB CHECKPOINT to flush WAL to main DB file
            self._require_write_auth()
            try:
                self.store.conn.execute("CHECKPOINT")
                self._json_response(200, {"status": "ok", "message": "WAL flushed to main database"})
            except Exception as e:
                self._json_response(500, {"error": "checkpoint_failed", "message": str(e)})
        elif path == "/admin/embeddings":
            # Batch generate embeddings for nodes missing them (OHM-emb)
            # Processes in small batches with delays to avoid OOM/timeout crashes
            try:
                from .queries import update_node_embedding

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
                rows = self.store.execute(
                    "SELECT id, label FROM ohm_nodes WHERE embedding IS NULL AND deleted_at IS NULL"
                )
                if not rows:
                    self._json_response(200, {
                        "status": "ok",
                        "updated": 0,
                        "failed": 0,
                        "total": 0,
                        "message": "All nodes already have embeddings",
                    })
                    return

                updated = 0
                failed = 0
                processed = 0
                for row in rows:
                    # Stop after batch_size nodes — client can re-call for more
                    if processed >= batch_size:
                        break
                    try:
                        if update_node_embedding(self.store.conn, row["id"]):
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
                self._json_response(200, {
                    "status": "ok" if remaining == 0 else "partial",
                    "updated": updated,
                    "failed": failed,
                    "processed": processed,
                    "total": total_missing,
                    "remaining": remaining,
                    "message": f"Generated {updated} embeddings ({failed} failed). {remaining} remaining — re-call to continue.",
                })
            except Exception as e:
                self._json_response(500, {"error": "embedding_backfill_failed", "message": str(e)})
        elif path == "/admin/snapshots":
            # DuckLake time-travel: list available snapshots (OHM-kdk.3)
            snapshots = self.store.list_snapshots()
            self._json_response(200, {"snapshots": snapshots, "count": len(snapshots)})
        elif path == "/graph/at":
            # DuckLake time-travel: query graph at specific snapshot version (OHM-kdk.3)
            version = qs.get("version", [None])[0]
            if not version:
                raise ValidationError("?version=N is required for /graph/at")
            try:
                version_int = int(version)
            except ValueError:
                raise ValidationError("?version must be an integer snapshot ID")
            result = self.store.graph_at_version(version_int)
            self._json_response(200, result)
        elif path == "/graph/changes":
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
            result = self.store.graph_changes(from_int, to_int)
            self._json_response(200, result)
        elif path.startswith("/reliability/"):
            # Compute source reliability metrics from historical outcomes
            source_agent = path[13:]  # strip /reliability/
            from .validation import validate_identifier
            source_agent = validate_identifier(source_agent, name="source_agent")
            from .queries import query_source_reliability
            result = query_source_reliability(self.store.conn, source_agent)
            self._json_response(200, result)
        elif path == "/observations":
            # List observations with optional filtering
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
            sql = (
                "SELECT * FROM ohm_observations WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            results = self.store.execute(sql, params)
            # Count query
            count_sql = "SELECT COUNT(*) as cnt FROM ohm_observations WHERE " + " AND ".join(conditions)
            count_params = params[:-2]
            total_result = self.store.execute(count_sql, count_params)
            total = total_result[0]["cnt"] if total_result else len(results)
            self._json_response(200, {"observations": results, "total": total, "limit": limit, "offset": offset})
        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})

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

        if path == "/node":
            # Support ?create_only=true to reject updates (upsert is the default — OHM-y2i.20)
            create_only = qs.get("create_only", ["false"])[0].lower() in ("true", "1", "yes")
            if create_only:
                existing = self.store.conn.execute(
                    "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [body["id"]],
                ).fetchone()
                if existing:
                    self._json_response(409, {
                        "error": "conflict",
                        "message": f"Node {body['id']} already exists. Use ?create_only=false for upsert.",
                    })
                    return

            result = self.store.write_node(
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
                agent_name=agent,
            )
            event_type = "node.created" if result.get("created") else "node.updated"
            _trigger_webhooks({
                "type": event_type,
                "agent": agent,
                "node": result,
            })
            if result.get("created", True):
                self._json_response(201, result)
            else:
                self._json_response(200, result)

        elif path == "/node/find_or_create":
            # Find existing node by label+type, or create new one
            from .queries import find_or_create_node
            node = find_or_create_node(
                self.store.conn,
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

        elif path == "/edge":
            result = self.store.write_edge(
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
            _trigger_webhooks({
                "type": "edge.created",
                "agent": agent,
                "edge": result,
            })
            self._json_response(201, result)

        elif path.startswith("/challenge/"):
            edge_id = path[11:]
            from .validation import validate_identifier
            edge_id = validate_identifier(edge_id, name="edge_id")
            reason = body.get("reason", "")
            confidence = body.get("confidence", 0.5)
            challenge_type = body.get("challenge_type", "CHALLENGED_BY")
            result = self.store.challenge_edge(edge_id, reason, confidence, challenge_type, agent_name=agent)
            if result:
                _trigger_webhooks({
                    "type": "edge.challenged",
                    "agent": agent,
                    "edge": result,
                    "challenge_type": challenge_type,
                })
                self._json_response(201, result)
            else:
                raise EdgeNotFoundError(f"Edge {edge_id} not found")

        elif path.startswith("/support/"):
            edge_id = path[9:]
            from .validation import validate_identifier
            edge_id = validate_identifier(edge_id, name="edge_id")
            reason = body.get("reason", "")
            confidence = body.get("confidence", 0.8)
            result = self.store.challenge_edge(edge_id, reason, confidence, "SUPPORTS", agent_name=agent)
            if result:
                _trigger_webhooks({
                    "type": "edge.supported",
                    "agent": agent,
                    "edge": result,
                })
                self._json_response(201, result)
            else:
                raise EdgeNotFoundError(f"Edge {edge_id} not found")

        elif path.startswith("/observe/"):
            node_id = path[9:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")
            result = self.store.write_observation(
                node_id=node_id,
                type=body.get("type", "measurement"),
                value=body.get("value"),
                baseline=body.get("baseline"),
                sigma=body.get("sigma"),
                source=body.get("source"),
                notes=body.get("notes"),
                source_name=body.get("source_name"),
                source_url=body.get("source_url"),
                agent_name=agent,
            )
            _trigger_webhooks({
                "type": "observation.created",
                "agent": agent,
                "observation": result,
            })
            self._json_response(201, result)

        elif path == "/observations":
            # OHM-0lf: Bulk observation upload.
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
                from .validation import validate_identifier
                try:
                    node_id = validate_identifier(node_id, name="node_id")
                except ValueError as e:
                    errors.append({"index": i, "error": str(e)})
                    continue
                try:
                    result = self.store.write_observation(
                        node_id=node_id,
                        type=obs.get("obs_type", obs.get("type", "measurement")),
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

            self._json_response(201, {
                "created": len(results),
                "errors": errors,
                "observations": results,
            })

        elif path == "/outcome":
            # Record whether a source agent's claim was correct
            source_agent = body.get("source_agent")
            claim_node = body.get("claim_node")
            outcome = body.get("outcome")
            notes = body.get("notes")
            if not source_agent or not claim_node or outcome is None:
                raise ValidationError("outcome requires source_agent, claim_node, and outcome fields")
            from .queries import query_record_outcome
            result = query_record_outcome(
                self.store.conn,
                source_agent=source_agent,
                claim_node=claim_node,
                outcome=bool(outcome),
                recorded_by=agent,
                notes=notes,
            )
            self._json_response(201, result)

        elif path == "/batch":
            # Batch node and edge creation — all-or-nothing transaction
            nodes = body.get("nodes", [])
            edges = body.get("edges", [])
            errors = []
            nodes_created = 0
            edges_created = 0

            if len(nodes) + len(edges) > MAX_BATCH_SIZE:
                raise ValidationError(
                    f"Batch too large: {len(nodes)} nodes + {len(edges)} edges = "
                    f"{len(nodes) + len(edges)} items exceeds limit of {MAX_BATCH_SIZE}"
                )

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
                self.store.conn.execute("BEGIN TRANSACTION")
                for node in nodes:
                    self.store.write_node(
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
                        agent_name=agent,
                    )
                    nodes_created += 1
                for edge in edges:
                    self.store.write_edge(
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
                self.store.conn.execute("COMMIT")
            except Exception:
                self.store.conn.execute("ROLLBACK")
                raise

            self._json_response(201, {
                "nodes_created": nodes_created,
                "edges_created": edges_created,
                "errors": errors,
            })

        elif path == "/webhook":
            # Register or update webhook callback URL for this agent
            url = body.get("url", "")
            events = body.get("events", ["node.created", "node.updated", "edge.created"])
            if not url:
                raise ValidationError("Webhook requires a 'url' field")
            _validate_webhook_url(url)
            with _webhook_lock:
                _webhook_registry[agent] = {"url": url, "events": events}
            self._json_response(200, {
                "status": "registered",
                "agent": agent,
                "url": url,
                "events": events,
            })

        elif path == "/state":
            result = self.store.update_agent_state(
                current_focus=body.get("focus"),
                active_patterns=body.get("patterns"),
                available_services=body.get("services"),
                session_id=body.get("session_id"),
                agent_name=agent,
            )
            self._json_response(200, result)

        elif path == "/register":
            # Agent registration — idempotent: creates or updates agent node + edges.
            # If an agent with the same name already exists, reuses its node and
            # refreshes its edges (deletes old, creates new).
            from .queries import create_edge, find_or_create_node

            agent_label = body.get("name", agent)
            # Use deterministic ID for agent nodes to prevent duplicates
            import re
            agent_id = "agent_" + re.sub(r'[^a-zA-Z0-9]+', '_', agent_label.lower()).strip('_')

            # Check if agent node already exists (including soft-deleted)
            existing_active = self.store.conn.execute(
                "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id]
            ).fetchone()
            existing_soft_deleted = self.store.conn.execute(
                "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [agent_id]
            ).fetchone()

            if existing_active:
                # Update existing agent node (description may have changed)
                self.store.conn.execute(
                    "UPDATE ohm_nodes SET content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                    [body.get("description"), agent, agent_id],
                )
                me = self.store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
                # Soft-delete old registration edges
                reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
                placeholders = ",".join(["?"] * len(reg_edge_types))
                self.store.conn.execute(
                    f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                    [agent_id] + list(reg_edge_types),
                )
            elif existing_soft_deleted:
                # Reactivate soft-deleted agent node
                self.store.conn.execute(
                    """UPDATE ohm_nodes SET
                        content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ?,
                        deleted_at = NULL
                    WHERE id = ?""",
                    [body.get("description"), agent, agent_id],
                )
                me = self.store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
                # Soft-delete old registration edges
                reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
                placeholders = ",".join(["?"] * len(reg_edge_types))
                self.store.conn.execute(
                    f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                    [agent_id] + list(reg_edge_types),
                )
            else:
                # Create new agent node with deterministic ID
                self.store.conn.execute(
                    """INSERT INTO ohm_nodes
                       (id, label, type, content, created_by, confidence, visibility, created_at, updated_at)
                       VALUES (?, ?, 'agent', ?, ?, 1.0, 'team', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    [agent_id, agent_label, body.get("description"), agent],
                )
                me = self.store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]

            created_edges = []
            for v in body.get("values", []):
                value_node = find_or_create_node(
                    self.store.conn, label=v, node_type="value", created_by=agent,
                )
                edge = create_edge(
                    self.store.conn, from_node=agent_id, to_node=value_node["id"],
                    edge_type="VALUES", layer="L1", created_by=agent, confidence=1.0,
                    provenance="self_declaration",
                )
                created_edges.append(edge)

            for g in body.get("goals", []):
                goal_node = find_or_create_node(
                    self.store.conn, label=g, node_type="goal", created_by=agent,
                )
                edge = create_edge(
                    self.store.conn, from_node=agent_id, to_node=goal_node["id"],
                    edge_type="GOALS", layer="L1", created_by=agent, confidence=1.0,
                    provenance="self_declaration",
                )
                created_edges.append(edge)

            for c in body.get("capabilities", []):
                cap_node = find_or_create_node(
                    self.store.conn, label=c, node_type="skill", created_by=agent,
                )
                edge = create_edge(
                    self.store.conn, from_node=agent_id, to_node=cap_node["id"],
                    edge_type="CAPABLE_OF", layer="L1", created_by=agent, confidence=1.0,
                    provenance="self_declaration",
                )
                created_edges.append(edge)

            for i in body.get("interests", []):
                topic_node = find_or_create_node(
                    self.store.conn, label=i, node_type="topic", created_by=agent,
                )
                edge = create_edge(
                    self.store.conn, from_node=agent_id, to_node=topic_node["id"],
                    edge_type="INTERESTED_IN", layer="L1", created_by=agent, confidence=1.0,
                    provenance="self_declaration",
                )
                created_edges.append(edge)

            for a in body.get("listens_to", []):
                other = find_or_create_node(
                    self.store.conn, label=a, node_type="agent", created_by=agent,
                )
                edge = create_edge(
                    self.store.conn, from_node=agent_id, to_node=other["id"],
                    edge_type="LISTENS_TO", layer="L3", created_by=agent, confidence=0.7,
                    provenance="self_declaration",
                )
                created_edges.append(edge)

            self._json_response(201, {
                "agent": me,
                "edges_created": len(created_edges),
            })

        elif path == "/heartbeat":
            from .methods import agent_heartbeat
            result = agent_heartbeat(
                self.store.conn, agent,
                focus=body.get("focus"),
            )
            # Also sync with DuckLake if configured
            sync_result = self.store.sync_heartbeat()
            result["ducklake_sync"] = sync_result
            self._json_response(200, result)

        elif path == "/deduplicate":
            # Remove duplicate edges (same from→to, type, layer), keeping most recent
            layer = qs.get("layer", [None])[0]
            if layer:
                from .validation import validate_layer
                try:
                    validate_layer(layer)
                except ValueError as e:
                    raise ValidationError(str(e))
            removed = self.store.deduplicate_edges(layer=layer)
            self._json_response(200, {"removed": removed, "layer": layer})

        elif path == "/admin/checkpoint":
            # Force DuckDB CHECKPOINT to flush WAL to main DB file
            try:
                self.store.conn.execute("CHECKPOINT")
                self._json_response(200, {"status": "ok", "message": "WAL flushed to main database"})
            except Exception as e:
                self._json_response(500, {"error": "checkpoint_failed", "message": str(e)})

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
            if self.no_auth or not self.tokens:
                agent = "ohm"
            else:
                raise AuthenticationError("Authentication required — provide Bearer token")

        if path.startswith("/node/"):
            node_id = path[6:]
            from .validation import validate_identifier
            node_id = validate_identifier(node_id, name="node_id")

            # Verify node exists (idempotent 404)
            node = self.store.conn.execute(
                "SELECT id, created_by FROM ohm_nodes WHERE id = ?", [node_id],
            ).fetchone()
            if not node:
                raise NodeNotFoundError(f"Node not found: {node_id}")

            # Only allow deletion of own nodes (unless no_auth mode)
            if not self.no_auth and node[1] != agent:
                raise PermissionDeniedError(
                    f"Cannot delete node {node_id}: owned by {node[1]}, you are {agent}"
                )

            # Use store method — splits edge deletion to avoid DuckDB index issues (OHM-cpi)
            result = self.store.delete_node(node_id, deleted_by=agent)
            self._json_response(200, result)

        elif path.startswith("/edge/"):
            edge_id = path[6:]
            from .validation import validate_identifier
            edge_id = validate_identifier(edge_id, name="edge_id")

            # Verify edge exists (idempotent 404)
            edge = self.store.conn.execute(
                "SELECT id, created_by FROM ohm_edges WHERE id = ?", [edge_id],
            ).fetchone()
            if not edge:
                raise EdgeNotFoundError(f"Edge not found: {edge_id}")

            # Only allow deletion of own edges (unless no_auth mode)
            if not self.no_auth and edge[1] != agent:
                raise PermissionDeniedError(
                    f"Cannot delete edge {edge_id}: owned by {edge[1]}, you are {agent}"
                )

            # Use store method
            result = self.store.delete_edge(edge_id, deleted_by=agent)
            self._json_response(200, result)

        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})


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
    OhmHandler.roles = config_roles if config_roles else config.get("roles", {})
    OhmHandler.no_auth = config.get("no_auth", False)
    OhmHandler.require_read_auth = config.get("require_read_auth", False)
    OhmHandler.schema_config = schema_config

    # ── Quack integration ──────────────────────────────────────────────
    quack_info: dict[str, Any] | None = None
    if config.get("quack", False):
        from .quack import is_available, start_server

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
        while not _sync_stop.wait(ducklake_sync_interval):
            try:
                store.sync_heartbeat()
            except Exception:
                pass

    if hasattr(store, "sync_heartbeat") and ducklake_sync_interval > 0:
        _sync_thread = threading.Thread(target=_ducklake_sync_loop, daemon=True, name="ducklake-sync")
        _sync_thread.start()
    print(f"Schema: {schema_config.name}", file=sys.stderr)
    if quack_info:
        print("Concurrent access: Quack (multi-writer)", file=sys.stderr)
    else:
        print("Concurrent access: HTTP (single-writer)", file=sys.stderr)

    # Graceful shutdown — CHECKPOINT before exit (OHM-8n9)
    def shutdown_handler(signum, frame):
        print("Shutting down...", file=sys.stderr)
        _sync_stop.set()
        try:
            store.sync_heartbeat()
        except Exception:
            pass
        try:
            store.conn.execute("CHECKPOINT")
        except Exception:
            pass
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
    parser.add_argument("--no-auth", action="store_true", help="Disable authentication (dev mode)")
    parser.add_argument(
        "--require-read-auth", action="store_true",
        help="Require authentication for read endpoints (default: public reads)",
    )
    parser.add_argument(
        "--schema", choices=["ohm", "topo"], default=None,
        help="Schema configuration (default: determined by entry point)",
    )
    parser.add_argument(
        "--quack", action="store_true",
        help="Enable Quack protocol for concurrent multi-writer access",
    )
    parser.add_argument(
        "--quack-uri", default=None,
        help="Quack server URI (default: quack:localhost)",
    )
    parser.add_argument(
        "--quack-token-env", default=None,
        help="Environment variable for Quack token (default: QUACK_TOKEN)",
    )
    parser.add_argument(
        "--check-migrations", action="store_true",
        help="Report pending schema migrations without applying them, then exit",
    )
    args = parser.parse_args()

    # Allow CLI override of schema
    if args.schema == "topo":
        from .schema import TOPO_SCHEMA
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

    # Migration dry-run: report pending migrations and exit without applying
    if args.check_migrations:
        from .schema import MIGRATIONS
        import duckdb as _duckdb

        db_path_check = config.get("db_path", str(Path.home() / ".ohm" / "ohm.duckdb"))
        try:
            _check_conn = _duckdb.connect(db_path_check, read_only=True)
            current = _check_conn.execute(
                "SELECT value FROM ohm_meta WHERE key = 'schema_version'"
            ).fetchone()
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
        from .db import attach_ducklake
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

    Same as ohmd but defaults to the TOPO schema configuration.
    """
    from .schema import TOPO_SCHEMA
    main(schema_config=TOPO_SCHEMA)


if __name__ == "__main__":
    main()
