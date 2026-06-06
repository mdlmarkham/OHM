"""OHM Agent Integrations — domain-specific SDK wrappers for each agent.

Each agent gets a thin wrapper around the OHM SDK that adds:
- Default attribution (agent name)
- Domain-specific defaults (provenance, confidence, node/edge types)
- Convenience methods for common operations

Usage:
    import ohm.sdk as ohm
    from ohm.integrations import SocratesIntegration

    with ohm.connect("~/.ohm/ohm.duckdb", actor="socrates") as graph:
        socrates = SocratesIntegration(graph)
        socrates.challenge(edge_id, reason="logical flaw", confidence=0.3)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ohm.sdk import Graph


class MetisIntegration:
    """Métis — zettelkasten → OHM nodes and edges.

    Bridges Métis zettelkasten notes to OHM:
    - Note ID → node ID
    - Note content → node content
    - [[wikilinks]] → L2 DERIVES_FROM edges
    - #tags → node tags JSON array
    - Provenance: 'conversation' for Matt-sourced, 'research' for Clio-sourced
    """

    def __init__(self, graph: Graph, source: str = "conversation"):
        self.graph = graph
        self.source = source

    def sync_note(
        self,
        note_id: str,
        title: str,
        content: str,
        *,
        tags: list[str] | None = None,
        wikilinks: list[str] | None = None,
        node_type: str = "idea",
    ) -> dict[str, Any]:
        """Sync a zettelkasten note to OHM.

        Creates or updates a node for the note, then creates DERIVES_FROM
        edges for each [[wikilink]] reference.

        Args:
            note_id: Unique note identifier (used as node ID).
            title: Note title (used as node label).
            content: Note body text.
            tags: List of #tags extracted from the note.
            wikilinks: List of [[wikilink]] target note IDs.
            node_type: OHM node type (default: 'idea').

        Returns:
            The created/updated node record.
        """
        node = self.graph.find_or_create_node(
            label=title,
            node_type=node_type,
            content=content,
            provenance=self.source,
        )

        # Create DERIVES_FROM edges for wikilinks
        if wikilinks:
            for target_id in wikilinks:
                # Ensure target node exists (or create stub)
                self.graph.find_or_create_node(
                    label=target_id,
                    node_type="idea",
                    provenance=self.source,
                )
                self.graph.create_edge(
                    from_node=node["id"],
                    to_node=target_id,
                    edge_type="DERIVES_FROM",
                    layer="L2",
                    provenance=self.source,
                )

        return node

    def batch_sync(
        self,
        notes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Sync multiple notes at once.

        Args:
            notes: List of note dicts with keys: id, title, content,
                   and optional tags, wikilinks.

        Returns:
            List of created/updated node records.
        """
        results = []
        for note in notes:
            result = self.sync_note(
                note_id=note["id"],
                title=note["title"],
                content=note.get("content", ""),
                tags=note.get("tags"),
                wikilinks=note.get("wikilinks"),
            )
            results.append(result)
        return results


class ClioIntegration:
    """Clio — research findings → OHM L3 edges.

    When Clio completes deep research:
    - Source nodes for primary sources
    - L3 CAUSES/CORRELATES_WITH/PREDICTS edges with confidence scores
    - Provenance='research', attribution to Clio agent
    """

    def __init__(self, graph: Graph):
        self.graph = graph

    def add_source(
        self,
        source_id: str,
        title: str,
        *,
        content: str | None = None,
        source_type: str = "primary",
    ) -> dict[str, Any]:
        """Register a research source as an OHM node.

        Args:
            source_id: Unique source identifier.
            title: Source title/citation.
            content: Source abstract or summary.
            source_type: 'primary' or 'secondary'.

        Returns:
            The created node record.
        """
        return self.graph.find_or_create_node(
            label=title,
            node_type="source",
            content=content,
            provenance="research",
        )

    def add_finding(
        self,
        from_source_id: str,
        to_concept_id: str,
        edge_type: str,
        *,
        confidence: float = 0.7,
        condition: str | None = None,
    ) -> dict[str, Any]:
        """Record a research finding as an L3 edge.

        Args:
            from_source_id: Source or concept node ID.
            to_concept_id: Target concept node ID.
            edge_type: L3 edge type (CAUSES, CORRELATES_WITH, PREDICTS, etc.).
            confidence: Clio's confidence in the finding (0.0-1.0).
            condition: Optional condition/context for the edge.

        Returns:
            The created edge record.
        """
        return self.graph.create_edge(
            from_node=from_source_id,
            to_node=to_concept_id,
            edge_type=edge_type,
            layer="L3",
            confidence=confidence,
            condition=condition,
            provenance="research",
        )

    def publish_synthesis(
        self,
        title: str,
        content: str,
        *,
        supporting_edge_ids: list[str],
    ) -> dict[str, Any]:
        """Publish a research synthesis as a node with supporting edges.

        Args:
            title: Synthesis title.
            content: Synthesis body.
            supporting_edge_ids: Edge IDs that support this synthesis.

        Returns:
            The created synthesis node record.
        """
        node = self.graph.create_node(
            label=title,
            node_type="concept",
            content=content,
            provenance="research",
        )
        for edge_id in supporting_edge_ids:
            self.graph.support(edge_id, reason=content, confidence=0.8)
        return node


