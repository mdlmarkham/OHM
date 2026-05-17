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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class Graph:
    """A connection to an OHM knowledge graph.

    Wraps a DuckDB connection with the OHM schema and provides
    high-level methods for reading and writing the graph.
    """

    def __init__(self, conn: DuckDBPyConnection, *, actor: str = "unknown"):
        self._conn = conn
        self.actor = actor
        self.token: str | None = None

    # ── Write ────────────────────────────────────────────────────────────

    def create_node(
        self,
        label: str,
        *,
        node_type: str = "concept",
        content: str | None = None,
        visibility: str = "team",
        provenance: str | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Create a node and return its full record.

        The node ID is auto-generated from the label (lowercased, spaces→underscores,
        with a short unique suffix). Returns the complete node record including
        all fields (id, label, type, content, created_by, created_at, etc.).
        """
        from ohm.queries import create_node

        return create_node(
            self._conn,
            label=label,
            node_type=node_type,
            content=content,
            created_by=self.actor,
            visibility=visibility,
            provenance=provenance,
            confidence=confidence,
        )

    def create_edge(
        self,
        *,
        from_node: str,
        to_node: str,
        edge_type: str,
        layer: str = "L3",
        confidence: float = 0.7,
        condition: str | None = None,
        provenance: str | None = None,
    ) -> dict[str, Any]:
        """Create an edge and return its full record.

        Returns the complete edge record including all fields
        (id, from_node, to_node, layer, edge_type, created_at, etc.).
        """
        from ohm.queries import create_edge

        return create_edge(
            self._conn,
            from_node=from_node,
            to_node=to_node,
            layer=layer,
            edge_type=edge_type,
            created_by=self.actor,
            confidence=confidence,
            condition=condition,
            provenance=provenance,
        )

    def challenge(self, edge_id: str, *, reason: str, confidence: float = 0.5) -> dict[str, Any]:
        """Challenge an existing edge. Returns the full challenge edge record."""
        from ohm.queries import create_challenge

        return create_challenge(
            self._conn,
            edge_id=edge_id,
            reason=reason,
            created_by=self.actor,
            confidence=confidence,
        )

    def support(self, edge_id: str, *, reason: str, confidence: float = 0.7) -> dict[str, Any]:
        """Support an existing edge. Returns the full support edge record."""
        from ohm.queries import create_support

        return create_support(
            self._conn,
            edge_id=edge_id,
            reason=reason,
            created_by=self.actor,
            confidence=confidence,
        )

    def update_edge(
        self,
        edge_id: str,
        *,
        confidence: float | None = None,
        provenance: str | None = None,
        condition: str | None = None,
    ) -> None:
        """Update your own edge. Raises PermissionDeniedError if not the owner."""
        from ohm.boundary import enforce_write_boundary

        enforce_write_boundary(self._conn, self.actor, edge_id)

        # Build SET clause dynamically — column names are hardcoded, not user-provided
        set_clauses: list[str] = []
        params: list[Any] = []
        if confidence is not None:
            set_clauses.append("confidence = ?")
            params.append(confidence)
        if provenance is not None:
            set_clauses.append("provenance = ?")
            params.append(provenance)
        if condition is not None:
            set_clauses.append("condition = ?")
            params.append(condition)
        if not set_clauses:
            return
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        set_clauses.append("updated_by = ?")
        params.append(self.actor)
        params.append(edge_id)
        self._conn.execute(
            "UPDATE ohm_edges SET " + ", ".join(set_clauses) + " WHERE id = ?", params,
        )

    def observe(
        self,
        node_id: str,
        *,
        obs_type: str,
        value: float | None = None,
        baseline: float | None = None,
        sigma: float | None = None,
        source: str = "analysis",
    ) -> dict[str, Any]:
        """Record an observation on a node. Returns the full observation record."""
        from ohm.queries import create_observation

        return create_observation(
            self._conn,
            node_id=node_id,
            obs_type=obs_type,
            value=value,
            baseline=baseline,
            sigma=sigma,
            source=source,
            created_by=self.actor,
        )

    def set_focus(self, focus: str) -> None:
        """Set the current focus for this agent."""
        from ohm.queries import set_agent_state

        set_agent_state(self._conn, agent_name=self.actor, focus=focus)

    def register_agent(
        self,
        *,
        description: str | None = None,
        values: list[str] | None = None,
        goals: list[str] | None = None,
        capabilities: list[str] | None = None,
        interests: list[str] | None = None,
        listens_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register this agent in the graph with identity, values, and capabilities.

        Creates an agent node and declares VALUES, GOALS, CAPABLE_OF,
        and INTERESTED_IN edges. Uses find_or_create for idempotency —
        calling twice won't duplicate the agent node or declared edges.

        Args:
            description: Agent description (stored as node content).
            values: What this agent optimizes for (e.g., ["wisdom", "connections"]).
            goals: What this agent is trying to achieve.
            capabilities: What this agent can do (e.g., ["research", "critique"]).
            interests: Topics this agent subscribes to (e.g., ["economics", "cognition"]).
            listens_to: Other agents whose output this agent follows.

        Returns:
            The agent node record.
        """
        # Create agent node
        me = self.find_or_create_node(
            label=self.actor,
            node_type="agent",
            content=description,
        )

        # Declare values (L1 — identity)
        for v in (values or []):
            value_node = self.find_or_create_node(label=v, node_type="value")
            # Check if edge already exists
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'VALUES' AND created_by = ?",
                [me["id"], value_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"], to_node=value_node["id"],
                    edge_type="VALUES", layer="L1", confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare goals (L1 — identity)
        for g in (goals or []):
            goal_node = self.find_or_create_node(label=g, node_type="goal")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'GOALS' AND created_by = ?",
                [me["id"], goal_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"], to_node=goal_node["id"],
                    edge_type="GOALS", layer="L1", confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare capabilities (L1 — identity)
        for c in (capabilities or []):
            cap_node = self.find_or_create_node(label=c, node_type="skill")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'CAPABLE_OF' AND created_by = ?",
                [me["id"], cap_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"], to_node=cap_node["id"],
                    edge_type="CAPABLE_OF", layer="L1", confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare interests / subscriptions (L1 — identity)
        for i in (interests or []):
            topic_node = self.find_or_create_node(label=i, node_type="topic")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'INTERESTED_IN' AND created_by = ?",
                [me["id"], topic_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"], to_node=topic_node["id"],
                    edge_type="INTERESTED_IN", layer="L1", confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare agent subscriptions (L3 — challengeable preference)
        for a in (listens_to or []):
            other_agent = self.find_or_create_node(label=a, node_type="agent")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'LISTENS_TO' AND created_by = ?",
                [me["id"], other_agent["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"], to_node=other_agent["id"],
                    edge_type="LISTENS_TO", layer="L3", confidence=0.7,
                    provenance="self_declaration",
                )

        return me

    # ── Read ─────────────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Retrieve a single node by ID.

        Returns the full node record (id, label, type, content, created_by,
        created_at, confidence, visibility, provenance, tags, metadata)
        or None if not found.
        """
        result = self._conn.execute(
            "SELECT * FROM ohm_nodes WHERE id = ?", [node_id]
        ).fetchone()
        if result is None:
            return None
        columns = [desc[0] for desc in self._conn.description]
        return dict(zip(columns, result))

    def get_edge(self, edge_id: str) -> dict[str, Any] | None:
        """Retrieve a single edge by ID.

        Returns the full edge record (id, from_node, to_node, layer, edge_type,
        confidence, condition, provenance, created_by, created_at, challenge_of,
        challenge_type) or None if not found.
        """
        result = self._conn.execute(
            "SELECT * FROM ohm_edges WHERE id = ?", [edge_id]
        ).fetchone()
        if result is None:
            return None
        columns = [desc[0] for desc in self._conn.description]
        return dict(zip(columns, result))

    def find_or_create_node(
        self,
        label: str,
        *,
        node_type: str = "concept",
        content: str | None = None,
        visibility: str = "team",
        provenance: str | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Find a node by label, or create it if it doesn't exist.

        Searches for an existing node with the exact label (case-insensitive).
        If found, returns its full record. If not found, creates a new node.

        Returns the full node record.
        """
        result = self._conn.execute(
            "SELECT id FROM ohm_nodes WHERE LOWER(label) = LOWER(?) LIMIT 1",
            [label],
        ).fetchone()
        if result:
            return self.get_node(result[0])  # type: ignore[return-value]
        return self.create_node(
            label=label,
            node_type=node_type,
            content=content,
            visibility=visibility,
            provenance=provenance,
            confidence=confidence,
        )

    def search_nodes(
        self,
        query: str,
        *,
        limit: int = 20,
        node_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search nodes by label or content text.

        Performs a case-insensitive ILIKE search on both label and content.
        Optionally filter by node_type.

        Args:
            query: Text to search for in labels and content.
            limit: Maximum results (default 20).
            node_type: Optional type filter (e.g., 'concept', 'source').

        Returns:
            List of matching node records.
        """
        # Build WHERE clause from hardcoded column names + parameterized values.
        # Column names (label, content, type) are not user-provided — only values use ?.
        conditions: list[str] = ["(label ILIKE ? OR content ILIKE ?)"]
        params: list[Any] = [f"%{query}%", f"%{query}%"]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)

        sql = (
            "SELECT * FROM ohm_nodes WHERE "
            + " AND ".join(conditions)
            + " ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit)
        result = self._conn.execute(sql, params)
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

    def neighborhood(
        self,
        node_id: str,
        *,
        depth: int = 3,
        layer: str | None = None,
        direction: str = "both",
    ) -> list[dict[str, Any]]:
        """Bounded-depth traversal from a node."""
        from ohm.queries import query_neighborhood

        return query_neighborhood(
            self._conn, node_id, depth=depth, layer=layer, direction=direction,
        )

    def path(
        self,
        from_node: str,
        to_node: str,
        *,
        max_depth: int = 10,
    ) -> list[dict[str, Any]]:
        """Shortest path between two nodes."""
        from ohm.queries import query_path

        return query_path(self._conn, from_node, to_node, max_depth=max_depth)

    def impact(self, node_id: str, *, depth: int = 5) -> list[dict[str, Any]]:
        """Downstream impact analysis."""
        from ohm.queries import query_impact

        return query_impact(self._conn, node_id, depth=depth)

    def confidence(self, edge_id: str) -> dict[str, Any]:
        """Full provenance and challenge audit for an edge."""
        from ohm.queries import query_confidence

        return query_confidence(self._conn, edge_id)

    def listen(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """Change feed since a timestamp."""
        from ohm.queries import query_change_feed

        return query_change_feed(self._conn, since=since)

    def agent_state(self, agent_name: str | None = None) -> list[dict[str, Any]]:
        """Query agent state."""
        from ohm.queries import query_agent_state

        return query_agent_state(self._conn, agent_name=agent_name)

    def stats(self) -> dict[str, Any]:
        """Graph statistics."""
        from ohm.queries import query_stats

        return query_stats(self._conn)

    # ── Substrate Methods ────────────────────────────────────────────────

    def aggregate(
        self, node_id: str, *, method: str = "weighted"
    ) -> dict[str, Any]:
        """Combine multiple observations on a node into a single value.

        Strategies: weighted (inverse-variance), mean, max_confidence, consensus.
        Same result regardless of caller — substrate method.
        """
        from ohm.methods import aggregate_observations

        return aggregate_observations(self._conn, node_id, method=method)

    def anomalies(
        self,
        *,
        sigma_threshold: float = 2.0,
        layer: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Detect anomalous observations using sigma-based flagging.

        |value - baseline| / sigma > threshold. Same result regardless of caller.
        """
        from ohm.methods import detect_anomalies

        return detect_anomalies(
            self._conn, sigma_threshold=sigma_threshold, layer=layer, limit=limit,
        )

    def contradictions(
        self,
        *,
        confidence_threshold: float = 0.5,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Flag conflicting observations and interpretations between agents.

        Detects: opposite observations, high-confidence challenges, contradictory
        L3 interpretations. Does NOT resolve — only surfaces for agents to address.

        Same result regardless of caller — substrate method.
        """
        from ohm.methods import detect_contradictions

        return detect_contradictions(
            self._conn, confidence_threshold=confidence_threshold, limit=limit,
        )

    def heartbeat(self, *, focus: str | None = None) -> dict[str, Any]:
        """Send an agent heartbeat. Updates last-seen timestamp.

        Call this at regular intervals (every sync_interval_sec). The substrate
        uses this to detect stale agents for health monitoring.

        Args:
            focus: Optional update to current focus.

        Returns:
            Updated agent state record.
        """
        from ohm.methods import agent_heartbeat

        return agent_heartbeat(self._conn, self.actor, focus=focus)

    def agent_health(self) -> list[dict[str, Any]]:
        """Check health of all registered agents.

        Returns status per agent: alive, stale, dead, or unknown.
        Stale = last heartbeat > 2x sync interval. Dead = never heartbeated.

        Same result regardless of caller — substrate method.
        """
        from ohm.methods import query_agent_health

        return query_agent_health(self._conn)

    def health(self) -> dict[str, Any]:
        """Compute structural health metrics for the graph."""
        from ohm.queries import query_graph_health

        return query_graph_health(self._conn)

    def provenance(self, node_id: str, *, max_depth: int = 10) -> list[dict[str, Any]]:
        """Trace provenance chain backward from a node.

        Follows DERIVES_FROM, REFERENCES, INFLUENCES, and SUPPORTS edges
        to find primary sources. Returns each source with chain depth and
        confidence product.

        Args:
            node_id: The node to trace from.
            max_depth: Maximum chain depth (default 10).

        Returns:
            List of source records with depth, confidence_product, and chain_path.
        """
        from ohm.queries import query_provenance

        return query_provenance(self._conn, node_id, max_depth=max_depth)

    def stale_edges(
        self,
        *,
        half_life_days: dict[str, float] | None = None,
        stale_threshold: float = 0.1,
    ) -> list[dict[str, Any]]:
        """Find edges whose confidence has decayed below a threshold.

        Decay is computed at read time (no data mutation):
        - L1/L2: no decay (permanent)
        - L3: 90-day half-life
        - L4: 30-day half-life

        effective_confidence = confidence * 0.5 ^ (age_days / half_life)

        Args:
            half_life_days: Override per-layer half-lives.
            stale_threshold: Effective confidence below this is stale (default 0.1).

        Returns:
            List of stale edge records with effective_confidence and decay_factor.
        """
        from ohm.queries import query_stale_edges

        return query_stale_edges(
            self._conn, half_life_days=half_life_days, stale_threshold=stale_threshold,
        )

    def batch_create_nodes(
        self,
        *,
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Create multiple nodes at once. All succeed or all fail.

        Args:
            nodes: List of dicts with: label, node_type, content, visibility,
                   provenance, confidence.

        Returns:
            List of created node records.
        """
        from ohm.queries import batch_create_nodes

        return batch_create_nodes(self._conn, nodes=nodes, created_by=self.actor)

    def batch_create_edges(
        self,
        *,
        edges: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Create multiple edges at once. All succeed or all fail.

        Args:
            edges: List of dicts with: from_node, to_node, edge_type, layer,
                   confidence, condition, provenance.

        Returns:
            List of created edge records.
        """
        from ohm.queries import batch_create_edges

        return batch_create_edges(self._conn, edges=edges, created_by=self.actor)

    def get_agent_config(self, agent_name: str) -> dict[str, Any] | None:
        """Get an agent's configuration (optimization target, services, etc.).

        Config is admin-set and read-only for agents. Returns None if
        the agent has no config entry.
        """
        result = self._conn.execute(
            "SELECT * FROM ohm_agent_config WHERE agent_name = ?", [agent_name]
        ).fetchone()
        if result is None:
            return None
        columns = [desc[0] for desc in self._conn.description]
        return dict(zip(columns, result))

    def list_agent_configs(self) -> list[dict[str, Any]]:
        """List all agent configurations.

        Returns the full config for every registered agent, including
        optimization targets, available services, and thresholds.
        """
        result = self._conn.execute(
            "SELECT * FROM ohm_agent_config ORDER BY agent_name"
        )
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

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
) -> Graph:
    """Open a connection to an OHM graph.

    Args:
        db_path: Path to DuckDB file, or ':memory:' for in-memory.
        actor: Agent name for attribution.
        token: Bearer token for ohmd authentication. If not provided,
               reads from OHM_TOKEN environment variable.

    Returns:
        A Graph instance ready for use.
    """
    import os

    from ohm.db import connect as db_connect

    resolved_token = token or os.environ.get("OHM_TOKEN")
    conn = db_connect(db_path)
    graph = Graph(conn, actor=actor)
    graph.token = resolved_token
    return graph
