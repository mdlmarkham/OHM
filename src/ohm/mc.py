"""Rust-accelerated Monte Carlo simulation for OHM (OHM-lqpk.4).

This module provides a Python delegation layer that tries to import the
Rust PyO3 extension (``ohm._mc_rust``) and falls back to the pure-Python
implementation when the extension is not available (e.g. on dev boxes
without a Rust toolchain, or during CI runs that don't build the extension).

Boundary contract (shared by both ``monte_carlo_impact`` and
``monte_carlo_cascade``):

    Input:
        adjacency: dict[str, list[tuple[str, float, float]]]
            node_id -> [(target_node, confidence, probability), ...]
        source: str
            Starting node for the cascade.
        trials: int
            Number of Monte Carlo trials.
        depth: int
            Maximum BFS depth per trial.
        seed: int | None
            Random seed for reproducibility.

    Output:
        impact_counts: dict[str, int]
            Per-node activation count (targets that passed both sampling
            stages). The source node is NOT counted — callers that need
            the source counted (e.g. monte_carlo_cascade) add it
            separately.
        per_trial_totals: list[int]
            Number of newly-activated nodes per trial (for mean/max stats).

Two-stage sampling per ADR-008:
    Stage 1: Edge existence — random() < confidence
    Stage 2: Effect propagation — random() < probability
"""

from __future__ import annotations


try:
    from ohm._mc_rust import monte_carlo_sim as _rust_sim

    _HAS_RUST = True
except ImportError:
    _HAS_RUST = False


def _python_sim(
    adjacency: dict[str, list[tuple[str, float, float]]],
    source: str,
    trials: int,
    depth: int,
    seed: int | None = None,
) -> tuple[dict[str, int], list[int]]:
    """Pure-Python fallback — identical algorithm to the Rust extension."""
    import random

    if seed is not None:
        random.seed(seed)

    impact_counts: dict[str, int] = {}
    per_trial_totals: list[int] = []

    for _ in range(trials):
        visited: set[str] = set()
        frontier = [source]
        affected_this_sim = 0

        for _ in range(depth):
            next_frontier: list[str] = []
            for current in frontier:
                if current in visited:
                    continue
                visited.add(current)
                if current not in adjacency:
                    continue
                for target, conf, prob in adjacency[current]:
                    if target in visited:
                        continue
                    if random.random() < conf:
                        if random.random() < prob:
                            next_frontier.append(target)
                            impact_counts[target] = impact_counts.get(target, 0) + 1
                            affected_this_sim += 1
            frontier = next_frontier
            if not frontier:
                break

        per_trial_totals.append(affected_this_sim)

    return impact_counts, per_trial_totals


def monte_carlo_sim(
    adjacency: dict[str, list[tuple[str, float, float]]],
    source: str,
    trials: int,
    depth: int,
    seed: int | None = None,
) -> tuple[dict[str, int], list[int]]:
    """Run Monte Carlo simulation with two-stage sampling.

    Uses the Rust extension if available, otherwise falls back to pure Python.
    Returns (impact_counts, per_trial_totals).
    """
    if _HAS_RUST:
        return _rust_sim(adjacency, source, trials, depth, seed)
    return _python_sim(adjacency, source, trials, depth, seed)


def has_rust_extension() -> bool:
    """Return True if the Rust extension is loaded."""
    return _HAS_RUST
