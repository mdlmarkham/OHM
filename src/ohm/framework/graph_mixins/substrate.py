"""Substrate Graph mixin: aggregation, compound confidence, substrate-layer ops."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class SubstrateGraphMixin(GraphMixinBase):
    """aggregate, compound_confidence, and related substrate operations."""

    def aggregate(self, node_id: str, *, method: str = "weighted") -> dict[str, Any]:
        """Combine multiple observations on a node into a single value.

        Strategies: weighted (inverse-variance), mean, max_confidence, consensus.
        Same result regardless of caller — substrate method.
        """
        from ohm.methods import aggregate_observations

        return aggregate_observations(self._conn, node_id, method=method)

    def auto_pert_from_observations(self, node_id: str, **kwargs) -> dict[str, Any]:
        """Derive PERT triple from observations on a node.

        Uses empirical percentiles of observation values to auto-generate
        PERT estimates (OHM-8fg9). Requires at least 3 observations.

        Args:
            node_id: Node to derive PERT from.
            bounds: Valid range (default [0, 1]).

        Returns:
            Dict with p05, p50, p95, mean, variance, n, method.
        """
        from ohm.inference.pert import auto_pert_from_observations as _auto_pert

        obs = self._conn.execute(
            "SELECT value FROM ohm_observations WHERE node_id = ? AND value IS NOT NULL AND deleted_at IS NULL ORDER BY created_at",
            [node_id],
        ).fetchall()
        values = [r[0] for r in obs]
        return _auto_pert(values, **kwargs)

    def auto_pert_from_edges(self, node_id: str, **kwargs) -> dict[str, Any]:
        """Derive PERT triple from edge probability distributions.

        Analyzes incoming/outgoing edge probabilities to auto-generate
        PERT estimates (OHM-8fg9). Uses edge probability_p50 values.

        Args:
            node_id: Node whose edges to analyze.
            default_spread: Fallback spread for single probability.

        Returns:
            Dict with p05, p50, p95, mean, variance, n, method.
        """
        from ohm.inference.pert import auto_pert_from_edge_distribution as _auto_pert_edges

        in_probs = self._conn.execute(
            "SELECT probability FROM ohm_edges WHERE to_node = ? AND probability IS NOT NULL AND deleted_at IS NULL",
            [node_id],
        ).fetchall()
        out_probs = self._conn.execute(
            "SELECT probability FROM ohm_edges WHERE from_node = ? AND probability IS NOT NULL AND deleted_at IS NULL",
            [node_id],
        ).fetchall()
        probs = [r[0] for r in in_probs] + [r[0] for r in out_probs]
        return _auto_pert_edges(probs, **kwargs)

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
            self._conn,
            sigma_threshold=sigma_threshold,
            layer=layer,
            limit=limit,
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
            self._conn,
            confidence_threshold=confidence_threshold,
            limit=limit,
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
            self._conn,
            node_id,
            observation_weight=observation_weight,
            evidence_weight=evidence_weight,
            method=method,
            baseline=baseline,
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
            self._conn,
            node_id,
            temporal_decay_hours=temporal_decay_hours,
            dry_run=dry_run,
        )

    def confidence_at(
        self,
        observation_id: str,
        *,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Compute effective confidence for an observation at a point in time (OHM-60pd).

        Wraps ``GET /observation/{id}/confidence`` on the HTTP path and
        ``confidence_at()`` from ``ohm.graph.decay`` on the local path.

        Args:
            observation_id: The observation to evaluate.
            at: ISO 8601 timestamp to evaluate at. Defaults to now.

        Returns:
            Dict with observation_id, effective_confidence, weibull_shape,
            half_life_days, decay_function, decay_profile, age_days,
            and evaluated_at.
        """
        from datetime import datetime, timezone
        from ohm.graph.decay import confidence_at as _confidence_at, decay_profile as _dp, default_weibull_shape
        from ohm.validation import validate_identifier, validate_timestamp

        obs_id = validate_identifier(observation_id, name="observation_id")
        t = None
        if at:
            at = validate_timestamp(at)
            t = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        else:
            t = datetime.now(timezone.utc)

        row = self._conn.execute(
            "SELECT * FROM ohm_observations WHERE id = ? AND deleted_at IS NULL",
            [obs_id],
        ).fetchone()
        if row is None:
            from ohm.exceptions import NodeNotFoundError

            raise NodeNotFoundError(f"Observation {obs_id} not found")
        cols = [d[0] for d in self._conn.description]
        obs = dict(zip(cols, row))

        eff = _confidence_at(obs, t=t)
        shape = obs.get("weibull_shape")
        if shape is None:
            shape = default_weibull_shape(obs.get("type", "_default"))
        hl = obs.get("half_life_days")
        fn = "weibull" if shape is not None else "exponential"

        anchor = obs.get("valid_from") or obs.get("created_at")
        age_days = None
        if anchor is not None:
            if isinstance(anchor, str):
                anchor = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (t - anchor).total_seconds() / 86400.0)

        return {
            "observation_id": obs_id,
            "effective_confidence": round(eff, 6),
            "weibull_shape": shape,
            "half_life_days": hl,
            "decay_function": fn,
            "decay_profile": _dp(hl, shape),
            "age_days": round(age_days, 4) if age_days is not None else None,
            "evaluated_at": t.isoformat(),
        }

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
        self,
        node_id: str,
        *,
        window_days: int = 60,
        min_observations: int = 3,
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
            self._conn,
            node_id,
            window_days=window_days,
            min_observations=min_observations,
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
        self,
        node_id: str,
        *,
        max_depth: int = 3,
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
        correlation: float | None = None,
        source_weights: dict[str, float] | None = None,
        use_diversity_correlation: bool = False,
    ) -> dict[str, Any]:
        """Combine multiple confidence values accounting for correlation and source reliability.

        When source_weights is provided, observations from reliable sources (higher
        p_accurate) count more. An observation from a reliable source (0.9) counts
        1.8× more than one from an unknown source (0.5).

        When observations are independent (correlation=0.0), confidences compound
        multiplicatively. When perfectly correlated (correlation=1.0), only the
        strongest evidence matters. Values between interpolate.

        When use_diversity_correlation is True, automatically computes effective
        correlation from source diversity (created_by and created_at fields):
        - Same agent, same day: 0.9 (near-duplicate)
        - Same agent, different day: 0.6 (same perspective, different evidence)
        - Different agent: 0.2 (independent perspective)

        This helps distinguish single-agent echo chambers (high correlation, low
        diversity) from well-validated nodes (low correlation, high diversity).

        Args:
            observations: List of dicts with 'confidence' key (0-1).
                May also include 'source' or 'created_by' for diversity.
                May include 'created_at' for temporal diversity.
            correlation: Override correlation (0.0-1.0). If None and
                use_diversity_correlation=False, defaults to 0.0 (independent).
            source_weights: Optional dict mapping source -> reliability weight.
                E.g., {"agent_a": 0.9, "agent_b": 0.5}. Default weight=0.5.
            use_diversity_correlation: If True, compute correlation from source
                diversity. Helps distinguish echo chambers from multi-agent validation.

        Returns:
            Dict with compound_confidence, method, correlation, observation_count,
            weighted (bool), diversity_correlation (if computed), and
            source_diversity_metrics (if use_diversity_correlation=True).
        """
        from ohm.methods import compound_confidence as _cc

        return _cc(
            observations,
            correlation=correlation,
            source_weights=source_weights,
            use_diversity_correlation=use_diversity_correlation,
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

    def orphan_triage(
        self,
        *,
        limit: int = 50,
        min_confidence: float | None = None,
    ) -> dict[str, Any]:
        """Batch triage orphan nodes, producing link suggestions (OHM-jx4q).

        Scans orphan nodes (zero edges) and generates suggestions for
        connecting them to the graph via same-type matching and label overlap.

        Args:
            limit: Max orphans to process (default 50).
            min_confidence: Only triage orphans with confidence >= this value.

        Returns:
            Dict with triaged_count, total_orphans, suggestions list, types_seen.
        """
        from ohm.queries import batch_orphan_triage

        return batch_orphan_triage(
            self._conn,
            limit=limit,
            min_confidence=min_confidence,
        )

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
        """[DEPRECATED] Use deterministic_cascade() instead.

        This method was renamed to clarify that it performs deterministic
        cascade propagation, not Monte Carlo simulation.
        """
        import warnings

        warnings.warn(
            "cascade_scenario is deprecated, use deterministic_cascade instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.deterministic_cascade(
            node_id,
            failure_probability=failure_probability,
            max_depth=max_depth,
        )

    def deterministic_cascade(
        self,
        node_id: str,
        *,
        failure_probability: float = 1.0,
        max_depth: int = 10,
    ) -> list[dict[str, Any]]:
        """Deterministic cascade through downstream graph from a node.

        Starting from *node_id* with *failure_probability*, walks downstream
        through CAUSES, EXPECTED_LIKELIHOOD, DEPENDS_ON, and THREATENS edges.
        Each downstream node's failure probability is computed as:

            P_downstream = P_upstream × edge.probability (or edge.confidence)

        Returns all downstream nodes with computed failure probabilities and
        the path chain that leads to each.

        For a probabilistic simulation with variance estimates, use
        monte_carlo_cascade() instead.

        Example:
            g.deterministic_cascade(supplier_node, failure_probability=0.3)
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
        from ohm.queries import query_deterministic_cascade

        return query_deterministic_cascade(
            self._conn,
            node_id,
            failure_probability=failure_probability,
            max_depth=max_depth,
        )

    def monte_carlo_cascade(
        self,
        node_id: str,
        *,
        trials: int = 1000,
        max_depth: int = 10,
        seed: int | None = None,
        default_probability: float = 0.5,
    ) -> dict[str, Any]:
        """Monte Carlo simulation of cascade through downstream graph.

        Runs *trials* number of cascade trials with two-stage sampling per ADR-008:
        - Stage 1: Edge existence — sample random() < confidence
        - Stage 2: Effect propagation — sample random() < probability

        Returns distribution statistics (p5, p50, p95, mean) for each
        downstream node rather than a single point estimate.

        For a deterministic analysis use deterministic_cascade().

        Args:
            node_id: Starting node for cascade simulation.
            trials: Number of Monte Carlo trials to run (default 1000).
            max_depth: Maximum traversal depth per trial.
            seed: Random seed for reproducibility. If None, results vary each run.
            default_probability: Default probability when edge has none set (default 0.5).

        Returns:
            Dict with node_id, results (per-node statistics), trials, and seed.
        """
        from ohm.queries import monte_carlo_cascade

        return monte_carlo_cascade(
            self._conn,
            node_id,
            trials=trials,
            max_depth=max_depth,
            seed=seed,
            default_probability=default_probability,
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
            self._conn,
            half_life_days=half_life_days,
            stale_threshold=stale_threshold,
        )

    def batch_create_nodes(
        self,
        nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Create multiple nodes at once. All succeed or all fail.

        Args:
            nodes: List of dicts with: label (required), id, type, content,
                visibility, provenance, confidence, priority, url, tags,
                metadata, task_status, assigned_to, due_date.

        Returns:
            List of created node records.
        """
        from ohm.queries import batch_create_nodes

        return batch_create_nodes(self._conn, nodes=nodes, created_by=self.actor)

    def batch_create_edges(
        self,
        edges: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Create multiple edges at once. All succeed or all fail.

        Args:
            edges: List of dicts with: from, to, type (required),
                layer, confidence, condition, provenance.

        Returns:
            List of created edge records.
        """
        from ohm.queries import batch_create_edges

        return batch_create_edges(self._conn, edges=edges, created_by=self.actor)

    def create_batch(
        self,
        *,
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create multiple nodes and edges in a single transaction.

        All succeed or all fail. Each item populates the change feed individually.

        Args:
            nodes: Optional list of node dicts (keys: label, node_type, content,
                   visibility, provenance, confidence, priority, url).
            edges: Optional list of edge dicts (keys: from_node, to_node,
                   edge_type, layer, confidence, condition, provenance, urgency,
                   probability).

        Returns:
            Dict with keys: nodes_created, edges_created, nodes, edges.
        """
        from ohm.queries import create_batch

        return create_batch(self._conn, nodes=nodes, edges=edges, created_by=self.actor)

    def get_agent_config(self, agent_name: str) -> dict[str, Any] | None:
        """Get an agent's configuration (optimization target, services, etc.).

        Config is admin-set and read-only for agents. Returns None if
        the agent has no config entry.
        """
        result = self._conn.execute("SELECT * FROM ohm_agent_config WHERE agent_name = ?", [agent_name]).fetchone()
        if result is None:
            return None
        columns = [desc[0] for desc in self._conn.description]
        return dict(zip(columns, result))

    def list_agent_configs(self) -> list[dict[str, Any]]:
        """List all agent configurations.

        Returns the full config for every registered agent, including
        optimization targets, available services, and thresholds.
        """
        result = self._conn.execute("SELECT * FROM ohm_agent_config ORDER BY agent_name")
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
        other_agents = self._conn.execute(
            """
            SELECT
                n.id AS agent_id,
                n.label AS agent_name,
                COUNT(DISTINCT e.to_node) AS overlap_count
            FROM ohm_nodes n
            JOIN ohm_edges e ON e.from_node = n.id AND e.layer = 'L1'
            WHERE n.type = 'agent'
              AND n.id != ?
              AND (
                (e.edge_type = 'VALUES' AND e.to_node IN (
                    SELECT to_node FROM ohm_edges
                    WHERE from_node = ? AND edge_type = 'VALUES' AND layer = 'L1'))
                OR
                (e.edge_type = 'INTERESTED_IN' AND e.to_node IN (
                    SELECT to_node FROM ohm_edges
                    WHERE from_node = ? AND edge_type = 'INTERESTED_IN' AND layer = 'L1'))
              )
            GROUP BY n.id, n.label
            ORDER BY overlap_count DESC
            LIMIT 10
        """,
            [me_id, me_id, me_id],
        ).fetchall()

        # Find agents with capabilities I might need (complementary)
        # (agents who can do what I can't)
        complementary = self._conn.execute(
            """
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
        """,
            [me_id, me_id],
        ).fetchall()

        results = []
        for agent in other_agents:
            results.append(
                {
                    "agent_id": agent[0],
                    "agent_name": agent[1],
                    "shared_values_interests": agent[2],
                    "recommendation": "LISTENS_TO",
                }
            )

        for cap in complementary:
            # Don't duplicate if already in results
            if not any(r["agent_id"] == cap[0] for r in results):
                results.append(
                    {
                        "agent_id": cap[0],
                        "agent_name": cap[1],
                        "complementary_capability": cap[3],
                        "recommendation": "LISTENS_TO",
                    }
                )

        return results
