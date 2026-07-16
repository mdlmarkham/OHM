"""OHM Python SDK — programmatic API for agents.

Provides a clean Python interface for agents to interact with the
knowledge graph without calling the CLI or writing raw SQL.

Usage:
    import ohm.sdk as ohm

    with ohm.connect(":memory:", actor="agent-alpha") as graph:
        a = graph.create_node(label="Pattern A")
        b = graph.create_node(label="Pattern B")
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")

        results = graph.neighborhood(a, depth=2)
        stats = graph.stats()
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.framework.graph_mixins.data_products import DataProductsGraphMixin
from ohm.framework.graph_mixins.change_feed import ChangeFeedGraphMixin
from ohm.framework.graph_mixins.substrate_computation import SubstrateComputationGraphMixin
from ohm.framework.graph_mixins.discovery_export import DiscoveryExportGraphMixin
from ohm.framework.graph_mixins.edge_versioning import EdgeVersioningGraphMixin
from ohm.framework.graph_mixins.customer_support import CustomerSupportGraphMixin
from ohm.framework.graph_mixins.temporal import TemporalGraphMixin
from ohm.framework.graph_mixins.substrate import SubstrateGraphMixin
from ohm.framework.graph_mixins.cybersecurity import CybersecurityGraphMixin
from ohm.framework.graph_mixins.discovery import DiscoveryGraphMixin
from ohm.framework.graph_mixins.write import WriteGraphMixin
from ohm.framework.graph_mixins.read import ReadGraphMixin


class Graph(DataProductsGraphMixin, ChangeFeedGraphMixin, SubstrateComputationGraphMixin, DiscoveryExportGraphMixin, EdgeVersioningGraphMixin, CustomerSupportGraphMixin, TemporalGraphMixin, SubstrateGraphMixin, CybersecurityGraphMixin, DiscoveryGraphMixin, WriteGraphMixin, ReadGraphMixin):  # noqa: E501
    """A connection to an OHM knowledge graph.

    Wraps a DuckDB connection with the OHM schema and provides
    high-level methods for reading and writing the graph.
    """

    def __init__(self, conn: DuckDBPyConnection, *, actor: str = "unknown"):
        self._conn = conn
        self.actor = actor
        self.token: str | None = None
        self.tenant_id: str | None = None
        self._signing_key: bytes | None = None

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> Graph:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def connect(
    db_path: str = ":memory:",
    *,
    actor: str = "unknown",
    token: str | None = None,
    tenant_id: str | None = None,
) -> Graph:
    """Open a connection to an OHM graph.

    Args:
        db_path: Path to DuckDB file, or ':memory:' for in-memory.
        actor: Agent name for attribution.
        token: Bearer token for ohmd authentication. If not provided,
               reads from OHM_TOKEN environment variable.
        tenant_id: Optional tenant identifier for multi-tenant routing
            (OHM-xbbi). When provided, opens a tenant-scoped DB at
            {db_path}/{actor}/{tenant_id}/ohm.duckdb. When db_path is
            ':memory:', tenant_id is stored in graph metadata only.

    Returns:
        A Graph instance ready for use.
    """
    import os

    from ohm.db import connect as db_connect

    resolved_token = token or os.environ.get("OHM_TOKEN")

    # Resolve tenant-scoped path (OHM-xbbi)
    if tenant_id is not None and db_path != ":memory:":
        from pathlib import Path as _Path

        tenant_db = str(_Path(db_path) / actor / tenant_id / "ohm.duckdb")
        _Path(tenant_db).parent.mkdir(parents=True, exist_ok=True)
        conn = db_connect(tenant_db)
    else:
        conn = db_connect(db_path)

    graph = Graph(conn, actor=actor)
    graph.token = resolved_token
    graph.tenant_id = tenant_id
    return graph


def connect_remote(
    uri: str = "quack:localhost",
    *,
    actor: str = "unknown",
    token: str | None = None,
    token_env: str | None = None,
    alias: str = "remote",
    strict: bool = True,
) -> Graph:
    """Connect to a remote OHM graph via Quack protocol.

    .. deprecated::
        Use :func:`connect_http` instead — it connects to the ohmd daemon
        via HTTP REST API and does not require the DuckDB Quack extension.
        Quack is not available in most DuckDB builds, causing
        connect_remote() to fail or silently fall back to stale local data.

    Creates a local in-memory DuckDB connection and attaches the remote
    Quack server as a catalog. All graph operations are sent to the
    remote server through Quack.

    Args:
        uri: Quack URI of the remote server (default: quack:localhost).
        actor: Agent name for attribution.
        token: Quack authentication token.
        token_env: Environment variable for the token (default: QUACK_TOKEN).
        alias: Catalog alias for the remote (default: 'remote').
        strict: If True (default), raise ConnectionError when Quack attach
            fails. If False, fall back to local file connection with warnings.

    Returns:
        A Graph instance connected to the remote server.
    """
    import os

    from ohm.db import connect as db_connect
    from ohm.quack import attach_remote, is_available

    conn = db_connect(":memory:")

    if is_available(conn):
        try:
            attach_remote(
                conn,
                uri=uri,
                alias=alias,
                token=token,
                token_env=token_env or "QUACK_TOKEN",
            )
            # Set search path to remote catalog so queries go there
            conn.execute(f"SET search_path = {alias}.main")
            graph = Graph(conn, actor=actor)
            graph.token = token or os.environ.get(token_env or "QUACK_TOKEN")
            return graph
        except Exception as e:
            if strict:
                raise ConnectionError(f"Failed to attach to remote Quack server at {uri}: {e}. Set strict=False to fall back to direct file connection.") from e
            # Fall back to direct connection with warning
            import warnings

            warnings.warn(
                f"Quack attach failed ({e}), falling back to local DB. Data may be stale. Set strict=False to suppress this warning.",
                UserWarning,
            )

    # Fallback: direct file connection
    if strict:
        raise ConnectionError(
            f"Quack is not available in this DuckDB installation. "
            f"Cannot connect to remote server at {uri}. "
            "Use connect_http() instead to connect via the ohmd REST API. "
            "Set strict=False to fall back to direct file connection, "
            "or install DuckDB with Quack extension support."
        )
    import warnings

    warnings.warn(
        "Quack not available, connecting to local DB. Set strict=False to suppress this warning.",
        UserWarning,
    )
    db_path = os.environ.get("OHM_DB", str(Path.home() / ".ohm" / "ohm.duckdb"))
    conn = db_connect(db_path)
    graph = Graph(conn, actor=actor)
    graph.token = token or os.environ.get("OHM_TOKEN")
    return graph


def connect_http(
    base_url: str = "http://127.0.0.1:8710",
    *,
    actor: str = "unknown",
    token: str | None = None,
    tenant_id: str | None = None,
    token_type: str | None = None,
) -> Graph:
    """Connect to an OHM daemon via HTTP REST API.

    This is the **recommended** way to connect to a running ohmd daemon.
    Unlike connect_remote(), this does not require the DuckDB Quack extension
    and works with any standard DuckDB installation.

    For the shared convenience client used by Olympus agents, see
    ``ohm_client.OHMClient`` (at /root/olympus/shared/ohm_client.py).

    Creates a local in-memory DuckDB connection for query caching and
    wraps HTTP calls to the ohmd REST API for write operations.
    Field names are mapped: SDK uses from_node/to_node/edge_type,
    HTTP API uses from/to/type.

    Multi-tenant usage:
        - Customer API key (token='ohm-cust-...') auto-routes to the tenant
          via server-side token resolution. Do NOT pass tenant_id; the SDK
          will not send X-Tenant-ID for customer keys.
        - Agent token on behalf of a tenant: pass tenant_id to send
          X-Tenant-ID header. The server routes to that tenant's store.

    Args:
        base_url: URL of the ohmd daemon (default: http://127.0.0.1:8710).
        actor: Agent name for attribution.
        token: Bearer token for authentication. Reads from OHM_TOKEN env if not provided.
        tenant_id: Optional tenant ID. Sends X-Tenant-ID header for agent-acting-on-tenant.
        token_type: Optional 'agent' or 'customer'. If omitted, inferred from the token prefix.

    Returns:
        A Graph instance connected via HTTP.
    """
    import json
    import os
    import urllib.request
    import urllib.error

    from ohm.db import connect as db_connect

    resolved_token = token or os.environ.get("OHM_TOKEN")
    resolved_token_type = token_type
    if resolved_token and resolved_token_type is None:
        # Customer API keys start with 'ohm-cu...'; everything else is treated as agent.
        resolved_token_type = "customer" if resolved_token.lower().startswith("ohm-cu") else "agent"
    conn = db_connect(":memory:")

    class HttpGraph(Graph):
        """Graph subclass that routes all requests through HTTP API."""

        def __init__(self, conn, actor, base_url, token, tenant_id=None, token_type=None):
            super().__init__(conn, actor=actor)
            self._base_url = base_url.rstrip("/")
            self._token = token
            self._tenant_id = tenant_id
            self._token_type = token_type

        def _http_request(self, method: str, path: str, body: dict | None = None) -> dict:
            """Make an HTTP request to the ohmd daemon with timeout."""
            url = f"{self._base_url}{path}"
            data = json.dumps(body).encode() if body else None
            headers = {"Content-Type": "application/json"}
            if self._token:
                token_header = f"Bearer {self._token}"
                try:
                    token_header.encode("latin-1")
                except UnicodeEncodeError:
                    from urllib.parse import quote

                    token_header = f"Bearer {quote(self._token, safe='-._~')}"
                headers["Authorization"] = token_header
            # ADR-043: customer-scoped tokens must NOT send X-Tenant-ID in transit.
            # Only agent tokens send the header, and only when a tenant_id was supplied.
            if self._tenant_id and self._token_type != "customer":
                headers["X-Tenant-ID"] = self._tenant_id
            # Pass actor identity as X-Ohm-Agent header so the server
            # can use it for created_by when the token maps to a generic agent.
            if self.actor and self.actor != "unknown":
                headers["X-Ohm-Agent"] = self.actor

            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    # Force UTF-8 decoding to handle non-ASCII characters
                    raw = resp.read()
                    try:
                        return json.loads(raw.decode("utf-8"))
                    except UnicodeDecodeError:
                        # Fallback to latin-1 with warning
                        import logging

                        logger = logging.getLogger(__name__)
                        logger.warning(f"Latin-1 fallback for response from {method} {path}")
                        return json.loads(raw.decode("latin-1"))
            except urllib.error.HTTPError as e:
                error_body = e.read().decode() if e.fp else str(e)
                raise ConnectionError(f"HTTP {e.code} from {method} {path}: {error_body}") from e
            except urllib.error.URLError as e:
                raise ConnectionError(f"Connection failed for {method} {path}: {e.reason}") from e
            except TimeoutError as e:
                raise ConnectionError(f"Timeout for {method} {path}: request took longer than 30s") from e

        def create_node(self, label: str, *, node_type: str = "concept", **kwargs) -> dict[str, Any]:
            """Create a node via HTTP API. Auto-generates ID from label."""
            import re

            node_id = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60]
            body = {
                "id": node_id,
                "label": label,
                "type": node_type,
                "content": kwargs.get("content"),
                "confidence": kwargs.get("confidence", 1.0),
                "visibility": kwargs.get("visibility", "team"),
                "provenance": kwargs.get("provenance"),
                "priority": kwargs.get("priority"),
                "tags": kwargs.get("tags"),
                "metadata": kwargs.get("metadata"),
                "utility_scale": kwargs.get("utility_scale"),
                "current_best_action": kwargs.get("current_best_action"),
                "action_alternatives": kwargs.get("action_alternatives"),
            }
            connects_to = kwargs.get("connects_to")
            if connects_to is not None:
                body["connects_to"] = connects_to
            return self._http_request("POST", "/node", body)

        def create_edge(self, *, from_node: str, to_node: str, edge_type: str, layer: str = "L3", **kwargs) -> dict[str, Any]:
            """Create an edge via HTTP API. Maps from_node→from, to_node→to, edge_type→type."""
            body = {
                "from": from_node,
                "to": to_node,
                "type": edge_type,
                "layer": layer,
                "confidence": kwargs.get("confidence", 0.7),
                "condition": kwargs.get("condition"),
                "provenance": kwargs.get("provenance"),
                "urgency": kwargs.get("urgency"),
                "probability": kwargs.get("probability"),
                "probability_p05": kwargs.get("probability_p05"),
                "probability_p50": kwargs.get("probability_p50"),
                "probability_p95": kwargs.get("probability_p95"),
                "confidence_p05": kwargs.get("confidence_p05"),
                "confidence_p50": kwargs.get("confidence_p50"),
                "confidence_p95": kwargs.get("confidence_p95"),
            }
            return self._http_request("POST", "/edge", body)

        def scratch(self, content: str, *, tags: list[str] | None = None, connects_to: list[str] | None = None, **kwargs) -> dict[str, Any]:
            """Write an L0 thinking fragment (OHM-a5rz.5). Single POST to /scratch."""
            body: dict[str, Any] = {"content": content}
            if tags:
                body["tags"] = tags
            if connects_to:
                body["connects_to"] = connects_to
            return self._http_request("POST", "/scratch", body)

        def link_fragment(self, fragment_id: str, target_id: str, edge_type: str = "REFINES_FRAG", note: str | None = None, **kwargs) -> dict[str, Any]:
            """Link two fragments via L0 edge (OHM-a5rz.11). POST to /fragments/{id}/connect."""
            body: dict[str, Any] = {"target_id": target_id, "edge_type": edge_type}
            if note:
                body["note"] = note
            return self._http_request("POST", f"/fragments/{fragment_id}/connect", body)

        def resolve_question(self, fragment_id: str, **kwargs) -> dict[str, Any]:
            """Mark a question fragment as resolved (OHM-a5rz.12). POST to /fragments/{id}/resolve."""
            return self._http_request("POST", f"/fragments/{fragment_id}/resolve", {})

        def fragment_resonance(self, min_shared: int = 2, limit: int = 10, **kwargs) -> list[dict[str, Any]]:
            """Detect cross-agent fragment resonance (OHM-a5rz.13). GET /admin/fragment-resonance."""
            result = self._http_request("GET", f"/admin/fragment-resonance?min_shared={min_shared}&limit={limit}")
            return result.get("resonance", []) if isinstance(result, dict) else []

        def stats(self) -> dict[str, Any]:
            """Get graph stats from the daemon."""
            return self._http_request("GET", "/stats")

        def listen(self, *, since: str | None = None, **kwargs) -> list[dict[str, Any]]:
            """Get change feed from the daemon."""
            params = []
            if since:
                params.append(f"since={since}")
            path = "/listen"
            if params:
                path += "?" + "&".join(params)
            return self._http_request("GET", path)

        def changes(self, *, since: str | None = None, limit: int = 100, **kwargs) -> dict[str, Any]:
            """Get personalized changes delta from the daemon (OHM-b7l7).

            Delegates to ``GET /changes`` with the agent name implicit in
            the daemon-side authentication token.
            """
            params = []
            if since:
                params.append(f"since={since}")
            params.append(f"limit={limit}")
            path = "/changes?" + "&".join(params)
            return self._http_request("GET", path)

        def search(self, query: str, *, node_type: str | None = None, limit: int = 20, include_l0: bool = False) -> list[dict[str, Any]]:
            """Search nodes via the daemon's /search endpoint (ILIKE text search).

            OHM-a5rz.18: L0 fragments excluded by default. Pass include_l0=True
            to include fragment-type nodes.

            Args:
                query: Text to search for in labels and content.
                node_type: Optional type filter.
                limit: Maximum results (default 20).
                include_l0: Include fragment-type nodes (default False).

            Returns:
                List of matching node records.
            """
            import urllib.parse

            params = [f"q={urllib.parse.quote(query)}", f"limit={limit}"]
            if node_type:
                params.append(f"type={node_type}")
            if include_l0:
                params.append("include_l0=true")
            path = "/search?" + "&".join(params)
            return self._http_request("GET", path)

        def semantic_search(
            self,
            query: str,
            *,
            node_type: str | None = None,
            limit: int = 10,
            min_confidence: float | None = None,
            membership_weight: float | None = None,
        ) -> list[dict[str, Any]]:
            """Search nodes via semantic similarity using embeddings.

            Args:
                query: Natural language text to search for.
                node_type: Optional type filter.
                limit: Maximum results (default 10).
                min_confidence: Minimum confidence threshold.
                membership_weight: Optional blend weight in [0, 1] for HD
                    Hamming similarity alongside cosine similarity (OHM-xuf4).
                    When None (default), pure cosine ranking is returned.
                    When provided, each result also carries
                    ``cosine_similarity``, ``hd_similarity``, and
                    ``blended_score`` fields, and results are re-ranked by
                    blended_score descending.

            Returns:
                List of dicts with node_id, label, type, confidence, distance.
                When ``membership_weight`` is set, each dict also carries
                ``cosine_similarity``, ``hd_similarity`` (None if node has
                no stored fingerprint), and ``blended_score``.
            """
            import urllib.parse

            params = [f"q={urllib.parse.quote(query)}", f"limit={limit}"]
            if node_type:
                params.append(f"type={node_type}")
            if min_confidence is not None:
                params.append(f"min_confidence={min_confidence}")
            if membership_weight is not None:
                params.append(f"membership_weight={membership_weight}")
            path = "/semantic_search?" + "&".join(params)
            return self._http_request("GET", path)

        def neighborhood(self, node_id: str, *, depth: int = 1) -> list[dict[str, Any]]:
            """Get edges in the neighborhood of a node.

            Args:
                node_id: The center node ID.
                depth: How many hops to explore (default 1).

            Returns:
                List of edge records in the neighborhood.
            """
            path = f"/neighborhood/{node_id}?depth={depth}"
            return self._http_request("GET", path)

        def delete_node(self, node_id: str) -> dict[str, Any]:
            """Delete a node via HTTP API."""
            return self._http_request("DELETE", f"/node/{node_id}")

        def get_node(self, node_id: str) -> dict[str, Any] | None:
            """Get a node by ID."""
            try:
                return self._http_request("GET", f"/node/{node_id}")
            except ConnectionError as e:
                if "404" in str(e):
                    return None
                raise

        def decision_recommend(self, node_id: str) -> dict[str, Any]:
            """Get the recommendation for a decision node.

            Returns a dict with keys: decision_id, label, current_best_action,
            action_alternatives, confidence, key_assumptions, utility_scale.
            """
            return self._http_request("GET", f"/decision/{node_id}/recommendation")

        def challenge(self, node_id: str, *, value: float | None = None, sigma: float = 0.5, notes: str | None = None, challenge_type: str | None = None) -> dict[str, Any]:
            """Challenge a node with an observation (records observation on node)."""
            body = {"value": value, "sigma": sigma}
            if notes:
                body["notes"] = notes
            if challenge_type:
                body["challenge_type"] = challenge_type
            return self._http_request("POST", f"/challenge/{node_id}", body)

        def challenge_edge(self, edge_id: str, *, reason: str = "", confidence: float = 0.5, challenge_type: str = "CHALLENGED_BY") -> dict[str, Any]:
            """Challenge an existing edge (creates CHALLENGED_BY edge).

            This is the proper way to challenge an interpretation — it creates
            a CHALLENGED_BY edge that shows up in confidence audits.

            Args:
                edge_id: The edge ID to challenge.
                reason: Why you're challenging this edge.
                confidence: Your confidence in the challenge (0-1).
                challenge_type: Type of challenge (CHALLENGED_BY, CONTRADICTS).

            Returns:
                The challenge edge record.
            """
            body = {"reason": reason, "confidence": confidence, "challenge_type": challenge_type}
            return self._http_request("POST", f"/challenge/{edge_id}", body)

        def support_edge(self, edge_id: str, *, reason: str = "", confidence: float = 0.8) -> dict[str, Any]:
            """Support an existing edge (creates SUPPORTS edge).

            Args:
                edge_id: The edge ID to support.
                reason: Why you support this edge.
                confidence: Your confidence in the support (0-1).

            Returns:
                The support edge record.
            """
            body = {"reason": reason, "confidence": confidence}
            return self._http_request("POST", f"/support/{edge_id}", body)

        def observe(
            self,
            node_id: str,
            *,
            obs_type: str = "measurement",
            value: float | None = None,
            baseline: float | None = None,
            sigma: float | None = None,
            source: str = "analysis",
            notes: str | None = None,
            source_name: str | None = None,
            source_url: str | None = None,
        ) -> dict[str, Any]:
            """Record an observation on a node.

            Args:
                node_id: Node to observe.
                obs_type: Type (measurement/anomaly/pattern/challenge/support/sentiment).
                value: Numeric observation value.
                baseline: Expected/baseline value.
                sigma: Standard deviation/confidence.
                source: Source (analysis/research/conversation/signal).
                notes: Free-text notes.
                source_name: Name of the source agent/system.
                source_url: URL reference.
            """
            body = {"type": obs_type}
            if value is not None:
                body["value"] = value
            if baseline is not None:
                body["baseline"] = baseline
            if sigma is not None:
                body["sigma"] = sigma
            if source:
                body["source"] = source
            if notes:
                body["notes"] = notes
            if source_name:
                body["source_name"] = source_name
            if source_url:
                body["source_url"] = source_url
            return self._http_request("POST", f"/observe/{node_id}", body)

        def compound_confidence(
            self,
            observations: list[dict],
            *,
            correlation: float = 0.0,
            source_weights: dict[str, float] | None = None,
        ) -> dict[str, Any]:
            """Combine multiple confidence values accounting for correlation and source reliability.

            When source_weights is provided, observations from reliable sources (higher
            p_accurate) count more. An observation from a reliable source (0.9) counts
            1.8× more than one from an unknown source (0.5).

            Computed client-side from observation dicts.
            When observations are independent (correlation=0.0), confidences compound
            multiplicatively. When perfectly correlated (1.0), only the strongest matters.

            Args:
                observations: List of dicts with 'confidence' key (0-1).
                    May also include 'source' or 'created_by' for weighting.
                correlation: 0.0 = independent, 1.0 = perfectly correlated.
                source_weights: Optional dict mapping source -> reliability weight.
                    E.g., {"agent_a": 0.9, "agent_b": 0.5}. Default weight=0.5.

            Returns:
                Dict with compound_confidence, method, correlation, observation_count,
                weighted (bool).
            """
            from ohm.methods import compound_confidence as _cc

            return _cc(observations, correlation=correlation, source_weights=source_weights)

        def record_outcome(
            self,
            *,
            source_agent: str,
            claim_node: str,
            outcome: bool,
            notes: str | None = None,
        ) -> dict[str, Any]:
            """Record whether a source agent's claim was correct or incorrect via HTTP."""
            body = {
                "source_agent": source_agent,
                "claim_node": claim_node,
                "outcome": outcome,
            }
            if notes:
                body["notes"] = notes
            return self._http_request("POST", "/outcome", body)

        def source_reliability(
            self,
            source_agent: str,
        ) -> dict[str, Any]:
            """Compute source reliability metrics from historical outcomes via HTTP."""
            import urllib.parse

            path = f"/reliability/{urllib.parse.quote(source_agent)}"
            return self._http_request("GET", path)

        # ── Task management ──────────────────────────────────────────────

        def create_task(
            self,
            id: str,
            label: str,
            content: str | None = None,
            *,
            priority: str = "P2",
            task_status: str = "open",
            assigned_to: str | None = None,
            due_date: str | None = None,
            confidence: float = 1.0,
            visibility: str = "team",
            provenance: str | None = None,
        ) -> dict[str, Any]:
            """Create a task node in the graph.

            Tasks are first-class nodes (type='task') that can be linked
            to concepts, patterns, and agents via edges. This enables
            context-rich task management where every task inherits the
            graph's relationship structure.

            Args:
                id: Unique task identifier.
                label: Human-readable task title.
                content: Task description / acceptance criteria.
                priority: P0-P4 (default P2).
                task_status: open/in_progress/blocked/review/done/cancelled.
                assigned_to: Agent name assigned to this task.
                due_date: ISO 8601 due date string.
                confidence: Confidence in task necessity (0.0-1.0).
                visibility: private/team/public.
                provenance: Source attribution.

            Returns:
                Node record with 'created' key.
            """
            from .schema import VALID_TASK_STATUSES, VALID_PRIORITY

            if task_status not in VALID_TASK_STATUSES:
                raise ValueError(f"Invalid task_status: {task_status} — must be one of: {', '.join(sorted(VALID_TASK_STATUSES))}")
            if priority not in VALID_PRIORITY:
                raise ValueError(f"Invalid priority: {priority} — must be one of: {', '.join(sorted(VALID_PRIORITY))}")
            body = {
                "id": id,
                "label": label,
                "type": "task",
                "content": content,
                "priority": priority,
                "task_status": task_status,
                "assigned_to": assigned_to,
                "due_date": due_date,
                "confidence": confidence,
                "visibility": visibility,
                "provenance": provenance,
            }
            return self._http_request("POST", "/node?create_only=false", body)

        def list_tasks(
            self,
            *,
            status: str | None = None,
            assigned_to: str | None = None,
            priority: str | None = None,
            limit: int = 100,
            offset: int = 0,
        ) -> dict[str, Any]:
            """List task nodes with optional filtering.

            Args:
                status: Filter by task_status (open/in_progress/blocked/review/done/cancelled).
                assigned_to: Filter by assigned agent.
                priority: Filter by priority (P0-P4).
                limit: Maximum results (default 100).
                offset: Pagination offset.

            Returns:
                Dict with 'tasks' list, 'total', 'limit', 'offset'.
            """
            import urllib.parse

            params = [f"limit={limit}", f"offset={offset}"]
            if status:
                params.append(f"status={urllib.parse.quote(status)}")
            if assigned_to:
                params.append(f"assigned_to={urllib.parse.quote(assigned_to)}")
            if priority:
                params.append(f"priority={urllib.parse.quote(priority)}")
            path = "/tasks?" + "&".join(params)
            return self._http_request("GET", path)

        def update_task_status(self, task_id: str, status: str) -> dict[str, Any]:
            """Update a task's status.

            Args:
                task_id: The task node ID.
                status: New status (open/in_progress/blocked/review/done/cancelled).

            Returns:
                Updated node record.
            """
            from .schema import VALID_TASK_STATUSES

            if status not in VALID_TASK_STATUSES:
                raise ValueError(f"Invalid status: {status} — must be one of: {', '.join(sorted(VALID_TASK_STATUSES))}")
            # Get current node to preserve other fields
            node = self.get_node(task_id)
            if node is None:
                raise ValueError(f"Task {task_id} not found")
            if node.get("type") != "task":
                raise ValueError(f"Node {task_id} is not a task (type={node.get('type')})")
            body = {
                "id": task_id,
                "label": node.get("label", ""),
                "type": "task",
                "content": node.get("content"),
                "priority": node.get("priority"),
                "task_status": status,
                "assigned_to": node.get("assigned_to"),
                "due_date": node.get("due_date"),
                "confidence": node.get("confidence", 1.0),
                "visibility": node.get("visibility", "team"),
                "provenance": node.get("provenance"),
            }
            return self._http_request("POST", "/node?create_only=false", body)

        def complete_task_with_outcome(
            self,
            task_id: str,
            outcome: str,
            *,
            notes: str | None = None,
            claim_node: str | None = None,
        ) -> dict[str, Any]:
            """Close a task and record its outcome against the linked claim (OHM-f5iq).

            Args:
                task_id: The task node id to close.
                outcome: ``TRUE`` (claim confirmed), ``FALSE`` (claim falsified),
                    or ``AMBIGUOUS`` (could not determine).
                notes: Optional justification for the outcome.
                claim_node: Optional explicit claim node id. Defaults to the
                    task's ``expected_claim`` column.

            Returns:
                Dict with ``task`` (updated node), ``outcome`` (canonical
                uppercase), and ``outcome_record`` (or None when AMBIGUOUS
                with no claim).
            """
            from .schema import VALID_TASK_OUTCOMES

            normalized = str(outcome).upper()
            if normalized not in VALID_TASK_OUTCOMES:
                raise ValueError(f"Invalid outcome: {outcome} — must be one of: {', '.join(sorted(VALID_TASK_OUTCOMES))}")
            body: dict[str, Any] = {"outcome": normalized}
            if notes is not None:
                body["notes"] = notes
            if claim_node is not None:
                body["claim_node"] = claim_node
            return self._http_request("POST", f"/tasks/{task_id}/outcome", body)

        def bayesian_inference(
            self,
            target: str,
            evidence: dict[str, int] | None = None,
            *,
            edge_types: list[str] | None = None,
            leak_probability: float = 0.15,
        ) -> dict[str, Any]:
            """Run Bayesian inference on the graph.

            Given observed evidence (node states), compute posterior probabilities
            for the target node using Variable Elimination. Requires pgmpy.

            Args:
                target: Node ID to compute posterior for.
                evidence: Dict mapping node IDs to observed states.
                    State 0 = "bad" (failure, closed, negative).
                    State 1 = "good" (normal, open, positive).
                    Pass empty dict or None for prior (no evidence).
                edge_types: Edge types to include (default: CAUSES, DEPENDS_ON,
                    THREATENS, EXPECTED_LIKELIHOOD).
                leak_probability: Baseline probability of bad outcome when all
                    parents are good (default 0.15). Critical for realistic priors.

            Returns:
                Dict with posterior probabilities, method, and network info.
                Falls back to heuristic cascade if pgmpy is unavailable.
            """
            import urllib.parse

            params = [f"target={urllib.parse.quote(target)}"]
            if evidence:
                evidence_str = ",".join(f"{k}:{v}" for k, v in evidence.items())
                params.append(f"evidence={urllib.parse.quote(evidence_str)}")
            params.append(f"leak={leak_probability}")
            path = "/inference?" + "&".join(params)
            return self._http_request("GET", path)

        def belief(
            self,
            target: str,
            evidence: dict[str, int | float] | None = None,
            *,
            edge_types: list[str] | None = None,
            layers: list[str] | None = None,
            leak_probability: float = 0.15,
            include_evidence_movers: bool = True,
            include_prior: bool = True,
            belief_statement: str | None = None,
        ) -> dict[str, Any]:
            """Get a complete belief summary for a target node (OHM-934).

            Composes posterior, drivers, VoI, and optionally prior/surprise,
            evidence movers, and belief calibration into a single call.

            Args:
                target: Node ID to query.
                evidence: Dict mapping node IDs to observed states.
                edge_types: Edge types for Bayesian network.
                layers: Causal layer filter.
                leak_probability: Noisy-OR leak probability.
                include_evidence_movers: Include per-observation impact ranking.
                include_prior: Include prior distribution and KL surprise.
                belief_statement: Agent-stated belief to calibrate, e.g. 'P(bad)=0.5'.

            Returns:
                Dict with posterior percentiles, drivers, VoI suggestions,
                and optional prior/surprise, evidence_movers, calibration.
            """
            import urllib.parse

            params = [f"target={urllib.parse.quote(target)}"]
            if evidence:
                evidence_str = ",".join(f"{k}:{v}" for k, v in evidence.items())
                params.append(f"evidence={urllib.parse.quote(evidence_str)}")
            if edge_types:
                params.append(f"edge_types={urllib.parse.quote(','.join(edge_types))}")
            if layers:
                params.append(f"layers={urllib.parse.quote(','.join(layers))}")
            params.append(f"leak={leak_probability}")
            params.append(f"include_evidence_movers={str(include_evidence_movers).lower()}")
            params.append(f"include_prior={str(include_prior).lower()}")
            if belief_statement:
                params.append(f"belief_statement={urllib.parse.quote(belief_statement)}")
            path = "/belief?" + "&".join(params)
            return self._http_request("GET", path)

        def causal_intervention(
            self,
            target: str,
            intervention_state: int,
            *,
            query_nodes: list[str] | None = None,
            leak_probability: float = 0.15,
        ) -> dict[str, Any]:
            """Run causal intervention using Pearl's do-operator (graph surgery).

            Differs from bayesian_inference (observation) in a critical way:
            - Observation: P(Y | X=x) includes confounder effects
            - Intervention: P(Y | do(X=x)) isolates the direct causal effect

            Implementation: sever all incoming edges to target, set target to
            intervention_state deterministically, propagate through remaining DAG.
            Then compare with observation-based inference to quantify confounding bias.

            Args:
                target: Node ID to intervene on.
                intervention_state: State to set the target to.
                    0 = "bad" (force failure), 1 = "good" (force normal).
                query_nodes: Optional list of downstream nodes to compute posteriors for.
                    If None, computes posteriors for all reachable descendants.
                leak_probability: Baseline probability of bad outcome when all
                    parents are good (default 0.15).

            Returns:
                Dict with posteriors for each downstream node, comparison with
                observation-based inference (confounding bias), and network info.
            """
            import urllib.parse

            params = [f"target={urllib.parse.quote(target)}"]
            params.append(f"state={intervention_state}")
            if query_nodes:
                query_str = ",".join(query_nodes)
                params.append(f"query={urllib.parse.quote(query_str)}")
            params.append(f"leak={leak_probability}")
            path = "/intervene?" + "&".join(params)
            return self._http_request("GET", path)

        def ate(
            self,
            cause: str,
            effect: str,
            *,
            leak_probability: float = 0.15,
        ) -> dict[str, Any]:
            """Compute Average Treatment Effect (ATE) from the Bayesian model.

            Model-based ATE: P(effect=bad|do(cause=bad)) - P(effect=bad|do(cause=good)).
            No observational data required — computed from the noisy-OR CPDs.

            Args:
                cause: Node ID for the treatment variable.
                effect: Node ID for the outcome variable.
                leak_probability: Baseline probability of bad outcome when all
                    parents are good (default 0.15).

            Returns:
                Dict with ATE, risk ratio, effect size, and interpretation.
            """
            import urllib.parse

            params = [f"cause={urllib.parse.quote(cause)}"]
            params.append(f"effect={urllib.parse.quote(effect)}")
            params.append(f"leak={leak_probability}")
            path = "/ate?" + "&".join(params)
            return self._http_request("GET", path)

        def sensitivity(
            self,
            cause: str,
            effect: str,
            *,
            leak_probability: float = 0.15,
        ) -> dict[str, Any]:
            """Compute sensitivity analysis (E-value) for a causal effect.

            The E-value (VanderWeele & Ding, 2017) answers:
            "How much unmeasured confounding would it take to overturn this conclusion?"

            Args:
                cause: Node ID for the treatment variable.
                effect: Node ID for the outcome variable.
                leak_probability: Baseline probability of bad outcome when all
                    parents are good (default 0.15).

            Returns:
                Dict with E-value, risk ratio, robustness assessment, and
                confounder perturbation analysis.
            """
            import urllib.parse

            params = [f"cause={urllib.parse.quote(cause)}"]
            params.append(f"effect={urllib.parse.quote(effect)}")
            params.append(f"leak={leak_probability}")
            path = "/sensitivity?" + "&".join(params)
            return self._http_request("GET", path)

        def adjustment(
            self,
            cause: str,
            effect: str,
            *,
            leak_probability: float = 0.15,
        ) -> dict[str, Any]:
            """Find valid backdoor/frontdoor adjustment sets for causal identification.

            Uses Pearl's criteria to identify which variables to condition on
            to get an unbiased estimate of the causal effect of cause on effect.

            Args:
                cause: Node ID for the treatment variable.
                effect: Node ID for the outcome variable.
                leak_probability: Baseline probability of bad outcome.

            Returns:
                Dict with backdoor sets, frontdoor sets, minimal adjustment set,
                instrumental variables, and adjusted estimates.
            """
            import urllib.parse

            params = [f"cause={urllib.parse.quote(cause)}"]
            params.append(f"effect={urllib.parse.quote(effect)}")
            params.append(f"leak={leak_probability}")
            path = "/adjustment?" + "&".join(params)
            return self._http_request("GET", path)

        def suggest_causes(
            self,
            *,
            min_confidence: float = 0.5,
        ) -> dict[str, Any]:
            """Suggest candidate CAUSES edges from existing non-causal relationships.

            Scans DEPENDS_ON, APPLIES_TO, REFINES, INFLUENCES, and EXPECTED_LIKELIHOOD
            edges for pairs that lack CAUSES edges. Also identifies root cause nodes
            and nodes disconnected from the causal graph.

            Args:
                min_confidence: Minimum confidence threshold for candidates.

            Returns:
                Dict with candidate_causes, root_causes, and disconnected nodes.
            """
            path = f"/suggest_causes?min_confidence={min_confidence}"
            return self._http_request("GET", path)

        def voi(
            self,
            decision: list[str] | None = None,
            *,
            top: int = 10,
            leak_probability: float = 0.15,
            root_prior: float = 0.3,
            layers: list[str] | None = None,
            edge_types: list[str] | None = None,
            timeout: float | None = None,
            min_observations: int = 0,
        ) -> dict[str, Any]:
            """Rank nodes by Value of Information for a set of decision nodes.

            Args:
                decision: Optional list of decision node IDs. Auto-detected if omitted.
                top: Number of top candidates to return.
                leak_probability: Baseline probability of bad outcome.
                root_prior: Prior probability of root bad state.
                layers: Layer filter list.
                edge_types: Edge-type filter list.
                timeout: Optional timeout in seconds.
                min_observations: Minimum observations before low-data warning.

            Returns:
                Dict with ranked VoI candidates and metadata.
            """
            import urllib.parse

            params = [f"top={top}", f"leak={leak_probability}", f"root_prior={root_prior}"]
            if decision:
                params.append(f"decision={urllib.parse.quote(','.join(decision))}")
            if layers:
                params.append(f"layers={urllib.parse.quote(','.join(layers))}")
            if edge_types:
                params.append(f"edge_types={urllib.parse.quote(','.join(edge_types))}")
            if timeout:
                params.append(f"timeout={timeout}")
            if min_observations:
                params.append(f"min_observations={min_observations}")
            path = "/voi?" + "&".join(params)
            return self._http_request("GET", path)

        def voi_tasks(
            self,
            *,
            agent: str | None = None,
            decision: list[str] | None = None,
            top: int = 5,
            leak_probability: float = 0.15,
            root_prior: float = 0.3,
            layers: list[str] | None = None,
        ) -> dict[str, Any]:
            """Generate concrete observation tasks from VoI ranking.

            Args:
                agent: Filter tasks for a specific agent.
                decision: List of decision node IDs.
                top: Number of tasks to generate.
                leak_probability: Baseline probability of bad outcome.
                root_prior: Prior probability of root bad state.
                layers: Layer filter list.

            Returns:
                Dict with task assignments.
            """
            import urllib.parse

            params = [f"top={top}", f"leak={leak_probability}", f"root_prior={root_prior}"]
            if agent:
                params.append(f"agent={urllib.parse.quote(agent)}")
            if decision:
                params.append(f"decision={urllib.parse.quote(','.join(decision))}")
            if layers:
                params.append(f"layers={urllib.parse.quote(','.join(layers))}")
            path = "/voi/tasks?" + "&".join(params)
            return self._http_request("GET", path)

        def regime(
            self,
            target: str,
            evidence: dict[str, int | float] | None = None,
            *,
            leak_probability: float = 0.15,
            window_days: float = 30.0,
            layers: list[str] | None = None,
        ) -> dict[str, Any]:
            """Detect regime shifts by comparing full-history vs windowed inference.

            Args:
                target: Target node ID.
                evidence: Dict of node-state evidence.
                leak_probability: Baseline probability of bad outcome.
                window_days: Window size in days.
                layers: Layer filter list.

            Returns:
                Dict with full_history, windowed, shift, and regime label.
            """
            import urllib.parse

            params = [f"target={urllib.parse.quote(target)}"]
            if evidence:
                ev_str = ",".join(f"{k}:{v}" for k, v in evidence.items())
                params.append(f"evidence={urllib.parse.quote(ev_str)}")
            params.append(f"leak={leak_probability}")
            params.append(f"window_days={window_days}")
            if layers:
                params.append(f"layers={urllib.parse.quote(','.join(layers))}")
            path = "/regime?" + "&".join(params)
            return self._http_request("GET", path)

        def game(
            self,
            target: str,
            *,
            players: list[str] | None = None,
            layers: list[str] | None = None,
        ) -> dict[str, Any]:
            """Extract a normal-form game from the causal graph around a target.

            Args:
                target: Target node ID.
                players: Optional list of player node IDs.
                layers: Layer filter list.

            Returns:
                Dict with payoff matrices, players, and actions.
            """
            import urllib.parse

            params = [f"target={urllib.parse.quote(target)}"]
            if players:
                params.append(f"players={urllib.parse.quote(','.join(players))}")
            if layers:
                params.append(f"layers={urllib.parse.quote(','.join(layers))}")
            path = "/game?" + "&".join(params)
            return self._http_request("GET", path)

        def nash(
            self,
            players: list[str],
            payoffs: list[list[list[float]]],
        ) -> dict[str, Any]:
            """Compute Nash equilibrium for an extracted game.

            Args:
                players: List of player identifiers.
                payoffs: Payoff matrices as returned by game().

            Returns:
                Dict with equilibrium strategies and payoffs.
            """
            import json
            import urllib.parse

            params = [f"players={urllib.parse.quote(','.join(players))}", f"payoffs={urllib.parse.quote(json.dumps(payoffs))}"]
            path = "/nash?" + "&".join(params)
            return self._http_request("GET", path)

        def policy(
            self,
            target: str,
            *,
            observation_cost: float | None = None,
            horizon: int = 1,
            leak_probability: float = 0.15,
            layers: list[str] | None = None,
        ) -> dict[str, Any]:
            """Compute a POMDP Phase-1 policy: observe vs act.

            Args:
                target: Target node ID.
                observation_cost: Cost of one observation.
                horizon: Planning horizon.
                leak_probability: Baseline probability of bad outcome.
                layers: Layer filter list.

            Returns:
                Dict with recommended action, EVPI, and belief state.
            """
            import urllib.parse

            params = [f"target={urllib.parse.quote(target)}", f"horizon={horizon}", f"leak={leak_probability}"]
            if observation_cost is not None:
                params.append(f"observation_cost={observation_cost}")
            if layers:
                params.append(f"layers={urllib.parse.quote(','.join(layers))}")
            path = "/policy?" + "&".join(params)
            return self._http_request("GET", path)

        def discover(
            self,
            nodes: list[str] | None = None,
            *,
            method: str = "pc",
            alpha: float = 0.05,
            min_observations: int = 5,
            indep_test: str = "fisherz",
            score_class: str = "local_score_BIC",
            queue: bool = False,
        ) -> dict[str, Any]:
            """Run causal structure discovery (PC/GES) on observation data.

            Args:
                nodes: Optional list of node IDs to restrict discovery to.
                method: 'pc', 'ges', or 'both'.
                alpha: Significance threshold.
                min_observations: Minimum observations per node.
                indep_test: Independence test for PC.
                score_class: Score class for GES.
                queue: If True, queue candidate edges for review.

            Returns:
                Dict with candidate_edges and optional queued_ids.
            """
            import urllib.parse

            params = [f"method={urllib.parse.quote(method)}", f"alpha={alpha}", f"min_observations={min_observations}"]
            if nodes:
                params.append(f"nodes={urllib.parse.quote(','.join(nodes))}")
            params.append(f"indep_test={urllib.parse.quote(indep_test)}")
            params.append(f"score_class={urllib.parse.quote(score_class)}")
            if queue:
                params.append("queue=true")
            path = "/discover?" + "&".join(params)
            return self._http_request("GET", path)

        def discovery_queue(
            self,
            *,
            status: str | None = None,
            method: str | None = None,
            limit: int = 100,
        ) -> dict[str, Any]:
            """List pending causal-discovery candidates for review.

            Args:
                status: Filter by status.
                method: Filter by discovery method.
                limit: Maximum records.

            Returns:
                Dict with queue list and count.
            """
            import urllib.parse

            params = [f"limit={limit}"]
            if status:
                params.append(f"status={urllib.parse.quote(status)}")
            if method:
                params.append(f"method={urllib.parse.quote(method)}")
            path = "/discover/queue?" + "&".join(params)
            return self._http_request("GET", path)

        def review_discovery(
            self,
            queue_id: str,
            action: str,
            *,
            reviewed_by: str | None = None,
            review_notes: str | None = None,
            edge_layer: str = "L3",
        ) -> dict[str, Any]:
            """Accept or reject a queued discovery candidate.

            Args:
                queue_id: Discovery queue entry ID.
                action: 'accept' or 'reject'.
                reviewed_by: Agent name.
                review_notes: Optional notes.
                edge_layer: Layer for created edge.

            Returns:
                Result dict from the review operation.
            """
            body: dict[str, Any] = {"queue_id": queue_id, "action": action, "edge_layer": edge_layer}
            if reviewed_by:
                body["reviewed_by"] = reviewed_by
            if review_notes:
                body["review_notes"] = review_notes
            return self._http_request("POST", "/discover/queue/review", body)

        def refute(
            self,
            cause: str,
            effect: str,
            *,
            n_samples: int = 1000,
            seed: int = 42,
            methods: list[str] | None = None,
        ) -> dict[str, Any]:
            """Test robustness of causal conclusions using DoWhy refutation methods.

            Generates synthetic data from the Bayesian network, then applies
            refutation methods to test how robust the causal estimate is.

            Methods: random_common_cause, placebo_treatment, data_subset,
            unobserved_confounder (default: all).

            Args:
                cause: Node ID for the treatment variable.
                effect: Node ID for the outcome variable.
                n_samples: Number of synthetic samples to generate.
                seed: Random seed for reproducibility.
                methods: List of refutation methods to apply.

            Returns:
                Dict with refutation results for each method.
            """
            import urllib.parse

            params = [f"cause={urllib.parse.quote(cause)}"]
            params.append(f"effect={urllib.parse.quote(effect)}")
            params.append(f"n_samples={n_samples}")
            params.append(f"seed={seed}")
            if methods:
                params.append(f"methods={urllib.parse.quote(','.join(methods))}")
            path = "/refute?" + "&".join(params)
            return self._http_request("GET", path)

        def lint(
            self,
            *,
            node_types: list[str] | None = None,
            limit: int = 1000,
        ) -> dict[str, Any]:
            """Lint the graph against the contract.

            Validates all nodes and edges for naming conventions, required fields,
            confidence bounds, and type validity.

            Args:
                node_types: Filter to specific node types (e.g., ["concept", "task"]).
                limit: Maximum entities to check per type.

            Returns:
                Dict with violations, summary, and contract info.
            """
            import urllib.parse

            params = [f"limit={limit}"]
            if node_types:
                params.append(f"node_types={urllib.parse.quote(','.join(node_types))}")
            path = "/lint?" + "&".join(params)
            return self._http_request("GET", path)

        def contract(self) -> dict[str, Any]:
            """Return the current contract configuration."""
            return self._http_request("GET", "/contract")

        def detect_verifiable_claims(
            self,
            *,
            agent: str | None = None,
            days_threshold: int = 14,
            confidence_threshold: float = 0.85,
            limit: int = 100,
        ) -> list[dict[str, Any]]:
            """Detect verifiable dated claims past their expected date with no outcome."""
            import urllib.parse

            params = [f"days_threshold={days_threshold}"]
            params.append(f"confidence_threshold={confidence_threshold}")
            params.append(f"limit={limit}")
            if agent:
                params.append(f"agent={urllib.parse.quote(agent)}")
            path = "/verifications/detect?" + "&".join(params)
            return self._http_request("GET", path)

        def create_verification_nudge(
            self,
            *,
            edge_id: str,
            confidence: float = 0.5,
            reason: str | None = None,
        ) -> dict[str, Any]:
            """Create a NUDGES_FOR_VERIFICATION edge prompting verification of a claim."""
            body = {"edge_id": edge_id, "confidence": confidence}
            if reason:
                body["reason"] = reason
            return self._http_request("POST", "/verifications/nudge", body)

        def record_verification_outcome(
            self,
            *,
            edge_id: str,
            outcome: str,
            reason: str | None = None,
        ) -> dict[str, Any]:
            """Record a verification outcome for a verifiable claim edge."""
            body = {"edge_id": edge_id, "outcome": outcome}
            if reason:
                body["reason"] = reason
            return self._http_request("POST", "/verifications/outcome", body)

        def list_pending_verifications(
            self,
            *,
            agent: str | None = None,
            limit: int = 100,
        ) -> list[dict[str, Any]]:
            """List pending NUDGES_FOR_VERIFICATION edges that haven't been resolved."""
            import urllib.parse

            params = [f"limit={limit}"]
            if agent:
                params.append(f"agent={urllib.parse.quote(agent)}")
            path = "/verifications/pending?" + "&".join(params)
            return self._http_request("GET", path)

    graph = HttpGraph(conn, actor, base_url, resolved_token, tenant_id=tenant_id, token_type=resolved_token_type)
    graph.tenant_id = tenant_id
    graph.token = resolved_token
    return graph
