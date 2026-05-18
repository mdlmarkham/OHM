"""OHMClient — pip-installable HTTP client for OHM daemon.

Auto-discovers agent tokens from ohm-config.json and provides
a simplified interface for agents to connect to the shared graph.

Usage:
    from ohm.client import OHMClient

    g = OHMClient(actor="metis")
    node = g.create_node("Research finding", node_type="concept")
    g.create_edge(from_node=node["id"], to_node=target, edge_type="CAUSES", layer="L3")
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Config search paths (in priority order)
_CONFIG_PATHS = [
    "/root/olympus/shared/ohm-config.json",
    "/etc/ohm/ohmd.json",
    "~/.ohm/ohm-config.json",
    "./ohm-config.json",
]


def _find_config() -> dict[str, Any] | None:
    """Find and load the OHM configuration file.

    Searches standard locations in priority order.
    Returns the parsed config dict, or None if no config found.
    """
    for path_str in _CONFIG_PATHS:
        path = Path(path_str).expanduser()
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _resolve_token(actor: str, config: dict[str, Any] | None) -> str | None:
    """Resolve the Bearer token for an agent.

    Checks in order:
    1. OHM_TOKEN environment variable
    2. config["tokens"][actor] from ohm-config.json
    3. config["tokens"]["*"] wildcard token

    Args:
        actor: Agent name.
        config: Parsed config dict, or None.

    Returns:
        Bearer token string, or None if not found.
    """
    # Environment variable takes precedence
    env_token = os.environ.get("OHM_TOKEN")
    if env_token:
        return env_token

    if not config:
        return None

    tokens = config.get("tokens", {})
    if isinstance(tokens, dict):
        # Exact match
        if actor in tokens:
            return tokens[actor]
        # Wildcard fallback
        if "*" in tokens:
            return tokens["*"]

    return None


def _resolve_base_url(config: dict[str, Any] | None) -> str:
    """Resolve the daemon base URL.

    Checks in order:
    1. OHM_BASE_URL environment variable
    2. config["base_url"] or config["host"] + config["port"]
    3. Default: http://127.0.0.1:8710

    Args:
        config: Parsed config dict, or None.

    Returns:
        Base URL string.
    """
    env_url = os.environ.get("OHM_BASE_URL")
    if env_url:
        return env_url

    if config:
        if "base_url" in config:
            return config["base_url"]
        host = config.get("host", "127.0.0.1")
        port = config.get("port", 8710)
        return f"http://{host}:{port}"

    return "http://127.0.0.1:8710"


class OHMClient:
    """HTTP client for the OHM daemon with automatic token resolution.

    Wraps connect_http() with config auto-discovery so agents don't
    need to manually manage tokens or URLs.

    Example:
        g = OHMClient(actor="metis")
        stats = g.stats()
        node = g.create_node("Pattern detected", node_type="pattern")
    """

    def __init__(
        self,
        actor: str = "unknown",
        *,
        base_url: str | None = None,
        token: str | None = None,
        config_path: str | None = None,
    ):
        """Initialize an OHMClient.

        Args:
            actor: Agent name for attribution and token lookup.
            base_url: Daemon URL. Auto-discovered if not provided.
            token: Bearer token. Auto-resolved from env/config if not provided.
            config_path: Explicit path to config file. Auto-discovered if not provided.
        """
        self.actor = actor

        # Load config
        if config_path:
            try:
                with open(config_path) as f:
                    self._config = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._config = None
        else:
            self._config = _find_config()

        # Resolve connection parameters
        self.base_url = base_url or _resolve_base_url(self._config)
        self.token = token or _resolve_token(actor, self._config)

        # Lazy-initialized graph connection
        self._graph = None

    @property
    def graph(self):
        """Get or create the underlying Graph connection."""
        if self._graph is None:
            from ohm.sdk import connect_http

            self._graph = connect_http(
                base_url=self.base_url,
                actor=self.actor,
                token=self.token,
            )
        return self._graph

    def create_node(self, label: str, *, node_type: str = "concept", **kwargs) -> dict[str, Any]:
        """Create a node in the shared graph."""
        return self.graph.create_node(label, node_type=node_type, **kwargs)

    def create_edge(self, *, from_node: str, to_node: str, edge_type: str,
                    layer: str = "L3", **kwargs) -> dict[str, Any]:
        """Create an edge in the shared graph."""
        return self.graph.create_edge(
            from_node=from_node, to_node=to_node,
            edge_type=edge_type, layer=layer, **kwargs,
        )

    def stats(self) -> dict[str, Any]:
        """Get graph statistics."""
        return self.graph.stats()

    def listen(self, *, since: str | None = None, **kwargs) -> list[dict[str, Any]]:
        """Get the change feed."""
        return self.graph.listen(since=since, **kwargs)

    def query(self, **kwargs) -> list[dict[str, Any]]:
        """Query the graph."""
        return self.graph.query(**kwargs)

    def register(
        self,
        *,
        description: str | None = None,
        values: list[str] | None = None,
        goals: list[str] | None = None,
        capabilities: list[str] | None = None,
        interests: list[str] | None = None,
        listens_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register this agent in the shared graph (idempotent).

        Maps description to the API's content field. All parameters are
        keyword-only to match the /register API endpoint.

        Args:
            description: Agent description (stored as node content).
            values: What this agent optimizes for.
            goals: What this agent is trying to achieve.
            capabilities: What this agent can do.
            interests: Topics this agent subscribes to.
            listens_to: Other agents whose output this agent follows.

        Returns:
            The agent node record with created edges.
        """
        body: dict[str, Any] = {"name": self.actor}
        if description is not None:
            body["description"] = description
        if values is not None:
            body["values"] = values
        if goals is not None:
            body["goals"] = goals
        if capabilities is not None:
            body["capabilities"] = capabilities
        if interests is not None:
            body["interests"] = interests
        if listens_to is not None:
            body["listens_to"] = listens_to
        return self.graph._http_request("POST", "/register", body)

    def close(self):
        """Close the underlying connection."""
        if self._graph is not None:
            self._graph.close()
            self._graph = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self):
        return f"OHMClient(actor={self.actor!r}, base_url={self.base_url!r})"
