"""
OHM Daemon — HTTP server for multi-agent shared access to the knowledge graph.

Uses Quack (DuckDB client-server protocol) for concurrent access with
token-based authentication and per-agent role enforcement.
"""

import argparse
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from .exceptions import (
    AuthenticationError,
    EdgeNotFoundError,
    NodeNotFoundError,
    OHMError,
    PermissionDeniedError,
    ValidationError,
)
from .schema import EDGE_TYPES, LAYER_DESCRIPTIONS, NODE_TYPES
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
}

_START_TIME = time.time()


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
    if isinstance(exc, ValidationError):
        return 400, "validation_error"
    if isinstance(exc, OHMError):
        return 500, "internal_error"
    return 500, "internal_error"


# ── HTTP Handler ───────────────────────────────────────────

class OhmHandler(BaseHTTPRequestHandler):
    """HTTP request handler for OHM daemon."""

    store: Optional[OhmStore] = None
    config: dict = {}
    tokens: dict = {}  # token -> agent_name
    roles: dict = {}    # agent_name -> role (read-write, read-only)
    no_auth: bool = False  # --no-auth flag: bypass all auth (dev mode)

    def log_message(self, format, *args):
        """Structured request logging with correlation ID."""
        corr_id = getattr(self, "_correlation_id", "-")
        timestamp = datetime.now(timezone.utc).isoformat()
        sys.stderr.write(
            f"[{timestamp}] [{corr_id}] {format % args}\n"
        )
        sys.stderr.flush()

    def _authenticate(self) -> Optional[str]:
        """Validate bearer token, return agent name or None."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if token in self.tokens:
                return self.tokens[token]
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        if "token" in qs:
            token = qs["token"][0]
            if token in self.tokens:
                return self.tokens[token]
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

    def _json_response(self, code: int, data):
        """Send a JSON response."""
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _read_body(self):
        """Read and parse JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body)

    def do_GET(self):
        """Handle GET requests with error mapping and correlation IDs."""
        self._correlation_id = str(uuid.uuid4())
        start = time.time()
        try:
            self._do_GET()
        except OHMError as e:
            self._error_response(e)
        except Exception as e:
            self._error_response(OHMError(str(e)))
        finally:
            elapsed = (time.time() - start) * 1000
            code = getattr(self, "_response_code", 0)
            self.log_message(
                "GET %s → %s (%.1fms)", self.path, code, elapsed,
            )

    def do_POST(self):
        """Handle POST requests with error mapping and correlation IDs."""
        self._correlation_id = str(uuid.uuid4())
        start = time.time()
        try:
            self._do_POST()
        except OHMError as e:
            self._error_response(e)
        except Exception as e:
            self._error_response(OHMError(str(e)))
        finally:
            elapsed = (time.time() - start) * 1000
            code = getattr(self, "_response_code", 0)
            self.log_message(
                "POST %s → %s (%.1fms)", self.path, code, elapsed,
            )

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
        if path == "/health":
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

        # Auth for all other GET endpoints
        agent = self._authenticate()
        if agent is None:
            if self.no_auth or not self.tokens:
                agent = "ohm"
            else:
                raise AuthenticationError("Authentication required — provide Bearer token")

        if path == "/status":
            status = self.store.status()
            status["uptime"] = round(time.time() - _START_TIME, 1)
            status["version"] = "0.2.0"
            self._json_response(200, status)
        elif path == "/schema":
            self._json_response(200, {
                "node_types": NODE_TYPES,
                "edge_types": EDGE_TYPES,
                "layers": LAYER_DESCRIPTIONS,
            })
        elif path == "/layers":
            self._json_response(200, LAYER_DESCRIPTIONS)
        elif path.startswith("/node/"):
            node_id = path[6:]
            node = self.store.get_node(node_id)
            if node:
                self._json_response(200, node)
            else:
                raise NodeNotFoundError(f"Node {node_id} not found")
        elif path.startswith("/edge/"):
            edge_id = path[6:]
            edge = self.store.get_edge(edge_id)
            if edge:
                self._json_response(200, edge)
            else:
                raise EdgeNotFoundError(f"Edge {edge_id} not found")
        elif path.startswith("/neighborhood/"):
            node_id = path[14:]
            depth = int(qs.get("depth", [3])[0])
            layer = qs.get("layer", [None])[0]
            from .graph import build_neighborhood_query
            sql, params = build_neighborhood_query(node_id, depth, layer)
            results = self.store.execute(sql, params)
            self._json_response(200, results)
        elif path.startswith("/path/"):
            parts = path[6:].split("/")
            if len(parts) >= 2:
                from .graph import build_path_query
                sql, params = build_path_query(parts[0], parts[1])
                results = self.store.execute(sql, params)
                self._json_response(200, results)
            else:
                raise ValidationError("Path requires /path/from/to")
        elif path.startswith("/impact/"):
            node_id = path[8:]
            depth = int(qs.get("depth", [5])[0])
            from .graph import build_impact_query
            sql, params = build_impact_query(node_id, depth)
            results = self.store.execute(sql, params)
            self._json_response(200, results)
        elif path.startswith("/confidence/"):
            edge_id = path[12:]
            from .graph import build_confidence_audit_query
            sql, params = build_confidence_audit_query(edge_id)
            results = self.store.execute(sql, params)
            self._json_response(200, results)
        elif path.startswith("/agent/"):
            agent_name = path[7:]
            state = self.store.get_agent_state(agent_name)
            if state:
                self._json_response(200, state)
            else:
                self._json_response(404, {"error": f"Agent {agent_name} not found"})
        elif path == "/agents":
            results = self.store.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name")
            self._json_response(200, results)
        elif path == "/listen":
            since = qs.get("since", [None])[0]
            agent_name = qs.get("agent", [agent or "ohm"])[0]
            if not since:
                state = self.store.get_agent_state(agent_name)
                if state and state.get("last_sync"):
                    since = state["last_sync"]
                else:
                    raise ValidationError("No last-check timestamp. Use ?since=ISO_TIMESTAMP")
            from .graph import build_change_feed_query
            sql, params = build_change_feed_query(since, agent_name=agent_name)
            results = self.store.execute(sql, params)
            self._json_response(200, results)
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

        from urllib.parse import urlparse
        path = urlparse(self.path).path.rstrip("/")
        body = self._read_body()

        if path == "/node":
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
            )
            self._json_response(201, result)

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
            )
            self._json_response(201, result)

        elif path.startswith("/challenge/"):
            edge_id = path[11:]
            reason = body.get("reason", "")
            confidence = body.get("confidence", 0.5)
            challenge_type = body.get("challenge_type", "CHALLENGED_BY")
            result = self.store.challenge_edge(edge_id, reason, confidence, challenge_type)
            if result:
                self._json_response(201, result)
            else:
                raise EdgeNotFoundError(f"Edge {edge_id} not found")

        elif path.startswith("/support/"):
            edge_id = path[9:]
            reason = body.get("reason", "")
            confidence = body.get("confidence", 0.8)
            result = self.store.challenge_edge(edge_id, reason, confidence, "SUPPORTS")
            if result:
                self._json_response(201, result)
            else:
                raise EdgeNotFoundError(f"Edge {edge_id} not found")

        elif path.startswith("/observe/"):
            node_id = path[9:]
            result = self.store.write_observation(
                node_id=node_id,
                type=body.get("type", "measurement"),
                value=body.get("value"),
                baseline=body.get("baseline"),
                sigma=body.get("sigma"),
                source=body.get("source"),
            )
            self._json_response(201, result)

        elif path == "/state":
            result = self.store.update_agent_state(
                current_focus=body.get("focus"),
                active_patterns=body.get("patterns"),
                available_services=body.get("services"),
                session_id=body.get("session_id"),
            )
            self._json_response(200, result)

        else:
            self._json_response(404, {"error": f"Unknown endpoint: {path}"})


