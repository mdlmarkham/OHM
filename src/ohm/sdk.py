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

    def search_edges(
        self,
        *,
        layer: str | None = None,
        edge_type: str | None = None,
        confidence_min: float | None = None,
        confidence_max: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search edges by layer, type, and confidence range.

        Args:
            layer: Optional layer filter (L1-L4).
            edge_type: Optional edge type filter.
            confidence_min: Minimum confidence threshold.
            confidence_max: Maximum confidence threshold.
            limit: Maximum results (default 100).

        Returns:
            List of matching edge records.
        """
        conditions: list[str] = ["1=1"]
        params: list[Any] = []
        if layer:
            conditions.append("layer = ?")
            params.append(layer)
        if edge_type:
            conditions.append("edge_type = ?")
            params.append(edge_type)
        if confidence_min is not None:
            conditions.append("confidence >= ?")
            params.append(confidence_min)
        if confidence_max is not None:
            conditions.append("confidence <= ?")
            params.append(confidence_max)

        sql = (
            "SELECT * FROM ohm_edges WHERE "
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

    def evolve_identity(
        self,
        edge_id: str,
        *,
        new_target: str,
        reason: str,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        """Evolve an identity edge (VALUES, GOALS, CAPABLE_OF, INTERESTED_IN).

        Identity evolution is NOT modification — it's a directed replacement.
        The old edge is marked superseded, and a new edge is created pointing
        to the new target. The change feed preserves the full history.

        Only the owning agent can evolve their own identity edges.
        Non-identity edges cannot be evolved (use challenge instead).

        Args:
            edge_id: The identity edge to evolve.
            new_target: Label of the new target node.
            reason: Why this evolution happened (stored in provenance).
            confidence: Confidence in the new identity declaration.

        Returns:
            The new edge record.
        """
        from ohm.boundary import enforce_identity_evolution
        from ohm.queries import _log_change

        enforce_identity_evolution(self._conn, self.actor, edge_id)

        # Get the old edge details
        old_edge = self.get_edge(edge_id)
        if old_edge is None:
            raise ValueError(f"Edge {edge_id} not found")

        # Mark old edge as superseded via metadata
        self._conn.execute(
            "UPDATE ohm_edges SET metadata = ? WHERE id = ?",
            ['{"superseded": true, "superseded_by": "pending"}', edge_id],
        )

        # Find or create the new target node
        edge_type = old_edge["edge_type"]
        node_type_map = {
            "VALUES": "value",
            "GOALS": "goal",
            "CAPABLE_OF": "skill",
            "INTERESTED_IN": "topic",
        }
        target_type = node_type_map.get(edge_type, "concept")
        new_node = self.find_or_create_node(label=new_target, node_type=target_type)

        # Create the new edge
        new_edge = self.create_edge(
            from_node=old_edge["from_node"],
            to_node=new_node["id"],
            edge_type=edge_type,
            layer="L1",
            confidence=confidence,
            provenance=f"evolved_from:{edge_id} reason:{reason}",
        )

        # Update the old edge's metadata with the new edge ID
        import json
        self._conn.execute(
            "UPDATE ohm_edges SET metadata = ? WHERE id = ?",
            [json.dumps({"superseded": True, "superseded_by": new_edge["id"]}), edge_id],
        )

        _log_change(self._conn, "ohm_edges", edge_id, "EVOLVE", self.actor)
        return new_edge

    def discover_peers(self) -> list[dict[str, Any]]:
        """Cold start discovery — find agents with shared values and interests.

        For new agents who need to bootstrap their relationships:
        1. Find agents with overlapping VALUES edges
        2. Find agents with overlapping INTERESTED_IN edges
        3. Find agents CAPABLE_OF what you need
        4. Rank by overlap count

        Returns:
            List of peer agents with overlap scores and suggested LISTENS_TO edges.
        """
        # Get my agent node
        agent_row = self._conn.execute(
            "SELECT id FROM ohm_nodes WHERE label = ? AND type = 'agent'",
            [self.actor],
        ).fetchone()
        me = self.get_node(agent_row[0]) if agent_row else None

        if me is None:
            return []  # Not registered yet

        me_id = me["id"]

        # Find my values and interests
        my_values = set()
        my_interests = set()
        my_capabilities = set()

        for row in self._conn.execute(
            "SELECT to_node, edge_type FROM ohm_edges WHERE from_node = ? AND layer = 'L1'",
            [me_id],
        ).fetchall():
            if row[1] == "VALUES":
                my_values.add(row[0])
            elif row[1] == "INTERESTED_IN":
                my_interests.add(row[0])
            elif row[1] == "CAPABLE_OF":
                my_capabilities.add(row[0])

        if not my_values and not my_interests:
            return []  # No identity declared

        # Find other agents with overlapping edges
        other_agents = self._conn.execute("""
            SELECT
                n.id AS agent_id,
                n.label AS agent_name,
                COUNT(DISTINCT e.to_node) AS overlap_count
            FROM ohm_nodes n
            JOIN ohm_edges e ON e.from_node = n.id AND e.layer = 'L1'
            WHERE n.type = 'agent'
              AND n.id != ?
              AND (
                (e.edge_type = 'VALUES' AND e.to_node IN (SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'VALUES' AND layer = 'L1'))
                OR
                (e.edge_type = 'INTERESTED_IN' AND e.to_node IN (SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'INTERESTED_IN' AND layer = 'L1'))
              )
            GROUP BY n.id, n.label
            ORDER BY overlap_count DESC
            LIMIT 10
        """, [me_id, me_id, me_id]).fetchall()

        # Find agents with capabilities I might need (complementary)
        # (agents who can do what I can't)
        complementary = self._conn.execute("""
            SELECT
                n.id AS agent_id,
                n.label AS agent_name,
                e.to_node AS capability_id,
                cn.label AS capability_label
            FROM ohm_nodes n
            JOIN ohm_edges e ON e.from_node = n.id AND e.edge_type = 'CAPABLE_OF' AND e.layer = 'L1'
            LEFT JOIN ohm_nodes cn ON cn.id = e.to_node
            WHERE n.type = 'agent'
              AND n.id != ?
              AND e.to_node NOT IN (
                SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'CAPABLE_OF' AND layer = 'L1'
              )
            LIMIT 10
        """, [me_id, me_id]).fetchall()

        results = []
        for agent in other_agents:
            results.append({
                "agent_id": agent[0],
                "agent_name": agent[1],
                "shared_values_interests": agent[2],
                "recommendation": "LISTENS_TO",
            })

        for cap in complementary:
            # Don't duplicate if already in results
            if not any(r["agent_id"] == cap[0] for r in results):
                results.append({
                    "agent_id": cap[0],
                    "agent_name": cap[1],
                    "complementary_capability": cap[3],
                    "recommendation": "LISTENS_TO",
                })

        return results

    # ── Change Feed Consumer ─────────────────────────────────────────────

    def listen(
        self,
        *,
        since: str | None = None,
        topics: list[str] | None = None,
        agents: list[str] | None = None,
        operations: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Consume the change feed, optionally filtered by topic, agent, or operation.

        This is the primary mechanism for agents to stay aware of changes.
        Called at regular intervals (heartbeat cadence).

        Topic filtering: if topics are specified, only returns changes to nodes
        whose label matches one of the agent's INTERESTED_IN topics (fuzzy match).

        Args:
            since: ISO timestamp or None (uses last_sync from agent state).
            topics: Filter to changes affecting these topic labels.
            agents: Filter to changes by these agents.
            operations: Filter to these operations (INSERT, UPDATE, EVOLVE, CHALLENGE).
            limit: Maximum changes to return.

        Returns:
            List of change feed entries relevant to this agent.
        """
        from ohm.queries import query_change_feed

        # Resolve 'since' from agent state if not provided
        if since is None:
            state = self._conn.execute(
                "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
                [self.actor],
            ).fetchone()
            if state and state[0]:
                since = str(state[0])
            else:
                # Default to last hour
                since = None  # Will return recent changes

        # Get raw change feed
        changes = query_change_feed(
            self._conn,
            since=since,
            agent_name=agents[0] if agents and len(agents) == 1 else None,
            limit=limit * 2,  # Overfetch for filtering
        )

        # Filter by topics if specified
        if topics:
            topic_labels = set(t.lower() for t in topics)
            filtered = []
            for change in changes:
                row_id = change.get("row_id", "")
                # Check if the changed node/edge relates to a topic
                node_label = self._conn.execute(
                    "SELECT label FROM ohm_nodes WHERE id = ?",
                    [row_id],
                ).fetchone()
                if node_label and any(t in node_label[0].lower() for t in topic_labels):
                    filtered.append(change)
                # Also check edges — target node might be a topic
                edge_target = self._conn.execute(
                    "SELECT n.label FROM ohm_edges e JOIN ohm_nodes n ON n.id = e.to_node WHERE e.id = ?",
                    [row_id],
                ).fetchone()
                if edge_target and any(t in edge_target[0].lower() for t in topic_labels):
                    filtered.append(change)
            changes = filtered

        # Filter by multiple agents if specified
        if agents and len(agents) > 1:
            agent_set = set(agents)
            changes = [c for c in changes if c.get("agent_name") in agent_set]

        # Filter by operations if specified
        if operations:
            op_set = set(operations)
            changes = [c for c in changes if c.get("operation") in op_set]

        # Don't include own changes by default (an agent doesn't need
        # to be notified about its own writes)
        changes = [c for c in changes if c.get("agent_name") != self.actor]

        # Update last_sync
        self._conn.execute(
            "UPDATE ohm_agent_state SET last_sync = now() WHERE agent_name = ?",
            [self.actor],
        )

        return changes[:limit]

    def pending_notifications(self) -> list[dict[str, Any]]:
        """Get pending notifications — changes since last listen() call.

        Shortcut for listen() with no filters. Returns changes from all
        agents since this agent last checked.

        Returns:
            List of change feed entries since last check.
        """
        return self.listen()

    # ── Substrate Computation ──────────────────────────────────────────

    def monte_carlo(
        self,
        node_id: str,
        *,
        simulations: int = 1000,
        depth: int = 3,
        confidence_threshold: float = 0.5,
    ) -> dict[str, Any]:
        """Monte Carlo simulation of failure propagation from a node.

        Randomly sample edge activation (on/off based on confidence) and
        trace downstream impact. Runs N simulations and returns the
        distribution of affected nodes.

        Same result regardless of which agent calls it — substrate method.

        Args:
            node_id: Source node for impact simulation.
            simulations: Number of Monte Carlo trials (default 1000).
            depth: Maximum traversal depth (default 3).
            confidence_threshold: Minimum confidence to consider edge active.

        Returns:
            Dict with affected_nodes, simulation_count, mean_affected, max_affected.
        """
        from ohm.methods import monte_carlo_impact

        return monte_carlo_impact(
            self._conn, node_id,
            simulations=simulations, depth=depth,
            confidence_threshold=confidence_threshold,
        )

    def near_duplicates(
        self,
        *,
        similarity_threshold: float = 0.8,
    ) -> list[dict[str, Any]]:
        """Find observations that may be duplicates from different agents.

        Two observations are near-duplicates if they're on the same node,
        same type, values within 10% of each other, and created within
        1 hour. The substrate flags these; agents decide whether to
        deduplicate.

        Same result regardless of which agent calls it — substrate method.

        Args:
            similarity_threshold: Minimum value similarity ratio (default 0.8).

        Returns:
            List of near-duplicate pairs with similarity scores.
        """
        from ohm.methods import detect_near_duplicates

        return detect_near_duplicates(
            self._conn, similarity_threshold=similarity_threshold,
        )

    def calibration(self, agent_name: str | None = None) -> dict[str, Any]:
        """Track how well an agent's confidence ratings predict outcomes.

        Calibration: do edges with high confidence actually hold up better?
        Measures the ratio of challenged vs. unchallenged edges by
        confidence band.

        Same result regardless of which agent calls it — substrate method.

        Args:
            agent_name: Agent to evaluate. Defaults to current actor.

        Returns:
            Dict with calibration_by_band, calibration_score (0-1).
        """
        from ohm.methods import compute_confidence_calibration

        return compute_confidence_calibration(
            self._conn, agent_name or self.actor,
        )

    # ── Discovery & Export ──────────────────────────────────────────────

    def suggest_connections(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Suggest links between nodes that share tags or co-occur in neighborhoods.

        Discovery strategies:
        1. Shared tags: nodes with overlapping tag sets
        2. Co-occurrence: nodes that appear in the same 2-hop neighborhood
        3. Type affinity: nodes of types that frequently connect

        The substrate suggests; agents decide whether to connect.
        Same result regardless of which agent calls it — substrate method.

        Args:
            limit: Maximum suggestions.

        Returns:
            List of {from_node, from_label, to_node, to_label, reason, score}.
        """
        suggestions = []

        # Strategy 1: Shared tags
        shared_tag_pairs = self._conn.execute("""
            SELECT
                n1.id AS from_node, n1.label AS from_label,
                n2.id AS to_node, n2.label AS to_label,
                COUNT(*) AS shared_tags
            FROM ohm_nodes n1
            JOIN ohm_nodes n2 ON n1.id < n2.id
            WHERE n1.tags IS NOT NULL AND n2.tags IS NOT NULL
              AND n1.tags != '[]' AND n2.tags != '[]'
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE (e.from_node = n1.id AND e.to_node = n2.id)
                     OR (e.from_node = n2.id AND e.to_node = n1.id)
              )
            GROUP BY n1.id, n1.label, n2.id, n2.label
            HAVING COUNT(*) >= 2
            ORDER BY shared_tags DESC
            LIMIT ?
        """, [limit]).fetchall()

        for row in shared_tag_pairs:
            suggestions.append({
                "from_node": row[0],
                "from_label": row[1],
                "to_node": row[2],
                "to_label": row[3],
                "reason": f"shared_tags({row[4]})",
                "score": row[4] / 5.0,  # Normalize
            })

        # Strategy 2: Co-occurrence in neighborhoods
        cooccur = self._conn.execute("""
            SELECT
                e1.from_node AS from_node,
                n1.label AS from_label,
                e2.from_node AS to_node,
                n2.label AS to_label,
                COUNT(*) AS cooccurrence
            FROM ohm_edges e1
            JOIN ohm_edges e2 ON e1.to_node = e2.to_node AND e1.from_node < e2.from_node
            LEFT JOIN ohm_nodes n1 ON n1.id = e1.from_node
            LEFT JOIN ohm_nodes n2 ON n2.id = e2.from_node
            WHERE NOT EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE (e.from_node = e1.from_node AND e.to_node = e2.from_node)
                   OR (e.from_node = e2.from_node AND e.to_node = e1.from_node)
            )
            GROUP BY e1.from_node, n1.label, e2.from_node, n2.label
            HAVING COUNT(*) >= 2
            ORDER BY cooccurrence DESC
            LIMIT ?
        """, [limit]).fetchall()

        for row in cooccur:
            from_node, from_label, to_node, to_label, count = row
            # Don't duplicate if already in suggestions
            if not any(s["from_node"] == from_node and s["to_node"] == to_node for s in suggestions):
                suggestions.append({
                    "from_node": from_node,
                    "from_label": from_label,
                    "to_node": to_node,
                    "to_label": to_label,
                    "reason": f"cooccurrence({count})",
                    "score": count / 5.0,
                })

        return sorted(suggestions, key=lambda s: -s["score"])[:limit]

    def export_graph(self) -> dict[str, Any]:
        """Export the entire graph as JSON-compatible dict.

        Used for backup, migration, and sharing.

        Returns:
            Dict with 'nodes', 'edges', 'observations', 'agent_state',
            'meta' (schema version, export timestamp, counts).
        """
        nodes = self._conn.execute("SELECT * FROM ohm_nodes ORDER BY created_at").fetchall()
        node_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_nodes LIMIT 0").description]
        nodes_json = []
        for row in nodes:
            d = dict(zip(node_cols, row))
            # Convert non-serializable types
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            nodes_json.append(d)

        edges = self._conn.execute("SELECT * FROM ohm_edges ORDER BY created_at").fetchall()
        edge_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_edges LIMIT 0").description]
        edges_json = []
        for row in edges:
            d = dict(zip(edge_cols, row))
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            edges_json.append(d)

        obs = self._conn.execute("SELECT * FROM ohm_observations ORDER BY created_at").fetchall()
        obs_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_observations LIMIT 0").description]
        obs_json = []
        for row in obs:
            d = dict(zip(obs_cols, row))
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            obs_json.append(d)

        agent_state = self._conn.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name").fetchall()
        as_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_agent_state LIMIT 0").description]
        as_json = []
        for row in agent_state:
            d = dict(zip(as_cols, row))
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            as_json.append(d)

        return {
            "meta": {
                "format": "ohm-export-v1",
                "schema_version": (
                    sv_row[0] if (sv_row := self._conn.execute(
                        "SELECT value FROM ohm_meta WHERE key = 'schema_version'",
                    ).fetchone()) else "unknown"
                ),
                "exported_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                "node_count": len(nodes_json),
                "edge_count": len(edges_json),
                "observation_count": len(obs_json),
            },
            "nodes": nodes_json,
            "edges": edges_json,
            "observations": obs_json,
            "agent_state": as_json,
        }

    def import_graph(self, data: dict[str, Any], *, merge: bool = True) -> dict[str, Any]:
        """Import graph data from an export dict.

        Args:
            data: Export dict (from export_graph()).
            merge: If True, merge with existing data (skip duplicates).
                   If False, replace all data (WARNING: destructive).

        Returns:
            Dict with import statistics.
        """
        import_count = {"nodes": 0, "edges": 0, "observations": 0, "skipped": 0}

        if not merge:
            # Destructive: clear all tables
            for table in ["ohm_observations", "ohm_edges", "ohm_nodes", "ohm_agent_state"]:
                self._conn.execute(f"DELETE FROM {table}")

        # Import nodes
        for node in data.get("nodes", []):
            existing = self._conn.execute(
                "SELECT id FROM ohm_nodes WHERE id = ?", [node["id"]]
            ).fetchone()
            if existing and merge:
                import_count["skipped"] += 1
                continue
            try:
                cols = [k for k in node.keys() if k not in ("id",)]
                vals = [node[k] for k in node.keys() if k not in ("id",)]
                col_str = ", ".join(["id"] + cols)
                val_str = ", ".join(["?"] * (1 + len(vals)))
                self._conn.execute(
                    f"INSERT INTO ohm_nodes ({col_str}) VALUES ({val_str})",
                    [node["id"]] + vals,
                )
                import_count["nodes"] += 1
            except Exception:
                import_count["skipped"] += 1

        # Import edges
        for edge in data.get("edges", []):
            existing = None
            if merge:
                existing = self._conn.execute(
                    "SELECT id FROM ohm_edges WHERE id = ?",
                    [edge["id"]],
                ).fetchone()
            if existing and merge:
                import_count["skipped"] += 1
                continue
            try:
                cols = [k for k in edge.keys() if k not in ("id",)]
                vals = [edge[k] for k in edge.keys() if k not in ("id",)]
                col_str = ", ".join(["id"] + cols)
                val_str = ", ".join(["?"] * (1 + len(vals)))
                self._conn.execute(
                    f"INSERT INTO ohm_edges ({col_str}) VALUES ({val_str})",
                    [edge["id"]] + vals,
                )
                import_count["edges"] += 1
            except Exception:
                import_count["skipped"] += 1

        # Import observations
        for obs in data.get("observations", []):
            try:
                cols = [k for k in obs.keys() if k not in ("id",)]
                vals = [obs[k] for k in obs.keys() if k not in ("id",)]
                col_str = ", ".join(["id"] + cols)
                val_str = ", ".join(["?"] * (1 + len(vals)))
                self._conn.execute(
                    f"INSERT INTO ohm_observations ({col_str}) VALUES ({val_str})",
                    [obs["id"]] + vals,
                )
                import_count["observations"] += 1
            except Exception:
                import_count["skipped"] += 1

        return import_count

    # ── Edge Versioning ────────────────────────────────────────────────

    def edge_history(self, edge_id: str) -> list[dict[str, Any]]:
        """Get the full history of an edge including supersessions and challenges.

        Edge versioning tracks the lifecycle of an edge:
        - Original creation
        - Confidence updates (by owner only)
        - Challenges (by other agents)
        - Supports (by other agents)
        - Identity evolution (L1 edges: superseded_by chain)

        Args:
            edge_id: The edge to get history for.

        Returns:
            List of events in chronological order, each with:
            type, agent, timestamp, details.
        """
        import json as _json
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")

        edge = self.get_edge(edge_id)
        if edge is None:
            return []

        history = []

        # 1. Original creation
        history.append({
            "type": "created",
            "agent": edge.get("created_by", "unknown"),
            "timestamp": str(edge.get("created_at", "")),
            "edge_type": edge.get("edge_type"),
            "confidence": edge.get("confidence"),
            "layer": edge.get("layer"),
        })

        # 2. Confidence updates — check change feed
        updates = self._conn.execute(
            """SELECT agent_name, occurred_at, new_data
               FROM ohm_change_feed
               WHERE table_name = 'ohm_edges' AND row_id = ?
                 AND operation IN ('UPDATE', 'EVOLVE')
               ORDER BY occurred_at""",
            [edge_id],
        ).fetchall()

        for agent, ts, new_data in updates:
            history.append({
                "type": "updated",
                "agent": agent,
                "timestamp": str(ts),
            })

        # 3. Challenges and supports
        reactions = self._conn.execute(
            """SELECT id, challenge_type, confidence, created_by, created_at, condition
               FROM ohm_edges
               WHERE challenge_of = ?
               ORDER BY created_at""",
            [edge_id],
        ).fetchall()

        for rid, rtype, rconf, ragent, rts, rreason in reactions:
            history.append({
                "type": rtype.lower() if rtype else "reaction",
                "agent": ragent,
                "timestamp": str(rts),
                "confidence": rconf,
                "reason": rreason,
                "reaction_edge_id": rid,
            })

        # 4. Identity evolution chain
        meta = edge.get("metadata")
        if meta:
            try:
                meta_dict = _json.loads(meta) if isinstance(meta, str) else meta
                if meta_dict.get("superseded"):
                    superseded_by = meta_dict.get("superseded_by")
                    history.append({
                        "type": "superseded",
                        "agent": edge.get("created_by", "unknown"),
                        "timestamp": str(edge.get("updated_at", "")),
                        "superseded_by": superseded_by,
                    })
                    # Follow the chain
                    if superseded_by:
                        next_edge = self.get_edge(superseded_by)
                        if next_edge:
                            history.append({
                                "type": "evolved_to",
                                "agent": next_edge.get("created_by", "unknown"),
                                "timestamp": str(next_edge.get("created_at", "")),
                                "edge_id": superseded_by,
                                "provenance": next_edge.get("provenance"),
                            })
            except (ValueError, TypeError):
                pass

        # Sort by timestamp
        history.sort(key=lambda h: h.get("timestamp", ""))
        return history

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


def connect_remote(
    uri: str = "quack:localhost",
    *,
    actor: str = "unknown",
    token: str | None = None,
    token_env: str | None = None,
    alias: str = "remote",
) -> Graph:
    """Connect to a remote OHM graph via Quack protocol.

    Creates a local in-memory DuckDB connection and attaches the remote
    Quack server as a catalog. All graph operations are sent to the
    remote server through Quack.

    Falls back to a direct file connection if Quack is not available
    (using the OHM_DB environment variable or default path).

    Args:
        uri: Quack URI of the remote server (default: quack:localhost).
        actor: Agent name for attribution.
        token: Quack authentication token.
        token_env: Environment variable for the token (default: QUACK_TOKEN).
        alias: Catalog alias for the remote (default: 'remote').

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
        except Exception:
            # Fall back to direct connection
            pass

    # Fallback: direct file connection
    db_path = os.environ.get("OHM_DB", str(Path.home() / ".ohm" / "ohm.duckdb"))
    conn = db_connect(db_path)
    graph = Graph(conn, actor=actor)
    graph.token = token or os.environ.get("OHM_TOKEN")
    return graph