class HephaestusIntegration:
    """Hephaestus — audit findings → OHM observations.

    When Hephaestus finds issues in code audits:
    - Equipment/system nodes for audited entities
    - Observations (anomaly type) with severity (sigma)
    - L2 REFERENCES edges linking findings to code
    """

    def __init__(self, graph: Graph):
        self.graph = graph

    def register_entity(
        self,
        entity_id: str,
        name: str,
        *,
        entity_type: str = "system",
        content: str | None = None,
    ) -> dict[str, Any]:
        """Register an audited entity as an OHM node.

        Args:
            entity_id: Unique entity identifier.
            name: Entity name.
            entity_type: 'system', 'equipment', or 'code'.
            content: Entity description or code reference.

        Returns:
            The created node record.
        """
        return self.graph.find_or_create_node(
            label=name,
            node_type=entity_type,
            content=content,
            provenance="audit",
        )

    def report_finding(
        self,
        entity_id: str,
        finding_type: str,
        *,
        value: float | None = None,
        sigma: float = 1.0,
        source: str = "audit",
        description: str | None = None,
    ) -> dict[str, Any]:
        """Report an audit finding as an observation.

        Args:
            entity_id: The entity node ID this finding relates to.
            finding_type: Type of finding (anomaly, vulnerability, etc.).
            value: Numeric severity/value.
            sigma: Severity/confidence (higher = more severe).
            source: Source of the finding.
            description: Human-readable description.

        Returns:
            The created observation record.
        """
        return self.graph.observe(
            entity_id,
            obs_type=finding_type,
            value=value,
            sigma=sigma,
            source=source,
        )

    def link_to_code(
        self,
        entity_id: str,
        code_node_id: str,
        *,
        edge_type: str = "REFERENCES",
    ) -> dict[str, Any]:
        """Link an audited entity to its code via L2 edge.

        Args:
            entity_id: The entity node ID.
            code_node_id: The code/repository node ID.
            edge_type: Edge type (default: REFERENCES).

        Returns:
            The created edge record.
        """
        return self.graph.create_edge(
            from_node=entity_id,
            to_node=code_node_id,
            edge_type=edge_type,
            layer="L2",
            provenance="audit",
        )


class SocratesIntegration:
    """Socrates — challenges → OHM CHALLENGED_BY edges.

    When Socrates challenges an argument:
    - Finds the relevant edge in OHM
    - Creates a CHALLENGED_BY edge with reasoning and confidence
    - Preserves the original edge unchanged (ADR-003)

    Challenge categories:
    - logical_flaw: The reasoning contains a logical error
    - insufficient_evidence: The claim lacks sufficient support
    - scope_too_narrow: The claim is true but too narrow in scope
    - alternative_explanation: A better explanation exists
    """

    CHALLENGE_CATEGORIES = frozenset(
        {
            "logical_flaw",
            "insufficient_evidence",
            "scope_too_narrow",
            "alternative_explanation",
        }
    )

    def __init__(self, graph: Graph):
        self.graph = graph

    def challenge(
        self,
        edge_id: str,
        *,
        reason: str,
        category: str = "logical_flaw",
        confidence: float = 0.5,
    ) -> dict[str, Any]:
        """Challenge an existing edge with Socratic reasoning.

        Args:
            edge_id: The edge to challenge.
            reason: Detailed reasoning for the challenge.
            category: Challenge category (must be in CHALLENGE_CATEGORIES).
            confidence: Socrates' confidence in the challenge (0.0-1.0).

        Returns:
            The created CHALLENGED_BY edge record.

        Raises:
            ValueError: If category is not a valid challenge category.
        """
        if category not in self.CHALLENGE_CATEGORIES:
            raise ValueError(f"Invalid challenge category: '{category}'. Must be one of: {', '.join(sorted(self.CHALLENGE_CATEGORIES))}")
        full_reason = f"[{category}] {reason}"
        return self.graph.challenge(edge_id, reason=full_reason, confidence=confidence)

    def review_layer(
        self,
        layer: str = "L3",
        *,
        min_confidence: float = 0.0,
        max_confidence: float = 0.6,
    ) -> list[dict[str, Any]]:
        """Find edges in a layer that may warrant challenge.

        Returns edges below the confidence threshold that Socrates
        should consider challenging.

        Args:
            layer: Layer to review (default: L3).
            min_confidence: Minimum confidence to include.
            max_confidence: Maximum confidence (edges above this are skipped).

        Returns:
            List of edge records that may warrant challenge.
        """
        # Use the SDK's search to find low-confidence edges
        # This is a heuristic — actual challenge decisions are Socrates' judgment
        return self.graph.search_edges(
            layer=layer,
            confidence_min=min_confidence,
            confidence_max=max_confidence,
        )