def run_server(config: dict, store: OhmStore):
    """Run the HTTP server."""
    OhmHandler.store = store
    OhmHandler.config = config
    OhmHandler.tokens = config.get("tokens", {})
    OhmHandler.roles = config.get("roles", {})
    OhmHandler.no_auth = config.get("no_auth", False)

    server = HTTPServer((config["host"], config["port"]), OhmHandler)
    print(f"OHM daemon listening on {config['host']}:{config['port']}", file=sys.stderr)

    # Graceful shutdown
    def shutdown_handler(signum, frame):
        print("Shutting down...", file=sys.stderr)
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    server.serve_forever()
    store.close()


def main():
    """CLI entry point for ohmd."""
    parser = argparse.ArgumentParser(description="OHM daemon — multi-agent knowledge graph server")
    parser.add_argument("--host", default=None, help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port (default: 8710)")
    parser.add_argument("--db", default=None, help="Path to DuckDB file")
    parser.add_argument("--config", default=None, help="Path to config file")
    parser.add_argument("--init-token", default=None, help="Create a token for an agent (agent_name)")
    parser.add_argument("--no-auth", action="store_true", help="Disable authentication (dev mode)")
    args = parser.parse_args()

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

    # Handle token generation
    if args.init_token:
        import secrets
        token = secrets.token_urlsafe(32)
        config.setdefault("tokens", {})[args.init_token] = token
        config_path = Path(os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json")))
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Token for {args.init_token}: {token}")
        print(f"Config saved to {config_path}")
        return

    # Initialize store
    store = OhmStore(db_path=config["db_path"], agent_name="ohmd")
    print(f"OHM database: {config['db_path']}", file=sys.stderr)
    print(f"Status: {store.status()}", file=sys.stderr)

    # Run server
    try:
        run_server(config, store)
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
    finally:
        store.close()


if __name__ == "__main__":
    main()