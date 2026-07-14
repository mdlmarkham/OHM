"""Substrate computation Graph mixin."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class SubstrateComputationGraphMixin(GraphMixinBase):
    """Monte Carlo, anomalies, aggregation, calibration."""

    def monte_carlo(
        self,
        node_id: str,
        *,
        simulations: int = 1000,
        depth: int = 3,
        confidence_threshold: float = 0.5,
        default_probability: float = 0.5,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Monte Carlo simulation of failure propagation from a node.

        Two-stage sampling per ADR-008:
        - Stage 1: Edge existence — sample random() < confidence
        - Stage 2: Effect propagation — sample random() < probability

        Same result regardless of which agent calls it — substrate method.

        Args:
            node_id: Source node for impact simulation.
            simulations: Number of Monte Carlo trials (default 1000).
            depth: Maximum traversal depth (default 3).
            confidence_threshold: Minimum confidence to consider edge (default 0.5).
            default_probability: Default probability when edge has none set (default 0.5).
            seed: Random seed for reproducibility (default None).

        Returns:
            Dict with affected_nodes, simulation_count, mean_affected, max_affected.
        """
        from ohm.methods import monte_carlo_impact

        return monte_carlo_impact(
            self._conn,
            node_id,
            simulations=simulations,
            depth=depth,
            confidence_threshold=confidence_threshold,
            default_probability=default_probability,
            seed=seed,
        )

    def markov_absorbing_risk(
        self,
        start_node: str,
        *,
        edge_types: list[str] | None = None,
        state_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Absorbing Markov chain risk: probability of reaching each absorbing state.

        Args:
            start_node: Node ID to compute absorption from.
            edge_types: Edge types to treat as transitions (default CAUSES, TRANSITIONS_TO).
            state_nodes: Optional restrict to specific node IDs.

        Returns:
            Dict with absorption_probabilities, transient_states, absorbing_states.
        """
        from ohm.markov import markov_absorbing_risk

        return markov_absorbing_risk(
            self._conn,
            start_node,
            edge_types=edge_types,
            state_nodes=state_nodes,
        )

    def markov_expected_steps(
        self,
        start_node: str,
        *,
        target_state: str | None = None,
        edge_types: list[str] | None = None,
        state_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Expected number of steps before absorption from a start node.

        Args:
            start_node: Node ID to compute from.
            target_state: Optional target absorbing state for directed step count.
            edge_types: Edge types to treat as transitions.
            state_nodes: Optional restrict to specific node IDs.

        Returns:
            Dict with expected_steps, expected_steps_per_state.
        """
        from ohm.markov import markov_expected_steps

        return markov_expected_steps(
            self._conn,
            start_node,
            target_state=target_state,
            edge_types=edge_types,
            state_nodes=state_nodes,
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
            self._conn,
            similarity_threshold=similarity_threshold,
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
            self._conn,
            agent_name or self.actor,
        )

    def agent_profile(self, agent_name: str | None = None) -> dict:
        """Extended agent calibration profile (OHM-792).

        Returns confidence calibration PLUS loop-risk, novelty, contrarian
        value, evidence quality, language-confidence bias, and blast-radius
        awareness.
        """
        from ohm.graph.calibration import compute_agent_profile

        return compute_agent_profile(self._conn, agent_name or self.actor)
