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

    # ── Discovery (ADR-005) ───────────────────────────────────────────────

    def schema(self) -> dict[str, Any]:
        """Return the current graph schema for agent introspection.

        Returns node types, edge types by layer, and schema version.
        ADR-005: Self-documenting interface for agents.

        Returns:
            Dict with 'node_types', 'edge_types_by_layer', 'schema_version'.
        """
        from ohm.schema import VALID_NODE_TYPES, LAYER_EDGE_TYPES, get_schema_version

        return {
            "node_types": sorted(VALID_NODE_TYPES),
            "edge_types_by_layer": {
                layer: sorted(types) for layer, types in LAYER_EDGE_TYPES.items()
            },
            "schema_version": get_schema_version(self._conn),
        }

    def layers(self) -> list[dict[str, Any]]:
        """Return L1-L4 layer descriptions for agent introspection.

        ADR-005: Self-documenting interface for agents.

        Returns:
            List of layer descriptors with name, sharing, ownership,
            edge_types, and example.
        """
        from ohm.schema import LAYER_EDGE_TYPES

        # Structured layer data derived from LAYER_EDGE_TYPES and CLI defaults
        layer_data = [
            {
                "name": "L1",
                "sharing": "Fully shared",
                "ownership": "Communal",
                "edge_types": sorted(LAYER_EDGE_TYPES["L1"]),
                "example": '"Hungary has a constitution"',
            },
            {
                "name": "L2",
                "sharing": "Shared + attributed",
                "ownership": "Proposing agent",
                "edge_types": sorted(LAYER_EDGE_TYPES["L2"]),
                "example": '"This idea derives from that source"',
            },
            {
                "name": "L3",
                "sharing": "Agent-owned, challengeable",
                "ownership": "Creating agent",
                "edge_types": sorted(LAYER_EDGE_TYPES["L3"]),
                "example": '"Pattern X causes outcome Y conf: 0.94 (agent-alpha)"',
            },
            {
                "name": "L4",
                "sharing": "Agent-owned, visible",
                "ownership": "Forecasting agent",
                "edge_types": sorted(LAYER_EDGE_TYPES["L4"]),
                "example": '"Outcome Z expected conf: 0.65 (agent-beta)"',
            },
        ]
        return layer_data

    def help(self) -> dict[str, Any]:
        """Return a complete introspection guide for this OHM graph.

        ADR-005: Self-documenting interface for agents. Returns everything
        an agent needs to know to use the graph: schema, layers, available
        methods, and usage examples.

        Returns:
            Dict with 'schema', 'layers', 'methods', 'examples', and 'version'.
        """
        return {
            "version": "0.2.0",
            "schema": self.schema(),
            "layers": self.layers(),
            "methods": {
                "write": {
                    "create_node": "Create a new node. Returns full record.",
                    "create_edge": "Create a new edge. Returns full record.",
                    "update_edge": "Update your own edge (confidence, provenance, condition).",
                    "batch_create_nodes": "Create multiple nodes atomically.",
                    "batch_create_edges": "Create multiple edges atomically.",
                },
                "read": {
                    "get_node": "Get a single node by ID.",
                    "get_edge": "Get a single edge by ID.",
                    "find_or_create_node": "Find node by label, or create if missing.",
                    "search_nodes": "Search nodes by label or content text.",
                    "search_edges": "Search edges with filters (layer, type, confidence).",
                },
                "analysis": {
                    "neighborhood": "Bounded-depth graph traversal from a node.",
                    "path": "Shortest path between two nodes.",
                    "impact": "Downstream failure impact analysis.",
                    "confidence": "Full provenance and challenge audit for an edge.",
                    "stats": "Aggregate graph statistics.",
                    "health": "Graph structural health diagnostics.",
                    "contradictions": "Detect opposing L3 assertions about the same subject.",
                    "stale_edges": "Find edges with decayed confidence below threshold.",
                    "composite_score": "Compute composite decision score for a node.",
                    "trend": "Detect temporal trends in observations.",
                },
                "collaboration": {
                    "challenge": "Challenge an existing edge (ADR-003).",
                    "support": "Support an existing edge (ADR-003).",
                    "observe": "Record an observation on a node.",
                    "register_agent": "Register this agent in the graph.",
                    "set_focus": "Set the current focus for this agent.",
                },
            },
            "examples": {
                "create_and_query": [
                    'node = graph.create_node(label="Pasture Health", node_type="concept")',
                    'edge = graph.create_edge(from_node="a", to_node="b", edge_type="CAUSES", layer="L3")',
                    'results = graph.neighborhood("a", depth=2)',
                ],
                "challenge_workflow": [
                    'graph.challenge(edge_id, reason="Insufficient evidence", confidence=0.3)',
                    'graph.support(edge_id, reason="Confirmed by satellite data", confidence=0.9)',
                    'audit = graph.confidence(edge_id)',
                ],
                "discovery": [
                    'schema = graph.schema()  # node types, edge types, version',
                    'layers = graph.layers()  # L1-L4 descriptions',
                    'help_info = graph.help()  # this complete guide',
                ],
            },
        }

    def status(self) -> dict[str, Any]:
        """Return graph status: node count, edge count, schema version, active agents.

        ADR-005: SDK parity with `ohm graph status`.

        Returns:
            Dict with total_nodes, total_edges, total_observations,
            active_agents, challenge_ratio, schema_version.
        """
        from ohm.queries import query_stats
        from ohm.schema import get_schema_version

        stats = query_stats(self._conn)
        return {
            "total_nodes": stats["total_nodes"],
            "total_edges": stats["total_edges"],
            "total_observations": stats["total_observations"],
            "active_agents": stats["active_agents"],
            "challenge_ratio": stats["challenge_ratio"],
            "schema_version": get_schema_version(self._conn),
        }

    def upgrade(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Apply pending schema migrations.

        ADR-005: SDK parity with `ohm graph upgrade`.

        Args:
            dry_run: If True, show pending migrations without applying.

        Returns:
            Dict with current_version, target_version, applied, pending.
        """
        from ohm.schema import SCHEMA_VERSION, MIGRATIONS, get_schema_version, initialize_schema

        current = get_schema_version(self._conn)
        pending = [(v, d) for v, d, _ in MIGRATIONS if current < v]

        if dry_run:
            return {
                "current_version": current,
                "target_version": SCHEMA_VERSION,
                "pending": [{"version": v, "description": d} for v, d in pending],
                "applied": False,
            }

        if not pending:
            return {
                "current_version": current,
                "target_version": SCHEMA_VERSION,
                "pending": [],
                "applied": False,
            }

        initialize_schema(self._conn)
        new_version = get_schema_version(self._conn)
        return {
            "current_version": new_version,
            "target_version": SCHEMA_VERSION,
            "pending": [],
            "applied": True,
        }

    def query(
        self,
        text: str | None = None,
        *,
        filter_type: str | None = None,
        layer: str | None = None,
        owner: str | None = None,
        confidence_min: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Natural language or structured graph query.

        ADR-005: SDK parity with `ohm graph query`.

        Args:
            text: Freeform text to search in labels and content.
            filter_type: Edge type filter for structured queries.
            layer: Layer filter (L1-L4).
            owner: Filter by creating agent.
            confidence_min: Minimum confidence threshold.
            limit: Maximum results (default 100).

        Returns:
            List of matching node or edge records.
        """
        if filter_type or layer or owner or confidence_min is not None:
            return self.search_edges(
                layer=layer, edge_type=filter_type,
                confidence_min=confidence_min, limit=limit,
            )
        if text:
            return self.search_nodes(text, limit=limit)
        # No filters: return recent nodes
        result = self._conn.execute(
            "SELECT * FROM ohm_nodes ORDER BY created_at DESC LIMIT ?", [limit],
        )
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

    def apply_decay(
        self,
        *,
        half_life_days: float = 30.0,
        min_confidence: float = 0.1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply confidence decay to stale edges.

        ADR-005: SDK parity with `ohm graph decay`.

        Args:
            half_life_days: Days until confidence halves (default 30).
            min_confidence: Floor for decayed confidence (default 0.1).
            dry_run: If True, show what would decay without modifying.

        Returns:
            Dict with decayed_count, affected_edges, summary.
        """
        from ohm.methods import apply_confidence_decay

        return apply_confidence_decay(
            self._conn,
            half_life_days=half_life_days,
            min_confidence=min_confidence,
            dry_run=dry_run,
        )

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
        urgency: str | None = None,
        priority: str | None = None,
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
            urgency=urgency,
            priority=priority,
        )

    def create_edge(
        self,
        *,
        from_node: str,
        to_node: str,
        edge_type: str,
        layer: str = "L3",
        confidence: float = 0.7,
        probability: float | None = None,
        condition: str | None = None,
        provenance: str | None = None,
        metadata: dict[str, Any] | None = None,
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
            probability=probability,
            condition=condition,
            provenance=provenance,
            metadata=metadata,
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

    def threat_cluster(
        self,
        ioc_node_id: str,
        *, edge_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find all alerts sharing a given IOC (Indicator of Compromise).

        Traverses THREAT_CLUSTER edges from the IOC node to find all related
        alerts — used in cybersecurity incident response to correlate IOCs
        across multiple alerts.
        """
        from ohm.queries import query_threat_cluster

        return query_threat_cluster(self._conn, ioc_node_id, edge_type=edge_type)

    # ── Cybersecurity: Source Reliability ──────────────────────────────

    def record_outcome(
        self,
        *,
        source_agent: str,
        claim_node: str,
        outcome: bool,
    ) -> dict[str, Any]:
        """Record whether a source agent's claim was correct or incorrect.

        Stores an outcome observation on the claim node. Use this to build
        a reliability history for each source, enabling source_reliability()
        to compute P(accurate) and false_positive_rate.

        Example:
            g.record_outcome(source_agent=edr_node, claim_node=alert_node, outcome=False)
            # EDR was wrong about this alert (false positive)

            g.record_outcome(source_agent=siem_node, claim_node=alert_node, outcome=True)
            # SIEM was correct about this alert

        Args:
            source_agent: Agent node ID that made the claim.
            claim_node: Node ID of the claim being evaluated.
            outcome: True if the claim was correct, False if incorrect.

        Returns:
            Dict with source_agent, claim_node, outcome, and recorded_by.
        """
        from ohm.queries import query_record_outcome

        return query_record_outcome(
            self._conn,
            source_agent=source_agent,
            claim_node=claim_node,
            outcome=outcome,
            recorded_by=self.actor,
        )

    def source_reliability(
        self,
        source_agent: str,
    ) -> dict[str, Any]:
        """Compute source reliability metrics from historical outcomes.

        Returns P(accurate), false_positive_rate, and outcome counts for the
        given source agent. Sources with high false_positive_rate should be
        downweighted in composite scores.

        Example:
            g.source_reliability(edr_node)
            → {p_accurate: 0.7, false_positive_rate: 0.3, total_outcomes: 100, ...}

        Args:
            source_agent: Agent node ID to evaluate.

        Returns:
            Dict with P(accurate), false_positive_rate, total_outcomes,
            accurate_count, false_positive_count.
        """
        from ohm.queries import query_source_reliability

        return query_source_reliability(self._conn, source_agent)
        from ohm.queries import query_source_reliability

        return query_source_reliability(self._conn, source_agent)

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

    def confidence_chain(self, node_id: str, *, max_depth: int = 5) -> dict[str, Any]:
        """Trace all incoming evidence edges to compute aggregate confidence.

        Walks incoming L2/L3 evidence edges recursively to build an evidence
        tree and computes aggregate confidence. Universal substrate method —
        works for any domain.

        Args:
            node_id: The node to trace evidence for.
            max_depth: Maximum chain depth (default 5).

        Returns:
            Dict with evidence_chain, aggregate_confidence, evidence_count.
        """
        from ohm.queries import query_confidence_chain

        return query_confidence_chain(self._conn, node_id, max_depth=max_depth)

    def agent_state(self, agent_name: str | None = None) -> list[dict[str, Any]]:
        """Query agent state."""
        from ohm.queries import query_agent_state

        return query_agent_state(self._conn, agent_name=agent_name)

    def stats(self) -> dict[str, Any]:
        """Graph statistics — edge counts by layer/type, node counts, challenge ratio."""
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

    def composite_score(
        self,
        node_id: str,
        *,
        observation_weight: float = 0.5,
        evidence_weight: float = 0.5,
        method: str = "arithmetic",
        baseline: float = 1.0,
        temporal_decay_hours: float | None = None,
    ) -> dict[str, Any]:
        """Compute a composite decision score combining observations and evidence.

        Universal substrate method — works for any domain.

        Two composition methods:
        - 'arithmetic': weighted arithmetic mean (default, backwards compatible)
        - 'geometric': geometric mean for multiplicative factors (demand forecasting)

        For geometric mode with baseline:
        - Values are treated as multipliers from baseline
        - baseline=1.0 means values are 1.0 = no change, 2.0 = double
        - Result is expressed as a multiplier from baseline

        Temporal decay:
        - When temporal_decay_hours is set, observation values are weighted by
          0.5^(age_hours / temporal_decay_hours). Stale observations contribute less.
        - Retail: temporal_decay_hours=4.0 (weather relevant for ~4 hours)
        - Cattle: temporal_decay_hours=168.0 (NDVI relevant for ~7 days)

        Args:
            node_id: The node to score.
            observation_weight: Weight for observation signal (0-1).
            evidence_weight: Weight for evidence signal (0-1).
            method: 'arithmetic' (default) or 'geometric' (multiplicative).
            baseline: Baseline for multiplicative mode (default 1.0).
            temporal_decay_hours: Half-life in hours for temporal decay.
                None (default) disables temporal weighting.

        Returns:
            Dict with composite_score, observation_score, evidence_score,
            observation_count, evidence_count, method, baseline,
            and temporal_decay_hours.
        """
        from ohm.methods import composite_score as _composite_score

        return _composite_score(
            self._conn, node_id,
            observation_weight=observation_weight, evidence_weight=evidence_weight,
            method=method, baseline=baseline,
            temporal_decay_hours=temporal_decay_hours,
        )

    def decay_observations(
        self,
        node_id: str | None = None,
        *,
        temporal_decay_hours: float = 4.0,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Compute time-decayed observation values using exponential half-life.

        For each observation, computes an effective value weighted by recency.
        Decay formula: effective_weight = 0.5^(age_hours / temporal_decay_hours).

        In dry_run mode, returns what would change without modifying the database.

        Args:
            node_id: Optional node ID to filter. None = all observations.
            temporal_decay_hours: Half-life in hours (default 4.0).
            dry_run: If True, return what would change without modifying data.

        Returns:
            List of dicts with id, node_id, original_value, decayed_value,
            age_hours, decay_factor, and sigma.
        """
        from ohm.methods import decay_observations as _decay_observations

        return _decay_observations(
            self._conn, node_id,
            temporal_decay_hours=temporal_decay_hours,
            dry_run=dry_run,
        )

    def expiring_soon(
        self,
        *,
        product_type: str | None = None,
        days: int = 5,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Find batches expiring within a given number of days.

        Uses BATCH_EXPIRES_BEFORE edges with expires_at metadata.
        Returns batches sorted by expiry date (soonest first).

        Retail scenario: inventory agent tracks delivery batches and
        alerts when they approach expiry.

        Args:
            product_type: Optional filter by product type (e.g., 'dairy', 'produce').
            days: Look-ahead window in days (default 5).
            limit: Maximum results to return.

        Returns:
            List of dicts with batch_id, product_type, expires_at,
            days_until_expiry, from_node, to_node, and metadata.
        """
        from ohm.queries import query_find_expiring_batches

        return query_find_expiring_batches(
            self._conn,
            product_type=product_type,
            days=days,
            limit=limit,
        )

    def detect_trend(
        self, node_id: str, *, window_days: int = 60, min_observations: int = 3,
    ) -> dict[str, Any]:
        """Detect temporal trends in observations for a node.

        Uses linear regression over observations within the window.
        Universal substrate method — works for any domain.

        Args:
            node_id: The node to analyze.
            window_days: Lookback window in days (default 60).
            min_observations: Minimum observations needed (default 3).

        Returns:
            Dict with trend (rising/falling/stable), slope_per_day, r_squared.
        """
        from ohm.methods import detect_trend as _detect_trend

        return _detect_trend(
            self._conn, node_id, window_days=window_days, min_observations=min_observations,
        )

    def rules_out(
        self,
        *,
        from_node: str,
        to_node: str,
        confidence: float = 0.9,
        layer: str = "L3",
        condition: str | None = None,
        provenance: str | None = None,
    ) -> dict[str, Any]:
        """Create a NEGATES edge indicating a finding rules out a condition.

        Convenience method for medical diagnosis: 'fever_absent NEGATES malaria'.
        Semantically different from a low-confidence SUPPORTS — absence of a finding
        actively rules out a condition rather than weakly supporting it.

        Args:
            from_node: The finding node (e.g., 'fever_absent').
            to_node: The condition node being ruled out (e.g., 'malaria').
            confidence: How confident the ruling-out is (default 0.9).
            layer: Edge layer (default L3).
            condition: Optional condition string.
            provenance: Optional provenance string.

        Returns:
            The created NEGATES edge record.
        """
        return self.create_edge(
            from_node=from_node,
            to_node=to_node,
            edge_type="NEGATES",
            layer=layer,
            confidence=confidence,
            condition=condition,
            provenance=provenance,
        )

    def differential_diagnosis(
        self, node_id: str, *, max_depth: int = 3,
    ) -> list[dict[str, Any]]:
        """Return candidate diagnoses for a patient node, ranked by evidence.

        Walks incoming evidence edges to find candidate conditions, then
        excludes any conditions ruled out by NEGATES edges. Results sorted
        by composite_score descending, with ruled-out conditions at the end.

        Args:
            node_id: The patient/finding node to diagnose.
            max_depth: Maximum traversal depth for evidence chain.

        Returns:
            List of dicts with node_id, label, type, composite_score,
            ruled_out (bool), ruled_out_by (list of edge ids).
        """
        from ohm.methods import differential_diagnosis as _dd

        return _dd(self._conn, node_id, max_depth=max_depth)

    def compound_confidence(
        self,
        observations: list[dict[str, Any]],
        *,
        correlation: float = 0.0,
    ) -> dict[str, Any]:
        """Combine multiple confidence values accounting for correlation.

        When observations are independent (correlation=0.0), confidences compound
        multiplicatively. When perfectly correlated (correlation=1.0), only the
        strongest evidence matters. Values between interpolate.

        Critical for medical diagnosis: two findings from the same modality
        are correlated and shouldn't double-count evidence, while findings from
        different modalities are independent and should compound.

        Args:
            observations: List of dicts with 'confidence' key (0-1).
            correlation: 0.0 = independent, 1.0 = perfectly correlated.

        Returns:
            Dict with compound_confidence, method, correlation, observation_count.
        """
        from ohm.methods import compound_confidence as _cc

        return _cc(observations, correlation=correlation)

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

    def cascade_scenario(
        self,
        node_id: str,
        *,
        failure_probability: float = 1.0,
        max_depth: int = 10,
    ) -> list[dict[str, Any]]:
        """Monte Carlo-style cascade through downstream graph from a node.

        Starting from *node_id* with *failure_probability*, walks downstream
        through CAUSES, EXPECTED_LIKELIHOOD, DEPENDS_ON, and THREATENS edges.
        Each downstream node's failure probability is computed as:

            P_downstream = P_upstream × edge.probability (or edge.confidence)

        Returns all downstream nodes with computed failure probabilities and
        the path chain that leads to each.

        Example:
            g.cascade_scenario(supplier_node, failure_probability=0.3)
            → {node: 'factory_a', failure_probability: 0.28, path: ['supplier_a']}
            → {node: 'distribution_b', failure_probability: 0.19, path: [...]}

        Args:
            node_id: Starting node (e.g., supplier that might fail).
            failure_probability: Probability that the starting node fails (0.0-1.0).
            max_depth: Maximum traversal depth.

        Returns:
            List of dicts with node_id, node_label, node_type, failure_probability,
            depth, and path.
        """
        from ohm.queries import query_cascade_scenario

        return query_cascade_scenario(
            self._conn,
            node_id,
            failure_probability=failure_probability,
            max_depth=max_depth,
        )

    def what_if(
        self,
        edge_id: str,
        *,
        max_depth: int = 10,
    ) -> dict[str, Any]:
        """Dry-run: what happens downstream if this edge's event occurs?

        Treats the edge's to_node as the failure origin with probability
        equal to the edge's probability (or confidence). Returns the cascade
        analysis without modifying the graph.

        Example:
            g.what_if(edge_id)
            → {trigger_edge: {...}, trigger_probability: 0.2,
               downstream_impact: [...], affected_nodes: 5}

        Args:
            edge_id: The edge whose event we're simulating.
            max_depth: Maximum traversal depth.

        Returns:
            Dict with trigger_edge, trigger_probability, downstream_impact,
            and affected_nodes count.
        """
        from ohm.queries import query_what_if

        return query_what_if(self._conn, edge_id, max_depth=max_depth)

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
        node_type: str | None = None,
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
            node_type: Filter to changes affecting nodes of this type (e.g., 'concept').
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
            node_type=node_type,
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

    # ── Customer Support: Handoff, Escalation, Provenance ──────────────

    def handoff(
        self,
        *,
        from_agent: str,
        to_agent: str,
        ticket_node: str,
        reason: str,
        edge_type: str = "TRANSFERRED_TO",
        confidence: float = 0.8,
    ) -> dict[str, Any]:
        """Transfer a ticket between agents with full context tracking.

        Creates a TRANSFERRED_TO (default), ESCALATED_TO, or DELEGATED_TO
        edge from the from_agent to the to_agent, and returns the full
        handoff chain for the ticket.

        Example:
            g.handoff(from_agent=agent_a, to_agent=agent_b,
                      ticket_node=ticket, reason="Customer needs specialist")
            → {edge: {...}, handoff_chain: [...]}

        Args:
            from_agent: Agent node ID transferring from.
            to_agent: Agent node ID transferring to.
            ticket_node: The ticket/case node being handed off.
            reason: Reason for the handoff.
            edge_type: TRANSFERRED_TO, ESCALATED_TO, or DELEGATED_TO.
            confidence: Confidence for the edge (default 0.8).

        Returns:
            Dict with the created edge and the full handoff chain.
        """
        from ohm.queries import query_handoff

        return query_handoff(
            self._conn,
            from_agent=from_agent,
            to_agent=to_agent,
            ticket_node=ticket_node,
            reason=reason,
            edge_type=edge_type,
            confidence=confidence,
            created_by=self.actor,
        )

    def escalate(
        self,
        *,
        ticket_node: str,
        to_tier: str,
        reason: str,
        from_agent: str | None = None,
        confidence: float = 0.9,
    ) -> dict[str, Any]:
        """Escalate a ticket to a higher tier with urgency.

        Creates an ESCALATED_TO edge and sets the ticket's urgency to 'high'.
        Returns the escalation edge and the updated ticket info.

        Example:
            g.escalate(ticket_node=ticket, to_tier=tier2,
                       reason="SLA breach imminent")
            → {edge: {...}, ticket: {urgency: 'high', ...}}

        Args:
            ticket_node: The ticket/case node being escalated.
            to_tier: Agent node ID or tier identifier to escalate to.
            reason: Reason for the escalation.
            from_agent: Agent node ID escalating from (optional).
            confidence: Confidence for the edge (default 0.9).

        Returns:
            Dict with the created edge and updated ticket info.
        """
        from ohm.queries import query_escalate

        return query_escalate(
            self._conn,
            ticket_node=ticket_node,
            to_tier=to_tier,
            reason=reason,
            from_agent=from_agent,
            confidence=confidence,
            created_by=self.actor,
        )

    def ticket_provenance(
        self,
        ticket_node: str,
        *,
        max_depth: int = 10,
    ) -> list[dict[str, Any]]:
        """Show the complete handoff and state history for a ticket.

        Follows TRANSFERRED_TO, ESCALATED_TO, DELEGATED_TO edges and
        state machine edges (OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY,
        CLOSED_BY) to reconstruct the full provenance chain.

        Example:
            g.ticket_provenance(ticket_node=ticket)
            → [{edge_type: 'OPENED_BY', from_label: 'agent_a', ...},
               {edge_type: 'TRANSFERRED_TO', from_label: 'agent_a', ...}]

        Args:
            ticket_node: The ticket/case node.
            max_depth: Maximum traversal depth.

        Returns:
            List of provenance records ordered chronologically.
        """
        from ohm.queries import query_ticket_provenance

        return query_ticket_provenance(
            self._conn, ticket_node, max_depth=max_depth,
        )

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
