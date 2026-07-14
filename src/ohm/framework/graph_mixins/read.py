"""Read Graph mixin: node/edge lookups."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class ReadGraphMixin(GraphMixinBase):
    """get_node, get_edge, find_or_create_node, and other lookups."""

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
