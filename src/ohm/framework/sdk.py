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

from datetime import datetime, timezone
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


class Graph(DataProductsGraphMixin, ChangeFeedGraphMixin, SubstrateComputationGraphMixin, DiscoveryExportGraphMixin, EdgeVersioningGraphMixin, CustomerSupportGraphMixin, TemporalGraphMixin, SubstrateGraphMixin, CybersecurityGraphMixin):
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

    # ── Discovery (ADR-005) ───────────────────────────────────────────────

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
        priority: str | None = None,
        url: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        utility_scale: str | float | None = None,
        utility_usd_per_day: float | None = None,
        utility_currency: str | None = None,
        current_best_action: str | None = None,
        action_alternatives: list[str] | None = None,
        connects_to: list[str] | None = None,
        source_tier: str | None = None,
        source_author: str | None = None,
        source_institution: str | None = None,
        data_origin: str | None = None,
    ) -> dict[str, Any]:
        """Create a node and return its full record.

        The node ID is auto-generated from the label (lowercased, spaces→underscores,
        with a short unique suffix). Returns the complete node record including
        all fields (id, label, type, content, created_by, created_at, etc.).

        For decision nodes (node_type='decision'), set utility_scale to a numeric
        0-1 value or one of {'best' (1.0), 'neutral' (0.5), 'worst' (0.0)},
        utility_usd_per_day (dollar-valued payoff), and action_alternatives to
        enable VoI analysis and game-theoretic payoffs.

        For cross-link-required node types (pattern, idea, task, decision, and
        the forward-compat synthesis/observation/interpretation/challenge types),
        pass `connects_to=[existing_node_id, ...]` to satisfy the OHM-tjzh /
        ADR-018 cross-link requirement. Each id must already exist in the graph.

        Args:
            tags: Optional tags for categorization and discovery.
            metadata: Optional structured key-value data (JSON dict).
            source_tier: Optional quality tier for the source (ADR-028). One of
                raw/unverified/preliminary/official/verified. When set, confidence
                must not exceed the tier's ceiling. None means tier not assessed
                (no ceiling applied — backward compatible).
            source_author: Optional original author of the source (ADR-033).
            source_institution: Optional institution the author belongs to (ADR-033).
            data_origin: Optional data origin type (ADR-033). One of
                ugc/peer_reviewed/government/news_wire/sensor/agent_synthesis/expert/unknown.
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
            priority=priority,
            url=url,
            tags=tags,
            metadata=metadata,
            utility_scale=utility_scale,
            utility_usd_per_day=utility_usd_per_day,
            utility_currency=utility_currency,
            current_best_action=current_best_action,
            action_alternatives=action_alternatives,
            connects_to=connects_to,
            source_tier=source_tier,
            source_author=source_author,
            source_institution=source_institution,
            data_origin=data_origin,
        )

    def scratch(
        self,
        content: str,
        *,
        tags: list[str] | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write an L0 thinking fragment (OHM-a5rz.5).

        Minimal write: just content. Auto-generates id, label (first 80 chars),
        type='fragment', confidence=0.0, provenance='scratch'. Extracts URLs
        from content. Fragments are exempt from cross-link requirements.

        Args:
            content: The fragment text (hunch, question, observation).
            tags: Optional tags for categorization.
            connects_to: Optional existing node ids to link this fragment to.

        Returns:
            The created fragment node dict.
        """
        from ohm.queries import scratch

        return scratch(
            self._conn,
            content=content,
            created_by=self.actor,
            tags=tags,
            connects_to=connects_to,
        )

    def link_fragment(
        self,
        fragment_id: str,
        target_id: str,
        edge_type: str = "REFINES_FRAG",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Link two fragments with an L0 edge (OHM-a5rz.11).

        Args:
            fragment_id: Source fragment node id.
            target_id: Target fragment node id.
            edge_type: One of REFINES_FRAG, CONTRADICTS_FRAG, INSPIRED_BY.
            note: Optional note about the link.

        Returns:
            The created edge record.
        """
        from ohm.queries import create_edge

        return create_edge(
            self._conn,
            from_node=fragment_id,
            to_node=target_id,
            layer="L0",
            edge_type=edge_type,
            created_by=self.actor,
            confidence=0.5,
            provenance="fragment_connect",
            metadata={"note": note} if note else None,
        )

    def resolve_question(self, fragment_id: str) -> dict[str, Any] | None:
        """Mark a question fragment as resolved (OHM-a5rz.12)."""
        from ohm.queries import resolve_question

        return resolve_question(
            self._conn,
            fragment_id=fragment_id,
            resolved_by=self.actor,
        )

    def fragment_resonance(self, min_shared: int = 2, limit: int = 10) -> list[dict[str, Any]]:
        """Detect cross-agent fragment resonance (OHM-a5rz.13).

        Finds fragments from different agents sharing context nodes.
        """
        from ohm.queries import detect_fragment_resonance

        return detect_fragment_resonance(self._conn, min_shared=min_shared, limit=limit)

    def create_edge(
        self,
        *,
        from_node: str,
        to_node: str,
        edge_type: str,
        layer: str = "L3",
        confidence: float = 0.7,
        probability: float | None = None,
        urgency: str | None = None,
        condition: str | None = None,
        provenance: str | None = None,
        metadata: dict[str, Any] | None = None,
        probability_p05: float | None = None,
        probability_p50: float | None = None,
        probability_p95: float | None = None,
        confidence_p05: float | None = None,
        confidence_p50: float | None = None,
        confidence_p95: float | None = None,
        source_tier: str | None = None,
    ) -> dict[str, Any]:
        """Create an edge and return its full record.

        Returns the complete edge record including all fields
        (id, from_node, to_node, layer, edge_type, created_at, etc.).

        Args:
            source_tier: Optional quality tier for the source (ADR-028). One of
                raw/unverified/preliminary/official/verified. When set, confidence
                must not exceed the tier's ceiling. None means tier not assessed
                (no ceiling applied — backward compatible).
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
            urgency=urgency,
            condition=condition,
            provenance=provenance,
            metadata=metadata,
            probability_p05=probability_p05,
            probability_p50=probability_p50,
            probability_p95=probability_p95,
            confidence_p05=confidence_p05,
            confidence_p50=confidence_p50,
            confidence_p95=confidence_p95,
            source_tier=source_tier,
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

    def run_oppositional_review(
        self,
        *,
        target_node_id: str | None = None,
        min_confidence: float = 0.5,
        homogeneity_threshold: float = 0.8,
        min_support_count: int = 2,
        auto_challenge: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Run oppositional review (OHM-jbsr).

        Detects CAUSES edges with homogeneous source_tier/agent support and
        optionally auto-challenges them. Returns flagged_edges, challenged_edges,
        and a review_summary. auto_challenge defaults to False (flag only).
        """
        from ohm.graph.methods import oppositional_review

        return oppositional_review(
            self._conn,
            target_node_id=target_node_id,
            min_confidence=min_confidence,
            homogeneity_threshold=homogeneity_threshold,
            min_support_count=min_support_count,
            auto_challenge=auto_challenge,
            reviewer_agent=self.actor,
            limit=limit,
        )

    def detect_consensus_only(self, edge_id: str) -> dict[str, Any]:
        """Check whether a CAUSES edge's support is consensus-only (OHM-2yq2).

        Returns is_consensus_only, supporting_edges, strongest_tier,
        strongest_ceiling, has_verified_outcome, recommended_ceiling.
        """
        from ohm.queries import detect_consensus_only_support

        return detect_consensus_only_support(self._conn, edge_id=edge_id)

    def fire_verification_nudge(self, edge_id: str, *, reason: str, confidence: float = 0.3) -> dict[str, Any]:
        """Auto-fire a consensus-only challenge nudge on an edge (OHM-2yq2).

        Idempotent: returns the existing CONSENSUS_FLAG nudge if one already
        exists. Creates a CHALLENGED_BY edge with challenge_type='CONSENSUS_FLAG'.
        """
        from ohm.queries import fire_verification_nudge as _fire

        return _fire(
            self._conn,
            edge_id=edge_id,
            reason=reason,
            created_by=self.actor,
            confidence=confidence,
        )

    def fingerprint(self, node_id: str, *, dim: int = 10000, seed: int = 42) -> dict[str, Any]:
        """Compute hyperdimensional fingerprint for a node (OHM-yk7z, ADR-031).

        Returns fingerprint_hex, dimension, seed, method, and component list.
        Pure computation — no DDL changes.
        """
        from ohm.graph.methods import compute_hd_fingerprint

        return compute_hd_fingerprint(self._conn, node_id, dim=dim, seed=seed)

    def hd_similarity_search(
        self,
        node_id: str,
        *,
        threshold: float = 0.65,
        limit: int = 20,
        dim: int = 10000,
        seed: int = 42,
    ) -> list[dict[str, Any]]:
        """Find nodes with similar HD fingerprints (OHM-yk7z, ADR-031).

        Naive all-pairs Hamming similarity. Returns list of dicts sorted
        by hd_similarity descending, filtered by threshold.
        """
        from ohm.graph.methods import hd_similarity_search

        return hd_similarity_search(
            self._conn,
            node_id,
            threshold=threshold,
            limit=limit,
            dim=dim,
            seed=seed,
        )

    def update_hd_fingerprint(self, node_id: str, *, dim: int = 10000, seed: int = 42) -> dict[str, Any]:
        """Compute and persist HD fingerprint for a node (OHM-wvz8.2, ADR-032).

        Stores fingerprint in hd_fingerprint BLOB column for later
        membership search. Returns fingerprint metadata.
        """
        from ohm.graph.queries import update_node_hd_fingerprint

        return update_node_hd_fingerprint(self._conn, node_id, dim=dim, seed=seed)

    def hd_membership_search(
        self,
        query_fingerprint_hex: str,
        *,
        threshold: float = 0.65,
        limit: int = 20,
        node_type: str | None = None,
        dim: int = 10000,
    ) -> list[dict[str, Any]]:
        """Search stored HD fingerprints by Hamming similarity (OHM-wvz8.2).

        Requires fingerprints to be pre-computed via update_hd_fingerprint()
        or batch_update_hd_fingerprints(). Returns nodes sorted by
        hd_similarity descending.
        """
        from ohm.graph.queries import hd_membership_search

        return hd_membership_search(
            self._conn,
            query_fingerprint_hex,
            threshold=threshold,
            limit=limit,
            node_type=node_type,
            dim=dim,
        )

    def batch_update_hd_fingerprints(
        self,
        *,
        dim: int = 10000,
        seed: int = 42,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Bulk-compute HD fingerprints for all nodes missing them (OHM-wvz8.2).

        Iterates nodes where hd_fingerprint IS NULL, computes and stores.
        Returns count of updated and skipped nodes.
        """
        from ohm.graph.queries import batch_update_hd_fingerprints

        return batch_update_hd_fingerprints(self._conn, dim=dim, seed=seed, limit=limit)

    def source_diversity(self, node_id: str, *, max_depth: int = 3) -> dict[str, Any]:
        """Compute source diversity score for a node (OHM-qi6r, ADR-033).

        Weighted Shannon entropy over author, institution, and data origin
        of evidence sources. Falls back to created_by when source_author
        is NULL. Score 0-1 where 1 = maximum diversity.
        """
        from ohm.graph.methods import source_diversity_score

        return source_diversity_score(self._conn, node_id, max_depth=max_depth)

    def detect_emerging_concepts(
        self,
        *,
        residual_mass_threshold: float = 0.5,
        stability_threshold: float = 0.7,
        min_observations: int = 3,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Detect unknown-ingredient emerging concepts (OHM-tlqz, ADR-034).

        Uses HD fingerprint residual mass to find nodes that are not
        well-explained by existing concepts. Stability gate prevents
        premature naming.
        """
        from ohm.graph.methods import detect_unknown_ingredients

        return detect_unknown_ingredients(
            self._conn,
            residual_mass_threshold=residual_mass_threshold,
            stability_threshold=stability_threshold,
            min_observations=min_observations,
            limit=limit,
        )

    def compute_residual_mass(self, node_id: str, *, dim: int = 10000, seed: int = 42) -> dict[str, Any]:
        """Compute HD residual mass for a node (OHM-tlqz, ADR-034)."""
        from ohm.graph.methods import compute_residual_mass

        return compute_residual_mass(self._conn, node_id, dim=dim, seed=seed)

    def update_emerging_concept_score(self, node_id: str, *, dim: int = 10000, seed: int = 42) -> dict[str, Any]:
        """Compute and store emerging concept score (OHM-tlqz, ADR-034)."""
        from ohm.graph.methods import update_emerging_concept_score

        return update_emerging_concept_score(self._conn, node_id, dim=dim, seed=seed)

    def name_emerging_concept(self, node_id: str, new_label: str, *, dim: int = 10000, seed: int = 42) -> dict[str, Any]:
        """Promote an emerging concept with a new label (OHM-tlqz, ADR-034).

        Gated on stability >= 0.7. Raises ValueError if unstable.
        """
        from ohm.graph.methods import promote_emerging_concept

        return promote_emerging_concept(
            self._conn,
            node_id=node_id,
            new_label=new_label,
            promoted_by=self.actor,
            dim=dim,
            seed=seed,
        )

    def sign_node(self, node_id: str, *, key: bytes | None = None, algorithm: str = "hmac-sha256", key_id: str = "default") -> dict[str, Any]:
        """Sign a node write with HMAC-SHA256 (OHM-enwb, ADR-035)."""
        from ohm.graph.queries import sign_node_write

        signing_key = key or self._signing_key
        if not signing_key:
            raise ValueError("No signing key available")
        return sign_node_write(self._conn, node_id, key=signing_key, algorithm=algorithm, key_id=key_id)

    def sign_edge(self, edge_id: str, *, key: bytes | None = None, algorithm: str = "hmac-sha256", key_id: str = "default") -> dict[str, Any]:
        """Sign an edge write with HMAC-SHA256 (OHM-enwb, ADR-035)."""
        from ohm.graph.queries import sign_edge_write

        signing_key = key or self._signing_key
        if not signing_key:
            raise ValueError("No signing key available")
        return sign_edge_write(self._conn, edge_id, key=signing_key, algorithm=algorithm, key_id=key_id)

    def verify_node(self, node_id: str, *, key: bytes | None = None) -> dict[str, Any]:
        """Verify a node's write signature (OHM-enwb, ADR-035)."""
        from ohm.graph.queries import verify_node_write

        signing_key = key or self._signing_key
        if not signing_key:
            raise ValueError("No signing key available")
        return verify_node_write(self._conn, node_id, key=signing_key)

    def verify_edge(self, edge_id: str, *, key: bytes | None = None) -> dict[str, Any]:
        """Verify an edge's write signature (OHM-enwb, ADR-035)."""
        from ohm.graph.queries import verify_edge_write

        signing_key = key or self._signing_key
        if not signing_key:
            raise ValueError("No signing key available")
        return verify_edge_write(self._conn, edge_id, key=signing_key)

    def create_suggestion(self, **kwargs) -> dict[str, Any]:
        """Create a suggestion for later triage (OHM-xtzk, ADR-036)."""
        from ohm.graph.queries import create_suggestion

        kwargs.setdefault("created_by", self.actor)
        kwargs.setdefault("source_agent", self.actor)
        return create_suggestion(self._conn, **kwargs)

    def query_suggestions(self, **kwargs) -> list[dict[str, Any]]:
        """Query suggestions by status/method/target (OHM-xtzk, ADR-036)."""
        from ohm.graph.queries import query_suggestions

        return query_suggestions(self._conn, **kwargs)

    def promote_suggestion(self, suggestion_id: str, *, edge_layer: str = "L3") -> dict[str, Any]:
        """Promote a ripe suggestion to a real edge (OHM-xtzk, ADR-036)."""
        from ohm.graph.queries import promote_suggestion

        return promote_suggestion(self._conn, suggestion_id, promoted_by=self.actor, edge_layer=edge_layer)

    def reject_suggestion(self, suggestion_id: str, *, notes: str | None = None) -> dict[str, Any]:
        """Reject a suggestion (OHM-xtzk, ADR-036)."""
        from ohm.graph.queries import reject_suggestion

        return reject_suggestion(self._conn, suggestion_id, rejected_by=self.actor, notes=notes)

    def ripen_suggestions(self, *, dry_run: bool = False, max_age_days: int = 30, ripeness_threshold: float = 0.7) -> dict[str, Any]:
        """Ripen suggestions and optionally auto-promote/expiry (OHM-xtzk, ADR-036)."""
        from ohm.graph.methods import ripen_then_decide

        return ripen_then_decide(self._conn, dry_run=dry_run, max_age_days=max_age_days, ripeness_threshold=ripeness_threshold)

    def set_read_scope(self, scope: dict | None) -> dict[str, Any]:
        """Set this agent's read scope (OHM-ybyb, ADR-037).

        None = full access (backward compat). Scope dict keys: layer, source_tier, node_id, created_by.
        """
        from ohm.boundary import set_agent_read_scope

        return set_agent_read_scope(self._conn, self.actor, scope)

    def get_read_scope(self) -> dict | None:
        """Get this agent's read scope (OHM-ybyb, ADR-037)."""
        from ohm.boundary import get_agent_read_scope

        return get_agent_read_scope(self._conn, self.actor)

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
            "UPDATE ohm_edges SET " + ", ".join(set_clauses) + " WHERE id = ?",
            params,
        )

    def batch_update_edges(
        self,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk update edges with PERT fields (OHM-9iyh).

        Wraps PATCH /edges for auto-populating probability_p05/p50/p95
        from observations or confidence values. Each update dict must
        include ``id`` plus any PERT fields to update.

        Args:
            updates: List of {id, probability_p50?, probability_p05?,
                probability_p95?, confidence?, ...} dicts.

        Returns:
            Dict with updated edges and error details.
        """
        if not updates:
            return {"updated": [], "count": 0}

        set_fields = [
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
        results = []
        errors = []

        for item in updates:
            edge_id = item.get("id")
            if not edge_id:
                errors.append({"error": "missing_id", "item": item})
                continue

            clauses: list[str] = []
            params: list[Any] = []
            for field in set_fields:
                if field in item:
                    clauses.append(f"{field} = ?")
                    params.append(item[field])

            if not clauses:
                errors.append({"error": "no_fields", "edge_id": edge_id})
                continue

            try:
                clauses.append("updated_at = CURRENT_TIMESTAMP")
                clauses.append("updated_by = ?")
                params.append(self.actor)
                params.append(edge_id)
                self._conn.execute(
                    f"UPDATE ohm_edges SET {', '.join(clauses)} WHERE id = ? AND deleted_at IS NULL",
                    params,
                )
                results.append(edge_id)
            except Exception as e:
                errors.append({"error": str(e), "edge_id": edge_id})

        return {"updated": results, "count": len(results), "errors": errors}

    def aggregate_experts(
        self,
        estimates: list[tuple[float, float, float]],
        weights: list[float] | None = None,
    ) -> dict[str, float]:
        """Aggregate multiple expert PERT estimates (OHM-9iyh).

        Each expert provides a (p05, p50, p95) triple. Uses weighted
        mixture-of-experts aggregation accounting for both within-expert
        uncertainty and between-expert disagreement.

        Args:
            estimates: List of (p05, p50, p95) triples from each expert.
            weights: Optional weights per expert (uniform if None).

        Returns:
            Dict with mean, variance, total_variance, p05, p50, p95.
        """
        from ohm.inference.pert import aggregate_mixture_of_experts

        return aggregate_mixture_of_experts(estimates, weights=weights)

    def observe(
        self,
        node_id_or_label: str,
        *,
        obs_type: str = "measurement",
        value: float | None = None,
        baseline: float | None = None,
        sigma: float | None = None,
        source: str = "analysis",
        notes: str | None = None,
        source_name: str | None = None,
        source_url: str | None = None,
        create_if_missing: bool = False,
    ) -> dict[str, Any]:
        """Record an observation on a node. Returns the full observation record.

        Args:
            node_id_or_label: Node ID or label to observe. If label is provided
                and create_if_missing=False (default), will raise if not found.
                If create_if_missing=True, will create the node first.
            obs_type: Type of observation (measurement, anomaly, pattern, etc.).
                Defaults to 'measurement' to match REST API default.
            value: Numeric observation value.
            baseline: Expected/baseline value for comparison.
            sigma: Standard deviation/confidence in the observation.
                If not provided and value is given, auto-computes as 0.1 * (1 - value).
            source: Observation source (analysis, research, conversation, signal).
                Defaults to 'analysis'.
            notes: Free-text notes about the observation.
            source_name: Name of the source agent or system.
            source_url: URL reference for the observation source.
            create_if_missing: If True and node_id_or_label is a label (not an ID),
                create the node first. Default False.

        Returns:
            The observation record.
        """
        from ohm.queries import create_observation

        if sigma is None and value is not None:
            sigma = 0.1 * (1.0 - value)

        resolved_id = self._resolve_label_or_id(node_id_or_label, create_if_missing=create_if_missing)

        return create_observation(
            self._conn,
            node_id=resolved_id,
            obs_type=obs_type,
            value=value,
            baseline=baseline,
            sigma=sigma,
            source=source,
            notes=notes,
            created_by=self.actor,
            source_name=source_name,
            source_url=source_url,
        )

    def set_focus(self, focus: str) -> None:
        """Set the current focus for this agent."""
        from ohm.queries import set_agent_state

        set_agent_state(self._conn, agent_name=self.actor, focus=focus)

    def write_synthesis(
        self,
        cluster_ids: list[str],
        label: str,
        content: str,
        *,
        edge_type: str = "SUPPORTS",
        confidence: float = 0.8,
        sigma: float = 0.1,
        provenance: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write a synthesis: one concept node + L3 edges + observation.

        The core L3 writing primitive. Instead of calling create_node,
        create_edge (×N), and observe separately, this collapses the
        most common agent writing pattern into a single call.

        Args:
            cluster_ids: Node IDs this synthesis connects to.
            label: Short name for the synthesis concept.
            content: Full synthesis text — your reasoning, the pattern you see.
            edge_type: L3 edge type (SUPPORTS, CAUSES, TRANSITIONS_TO,
                APPLIES_TO, INFLUENCES, REFINES). Default SUPPORTS.
            confidence: Your confidence in this synthesis (0-1).
            sigma: Uncertainty in confidence (0-1).
            provenance: How you arrived at this (e.g., 'pattern_analysis').
            tags: Tags for discoverability (e.g., ['AND-OR', 'governance']).

        Returns:
            Dict with node, edges_created (count), and observation.
        """
        from ohm.graph.schema import generate_node_id
        from ohm.validation import validate_identifier
        from ohm.queries import create_node, create_edge, create_observation
        import json as _json

        node_id = generate_node_id(label)
        node_result = create_node(
            self._conn,
            label=label,
            node_type="concept",
            content=content,
            created_by=self.actor,
            provenance=provenance or f"{self.actor}_synthesis",
            confidence=confidence,
        )
        node_id = node_result["id"] if isinstance(node_result, dict) else node_id

        # Add tags if provided
        if tags:
            self._conn.execute(
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
                create_edge(
                    self._conn,
                    from_node=node_id,
                    to_node=safe_cid,
                    layer="L3",
                    edge_type=edge_type,
                    created_by=self.actor,
                    confidence=confidence,
                )
                edges_created += 1
            except Exception:
                continue

        # Record observation on the synthesis node
        obs_result = create_observation(
            self._conn,
            node_id=node_id,
            obs_type="pattern",
            value=confidence,
            sigma=sigma,
            source="synthesis",
            notes=content,
            created_by=self.actor,
        )

        # OHM-8q5d: Aggregate source diversity across cluster_ids
        try:
            from ohm.graph.methods import source_diversity_score

            cluster_diversity = []
            for cid in cluster_ids:
                ds = source_diversity_score(self._conn, cid)
                cluster_diversity.append(ds)
            if cluster_diversity:
                avg_score = sum(d["score"] for d in cluster_diversity) / len(cluster_diversity)
                source_div = {
                    "cluster_diversity": cluster_diversity,
                    "aggregate_score": round(avg_score, 4),
                    "cluster_count": len(cluster_diversity),
                }
            else:
                source_div = {
                    "cluster_diversity": [],
                    "aggregate_score": 0.0,
                    "cluster_count": 0,
                }
        except Exception:
            source_div = None

        return {
            "node": node_result if isinstance(node_result, dict) else {"id": node_id, "label": label},
            "edges_created": edges_created,
            "observation": obs_result,
            "source_diversity": source_div,
        }

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
        for v in values or []:
            value_node = self.find_or_create_node(label=v, node_type="value")
            # Check if edge already exists
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'VALUES' AND created_by = ?",
                [me["id"], value_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"],
                    to_node=value_node["id"],
                    edge_type="VALUES",
                    layer="L1",
                    confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare goals (L1 — identity)
        for g in goals or []:
            goal_node = self.find_or_create_node(label=g, node_type="goal")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'GOALS' AND created_by = ?",
                [me["id"], goal_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"],
                    to_node=goal_node["id"],
                    edge_type="GOALS",
                    layer="L1",
                    confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare capabilities (L1 — identity)
        for c in capabilities or []:
            cap_node = self.find_or_create_node(label=c, node_type="skill")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'CAPABLE_OF' AND created_by = ?",
                [me["id"], cap_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"],
                    to_node=cap_node["id"],
                    edge_type="CAPABLE_OF",
                    layer="L1",
                    confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare interests / subscriptions (L1 — identity)
        for i in interests or []:
            topic_node = self.find_or_create_node(label=i, node_type="topic")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'INTERESTED_IN' AND created_by = ?",
                [me["id"], topic_node["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"],
                    to_node=topic_node["id"],
                    edge_type="INTERESTED_IN",
                    layer="L1",
                    confidence=1.0,
                    provenance="self_declaration",
                )

        # Declare agent subscriptions (L3 — challengeable preference)
        for a in listens_to or []:
            other_agent = self.find_or_create_node(label=a, node_type="agent")
            existing = self._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'LISTENS_TO' AND created_by = ?",
                [me["id"], other_agent["id"], self.actor],
            ).fetchone()
            if not existing:
                self.create_edge(
                    from_node=me["id"],
                    to_node=other_agent["id"],
                    edge_type="LISTENS_TO",
                    layer="L3",
                    confidence=0.7,
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
        result = self._conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [node_id]).fetchone()
        if result is None:
            return None
        columns = [desc[0] for desc in self._conn.description]
        row = dict(zip(columns, result))
        # node_type is the write API field name; DB column is type. Expose both.
        if "type" in row and "node_type" not in row:
            row["node_type"] = row["type"]
        return row

    def node_context(self, node_id: str, *, domain: str | None = None) -> dict[str, Any]:
        """Assemble a complete context envelope for a node (OHM-807).

        Returns all relevant context in one call:
        - node metadata
        - neighborhood (upstream/downstream by layer)
        - recent observations
        - external signal attachments
        - confidence summary

        Domain-specific enrichment (prospects, plans, etc.) is added
        when domain is specified and the domain's tables exist.

        Args:
            node_id: The node ID to get context for.
            domain: Optional domain name for domain-specific enrichment.

        Returns:
            Dict with node, neighborhood, observations, signals, and
            confidence fields. Returns empty structures for missing
            components rather than errors.
        """
        node = self.get_node(node_id)
        if node is None:
            return {"error": "node_not_found", "node_id": node_id}

        # Neighborhood (upstream + downstream, all layers)
        try:
            neighbors = self.neighborhood(node_id, depth=2)
        except Exception:
            neighbors = []

        # Recent observations
        try:
            obs_result = self._conn.execute(
                "SELECT * FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 10",
                [node_id],
            )
            obs_columns = [desc[0] for desc in obs_result.description]
            observations = [dict(zip(obs_columns, row)) for row in obs_result.fetchall()]
        except Exception:
            observations = []

        # External signal attachments (OHM-802)
        signals: list[dict[str, Any]] = []
        try:
            from ohm.graph.queries import get_external_signals

            signals = get_external_signals(self._conn, node_id, domain=domain) if domain else get_external_signals(self._conn, node_id)
        except Exception:
            pass

        # Confidence summary
        try:
            confidence = self.compound_confidence(node_id)
        except Exception:
            confidence = {}

        return {
            "node": node,
            "neighborhood": neighbors,
            "observations": observations,
            "signals": signals,
            "confidence": confidence,
        }

    def get_edge(self, edge_id: str) -> dict[str, Any] | None:
        """Retrieve a single edge by ID.

        Returns the full edge record (id, from_node, to_node, layer, edge_type,
        confidence, condition, provenance, created_by, created_at, challenge_of,
        challenge_type) or None if not found.
        """
        result = self._conn.execute("SELECT * FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
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

    def resolve_node(
        self,
        query: str,
        *,
        node_type: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve a query string to a node via alias matching (OHM-z2gp).

        Normalizes the query label, checks ohm_aliases, and returns the
        first matching node record. Returns None if no match found.

        Args:
            query: Label or alias to search for.
            node_type: Optional — only return nodes of this type.

        Returns:
            Node record dict or None.
        """
        from ohm.queries import resolve_node_by_alias

        result = resolve_node_by_alias(self._conn, query=query)
        if result is None:
            return None
        if node_type and result.get("type") != node_type:
            return None
        return result

    def merge_nodes(
        self,
        keep_id: str,
        merge_id: str,
    ) -> dict[str, Any]:
        """Merge two nodes — re-point edges/observations and soft-delete the
        duplicate (OHM-z2gp).

        Args:
            keep_id: Node ID to keep (canonical).
            merge_id: Node ID to merge away (soft-deleted).

        Returns:
            Dict with keep, merged, edges_repointed, observations_repointed.
        """
        from ohm.queries import merge_nodes as _merge_nodes

        return _merge_nodes(
            self._conn,
            keep_id=keep_id,
            merge_id=merge_id,
            merged_by=self.actor,
        )

    def find_duplicates(
        self,
        *,
        threshold: float = 0.85,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Find duplicate nodes via alias, content hash, and semantic similarity
        (OHM-z2gp).

        Args:
            threshold: Cosine similarity threshold for semantic duplicates
                (default 0.85).
            limit: Max pairs per strategy.

        Returns:
            Dict with alias_collisions, content_hash_collisions,
            semantic_duplicates, and summary.
        """
        from ohm.methods import detect_alias_duplicates, detect_semantic_duplicates

        alias_dups = detect_alias_duplicates(self._conn, limit=limit)
        semantic_dups = detect_semantic_duplicates(self._conn, similarity_threshold=threshold, limit=limit)
        alias_collisions = [d for d in alias_dups if d.get("kind") == "alias_collision"]
        hash_collisions = [d for d in alias_dups if d.get("kind") == "content_hash_collision"]
        return {
            "alias_collisions": alias_collisions,
            "content_hash_collisions": hash_collisions,
            "semantic_duplicates": semantic_dups,
            "summary": {
                "total": len(alias_collisions) + len(hash_collisions) + len(semantic_dups),
                "alias_collisions": len(alias_collisions),
                "content_hash_collisions": len(hash_collisions),
                "semantic_duplicates": len(semantic_dups),
                "threshold": threshold,
            },
        }

    def search_nodes(
        self,
        query: str,
        *,
        limit: int = 20,
        node_type: str | None = None,
        include_l0: bool = False,
    ) -> list[dict[str, Any]]:
        """Search nodes by label or content text.

        Performs a case-insensitive ILIKE search on both label and content.
        Optionally filter by node_type.

        OHM-a5rz.18: L0 fragments are excluded by default. Pass
        include_l0=True to include fragment-type nodes.

        Args:
            query: Text to search for in labels and content.
            limit: Maximum results (default 20).
            node_type: Optional type filter (e.g., 'concept', 'source').
            include_l0: Include fragment-type nodes (default False).

        Returns:
            List of matching node records.
        """
        from ohm.queries import search

        return search(
            self._conn,
            query=query,
            limit=limit,
            node_type=node_type,
            include_l0=include_l0,
        )

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

        sql = "SELECT * FROM ohm_edges WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        result = self._conn.execute(sql, params)
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

    def threat_cluster(
        self,
        ioc_node_id: str,
        *,
        edge_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find all alerts sharing a given IOC (Indicator of Compromise).

        Traverses THREAT_CLUSTER edges from the IOC node to find all related
        alerts — used in cybersecurity incident response to correlate IOCs
        across multiple alerts.
        """
        from ohm.queries import query_threat_cluster

        return query_threat_cluster(self._conn, ioc_node_id, edge_type=edge_type)


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
