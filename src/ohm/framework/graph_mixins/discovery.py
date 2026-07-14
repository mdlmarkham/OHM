"""Discovery Graph mixin (ADR-005): schema introspection, search."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class DiscoveryGraphMixin(GraphMixinBase):
    """schema, search_nodes, search_edges, _resolve_label_or_id, etc."""

    def schema(self) -> dict[str, Any]:
        """Return the current graph schema for agent introspection.

        Returns node types, edge types by layer, and schema version.
        ADR-005: Self-documenting interface for agents.

        Returns:
            Dict with 'node_types', 'edge_types_by_layer', 'schema_version',
            'observation_types', 'provenances', 'visibilities', and 'guide'.
        """
        from ohm.schema import (
            VALID_NODE_TYPES,
            LAYER_EDGE_TYPES,
            VALID_OBSERVATION_TYPES,
            VALID_OBSERVATION_SOURCES,
            VALID_VISIBILITIES,
            VALID_PROVENANCES,
            get_schema_version,
        )

        return {
            "node_types": sorted(VALID_NODE_TYPES),
            "edge_types_by_layer": {layer: sorted(types) for layer, types in LAYER_EDGE_TYPES.items()},
            "observation_types": sorted(VALID_OBSERVATION_TYPES),
            "observation_sources": sorted(VALID_OBSERVATION_SOURCES),
            "visibilities": sorted(VALID_VISIBILITIES),
            "provenances": sorted(VALID_PROVENANCES),
            "schema_version": get_schema_version(self._conn),
        }

    def guide(self) -> dict[str, Any]:
        """Return a usage guide for agents who want to use OHM effectively.

        Includes when to use each node type, edge type, and endpoint,
        plus L0 thinking layer guidance.

        Returns:
            Dict with 'overview', 'writing', 'reading', 'L0_thinking_layer',
            'node_type_guide', 'edge_type_guide', 'cross_link_rule'.
        """
        import requests

        r = requests.get(f"{self.url}/schema", headers=self._headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("guide", data)

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
                "name": "L0",
                "sharing": "Agent-owned, unreliable",
                "ownership": "Creating agent",
                "edge_types": sorted(LAYER_EDGE_TYPES["L0"]),
                "example": '"I have a hunch about this pattern"',
            },
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
                    "audit = graph.confidence(edge_id)",
                ],
                "discovery": [
                    "schema = graph.schema()  # node types, edge types, version",
                    "layers = graph.layers()  # L1-L4 descriptions",
                    "help_info = graph.help()  # this complete guide",
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

    def _resolve_label_or_id(self, node_id_or_label: str, *, create_if_missing: bool = False) -> str:
        """Resolve a node_id or label to a node_id.

        Heuristic: if the string matches UUID pattern (36 chars with 4 hyphens),
        treat as ID. Otherwise search by label first, then try as ID.

        Args:
            node_id_or_label: Node ID or label.
            create_if_missing: If True and input is a label (not ID), create the node.

        Returns:
            The resolved node_id.

        Raises:
            NodeNotFoundError: If not found and create_if_missing=False.
        """

        if len(node_id_or_label) == 36 and node_id_or_label.count("-") == 4:
            return node_id_or_label

        row = self._conn.execute("SELECT id FROM ohm_nodes WHERE label = ?", [node_id_or_label]).fetchone()
        if row:
            return row[0]

        row = self._conn.execute("SELECT id FROM ohm_nodes WHERE id = ?", [node_id_or_label]).fetchone()
        if row:
            return row[0]

        if create_if_missing:
            result = self.create_node(label=node_id_or_label)
            return result["id"]

        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"No node found with label '{node_id_or_label}'")

    def upgrade(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Apply pending schema migrations.

        ADR-005: SDK parity with `ohm graph upgrade`.

        Args:
            dry_run: If True, show pending migrations without applying.

        Returns:
            Dict with current_version, target_version, applied, pending.
        """
        from ohm.schema import SCHEMA_VERSION, MIGRATIONS, get_schema_version, initialize_schema

        def _version_tuple(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split("."))

        current = get_schema_version(self._conn)
        current_key = _version_tuple(current)
        pending = [(v, d) for v, d, _ in MIGRATIONS if current_key < _version_tuple(v)]

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
                layer=layer,
                edge_type=filter_type,
                confidence_min=confidence_min,
                limit=limit,
            )
        if text:
            return self.search_nodes(text, limit=limit)
        # No filters: return recent nodes
        result = self._conn.execute(
            "SELECT * FROM ohm_nodes ORDER BY created_at DESC LIMIT ?",
            [limit],
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
