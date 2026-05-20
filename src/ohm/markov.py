"""OHM Markov Chain analysis — absorbing-state risk and expected steps.

Converts OHM edges into a transition matrix and computes absorption
probabilities and expected step counts using NumPy linear algebra.
This fills the gap between Bayesian (conditional static inference)
and Monte Carlo (one-shot stochastic propagation) by analyzing
sequential multi-step state evolution with absorption.

Reference: docs/markov-feasibility.md (OHM-1jh research spike)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb
    from duckdb import DuckDBPyConnection

logger = logging.getLogger(__name__)

try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    logger.info("numpy not available — Markov chain analysis disabled. Install with: pip install numpy")


def _require_numpy() -> None:
    if not NUMPY_AVAILABLE:
        raise ImportError("numpy is required for Markov chain analysis. Install with: pip install numpy")


def _build_transition_matrix(
    conn: "DuckDBPyConnection",
    *,
    edge_types: list[str] | None = None,
    state_nodes: list[str] | None = None,
) -> tuple[list[str], Any, list[str], list[str]]:
    """Build transition matrix from OHM edges.

    Returns:
        Tuple of (node_list, transition_matrix, transient_states, absorbing_states)
        transition_matrix is a NumPy array if numpy is available, else None.
    """
    _require_numpy()

    if edge_types is None:
        edge_types = ["CAUSES", "TRANSITIONS_TO"]

    type_placeholders = ", ".join(["?"] * len(edge_types))

    if state_nodes is not None:
        node_placeholders = ", ".join(["?"] * len(state_nodes))
        edges = conn.execute(
            f"SELECT from_node, to_node, probability, confidence "
            f"FROM ohm_edges "
            f"WHERE edge_type IN ({type_placeholders}) "
            f"AND from_node IN ({node_placeholders}) "
            f"AND to_node IN ({node_placeholders}) "
            f"AND deleted_at IS NULL",
            edge_types + state_nodes + state_nodes,
        ).fetchall()
    else:
        edges = conn.execute(
            f"SELECT from_node, to_node, probability, confidence "
            f"FROM ohm_edges "
            f"WHERE edge_type IN ({type_placeholders}) "
            f"AND deleted_at IS NULL",
            edge_types,
        ).fetchall()

    if not edges:
        return [], None, [], []

    node_set: set[str] = set()
    for from_node, to_node, _, _ in edges:
        node_set.add(from_node)
        node_set.add(to_node)
    nodes = sorted(node_set)
    node_to_idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)

    has_outgoing: set[str] = set()
    for from_node, _, _, _ in edges:
        has_outgoing.add(from_node)

    absorbing = [n for n in nodes if n not in has_outgoing]
    transient = [n for n in nodes if n in has_outgoing]

    matrix = np.zeros((n, n), dtype=np.float64)

    out_degree: dict[str, float] = {}
    for from_node, _, prob, conf in edges:
        p = float(prob) if prob is not None else (float(conf) if conf is not None else 0.5)
        out_degree[from_node] = out_degree.get(from_node, 0.0) + p

    for from_node, to_node, prob, conf in edges:
        i = node_to_idx[from_node]
        j = node_to_idx[to_node]
        p = float(prob) if prob is not None else (float(conf) if conf is not None else 0.5)
        total = out_degree[from_node]
        if total > 0:
            matrix[i, j] = p / total
        else:
            matrix[i, j] = p

    for idx, node in enumerate(nodes):
        row_sum = matrix[idx].sum()
        if row_sum > 0 and row_sum != 1.0:
            matrix[idx] /= row_sum

    for node in absorbing:
        i = node_to_idx[node]
        matrix[i, i] = 1.0

    return nodes, matrix, transient, absorbing


def markov_absorbing_risk(
    conn: "DuckDBPyConnection",
    start_node: str,
    *,
    edge_types: list[str] | None = None,
    state_nodes: list[str] | None = None,
) -> dict[str, Any]:
    """Compute absorption probabilities from a start node.

    Uses absorbing Markov chain theory: N = (I - Q)^(-1), B = N @ R
    where Q = transient-to-transient, R = transient-to-absorbing.

    Args:
        conn: Active DuckDB connection.
        start_node: Node ID to compute absorption from.
        edge_types: Edge types to treat as transitions.
        state_nodes: Optional restrict to specific node IDs.

    Returns:
        Dict with 'method', 'start_node', 'absorption_probabilities',
        'transient_states', 'absorbing_states', 'n_states'.
    """
    _require_numpy()

    nodes, matrix, transient, absorbing = _build_transition_matrix(
        conn, edge_types=edge_types, state_nodes=state_nodes
    )

    if not nodes:
        # No transition edges found. Check if start_node exists in ohm_nodes.
        existing = conn.execute(
            "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [start_node],
        ).fetchone()
        if existing:
            return {
                "method": "markov_absorbing_risk",
                "start_node": start_node,
                "absorption_probabilities": {start_node: 1.0},
                "transient_states": [],
                "absorbing_states": [start_node],
                "n_states": 1,
            }
        return {
            "method": "markov_absorbing_risk",
            "start_node": start_node,
            "absorption_probabilities": {},
            "transient_states": [],
            "absorbing_states": [],
            "n_states": 0,
            "error": f"start_node '{start_node}' not in graph and no transition edges found",
        }

    if start_node not in nodes:
        return {
            "method": "markov_absorbing_risk",
            "start_node": start_node,
            "absorption_probabilities": {},
            "transient_states": transient,
            "absorbing_states": absorbing,
            "n_states": len(nodes),
            "error": f"start_node '{start_node}' not in graph",
        }

    node_to_idx = {n: i for i, n in enumerate(nodes)}

    if not absorbing:
        return {
            "method": "markov_absorbing_risk",
            "start_node": start_node,
            "absorption_probabilities": {},
            "transient_states": transient,
            "absorbing_states": [],
            "n_states": len(nodes),
            "error": "no absorbing states found — all states are transient",
        }

    if not transient:
        if start_node in absorbing:
            return {
                "method": "markov_absorbing_risk",
                "start_node": start_node,
                "absorption_probabilities": {start_node: 1.0},
                "transient_states": [],
                "absorbing_states": absorbing,
                "n_states": len(nodes),
            }
        return {
            "method": "markov_absorbing_risk",
            "start_node": start_node,
            "absorption_probabilities": {},
            "transient_states": [],
            "absorbing_states": absorbing,
            "n_states": len(nodes),
            "error": "no transient states",
        }

    transient_idx = [node_to_idx[n] for n in transient]
    absorbing_idx = [node_to_idx[n] for n in absorbing]

    Q = matrix[np.ix_(transient_idx, transient_idx)]
    R = matrix[np.ix_(transient_idx, absorbing_idx)]

    n_t = len(transient)
    I = np.eye(n_t)
    try:
        N = np.linalg.inv(I - Q)
    except np.linalg.LinAlgError:
        return {
            "method": "markov_absorbing_risk",
            "start_node": start_node,
            "absorption_probabilities": {},
            "transient_states": transient,
            "absorbing_states": absorbing,
            "n_states": len(nodes),
            "error": "singular matrix — chain may have non-absorbing cycles",
        }

    B = N @ R

    if start_node in transient:
        start_t_idx = transient.index(start_node)
        absorption_probs = {}
        for j, abs_node in enumerate(absorbing):
            absorption_probs[abs_node] = round(float(B[start_t_idx, j]), 6)
    else:
        absorption_probs = {start_node: 1.0}

    return {
        "method": "markov_absorbing_risk",
        "start_node": start_node,
        "absorption_probabilities": absorption_probs,
        "transient_states": transient,
        "absorbing_states": absorbing,
        "n_states": len(nodes),
    }


def markov_expected_steps(
    conn: "DuckDBPyConnection",
    start_node: str,
    *,
    target_state: str | None = None,
    edge_types: list[str] | None = None,
    state_nodes: list[str] | None = None,
) -> dict[str, Any]:
    """Compute expected number of steps to absorption from a start node.

    Uses the fundamental matrix: t = N @ 1 (vector of expected steps
    from each transient state before absorption).

    Args:
        conn: Active DuckDB connection.
        start_node: Node ID to compute from.
        target_state: If specified, compute expected steps to this specific
            absorbing state (not implemented — returns total for now).
        edge_types: Edge types to treat as transitions.
        state_nodes: Optional restrict to specific node IDs.

    Returns:
        Dict with 'method', 'start_node', 'expected_steps',
        'expected_steps_per_state', 'transient_states', 'absorbing_states'.
    """
    _require_numpy()

    nodes, matrix, transient, absorbing = _build_transition_matrix(
        conn, edge_types=edge_types, state_nodes=state_nodes
    )

    if not nodes:
        existing = conn.execute(
            "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [start_node],
        ).fetchone()
        if existing:
            return {
                "method": "markov_expected_steps",
                "start_node": start_node,
                "expected_steps": 0.0,
                "expected_steps_per_state": {},
                "transient_states": [],
                "absorbing_states": [start_node],
                "n_states": 1,
            }
        return {
            "method": "markov_expected_steps",
            "start_node": start_node,
            "expected_steps": 0.0,
            "expected_steps_per_state": {},
            "transient_states": [],
            "absorbing_states": [],
            "n_states": 0,
            "error": f"start_node '{start_node}' not in graph and no transition edges found",
        }

    if start_node not in nodes:
        return {
            "method": "markov_expected_steps",
            "start_node": start_node,
            "expected_steps": 0.0,
            "expected_steps_per_state": {},
            "transient_states": transient,
            "absorbing_states": absorbing,
            "n_states": len(nodes),
            "error": f"start_node '{start_node}' not in graph",
        }

    node_to_idx = {n: i for i, n in enumerate(nodes)}

    if start_node not in transient:
        return {
            "method": "markov_expected_steps",
            "start_node": start_node,
            "expected_steps": 0.0,
            "expected_steps_per_state": {},
            "transient_states": transient,
            "absorbing_states": absorbing,
            "n_states": len(nodes),
        }

    if not transient:
        return {
            "method": "markov_expected_steps",
            "start_node": start_node,
            "expected_steps": 0.0,
            "expected_steps_per_state": {},
            "transient_states": [],
            "absorbing_states": absorbing,
            "n_states": len(nodes),
        }

    transient_idx = [node_to_idx[n] for n in transient]
    Q = matrix[np.ix_(transient_idx, transient_idx)]

    n_t = len(transient)
    I = np.eye(n_t)
    try:
        N = np.linalg.inv(I - Q)
    except np.linalg.LinAlgError:
        return {
            "method": "markov_expected_steps",
            "start_node": start_node,
            "expected_steps": 0.0,
            "expected_steps_per_state": {},
            "transient_states": transient,
            "absorbing_states": absorbing,
            "n_states": len(nodes),
            "error": "singular matrix — chain may have non-absorbing cycles",
        }

    ones = np.ones(n_t)
    t = N @ ones

    steps_per_state = {}
    for i, state in enumerate(transient):
        steps_per_state[state] = round(float(t[i]), 4)

    start_t_idx = transient.index(start_node)
    expected = round(float(t[start_t_idx]), 4)

    result: dict[str, Any] = {
        "method": "markov_expected_steps",
        "start_node": start_node,
        "expected_steps": expected,
        "expected_steps_per_state": steps_per_state,
        "transient_states": transient,
        "absorbing_states": absorbing,
        "n_states": len(nodes),
    }

    if target_state is not None:
        if target_state in absorbing:
            absorption = markov_absorbing_risk(
                conn, start_node, edge_types=edge_types, state_nodes=state_nodes
            )
            prob = absorption.get("absorption_probabilities", {}).get(target_state, 0.0)
            if prob > 0:
                result["target_state"] = target_state
                result["target_probability"] = prob
                result["expected_steps_to_target"] = round(expected / prob, 4) if prob > 0 else float("inf")
            else:
                result["target_state"] = target_state
                result["target_probability"] = 0.0
                result["expected_steps_to_target"] = float("inf")
        else:
            result["target_state"] = target_state
            result["target_probability"] = 0.0
            result["expected_steps_to_target"] = float("inf")
            result["warning"] = f"target_state '{target_state}' is not an absorbing state"

    return result
