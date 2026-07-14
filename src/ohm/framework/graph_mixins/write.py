"""Write Graph mixin: node/edge creation, observations, mutation helpers."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class WriteGraphMixin(GraphMixinBase):
    """create_node, create_edge, observe, and other mutation helpers."""

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
