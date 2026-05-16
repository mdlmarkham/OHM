"""OHM Python SDK — programmatic API for agents.

Provides a clean Python interface for agents to interact with the
knowledge graph without calling the CLI or writing raw SQL.

Usage:
    import ohm.sdk as ohm

    with ohm.connect(":memory:", actor="metis") as graph:
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
    ) -> str:
        """Create a node and return its ID.

        The node ID is auto-generated from the label (lowercased, spaces→underscores,
        with a short unique suffix). Use get_node() to retrieve the full record.
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
    ) -> str:
        """Create an edge and return its ID.

        Use get_edge() to retrieve the full record including timestamps.
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

    def challenge(self, edge_id: str, *, reason: str, confidence: float = 0.5) -> str:
        """Challenge an existing edge. Returns the challenge edge ID."""
        from ohm.queries import create_challenge

        return create_challenge(
            self._conn,
            edge_id=edge_id,
            reason=reason,
            created_by=self.actor,
            confidence=confidence,
        )

    def support(self, edge_id: str, *, reason: str, confidence: float = 0.7) -> str:
        """Support an existing edge. Returns the support edge ID."""
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
    ) -> str:
        """Record an observation on a node. Returns the observation ID."""
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
    ) -> str:
        """Find a node by label, or create it if it doesn't exist.

        Searches for an existing node with the exact label (case-insensitive).
        If found, returns its ID. If not found, creates a new node.

        Returns the node ID.
        """
        result = self._conn.execute(
            "SELECT id FROM ohm_nodes WHERE LOWER(label) = LOWER(?) LIMIT 1",
            [label],
        ).fetchone()
        if result:
            return result[0]
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
        conditions = ["(label ILIKE ? OR content ILIKE ?)"]
        params: list[Any] = [f"%{query}%", f"%{query}%"]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)

        where = " AND ".join(conditions)
        result = self._conn.execute(
            f"SELECT * FROM ohm_nodes WHERE {where} ORDER BY created_at DESC LIMIT ?",
            [*params, limit],
        )
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
