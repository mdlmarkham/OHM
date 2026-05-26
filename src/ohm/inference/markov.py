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
    from duckdb import DuckDBPyConnection

from ohm.graph_reader import coerce_reader as _coerce_reader
from ohm.semantic_roles import SemanticRoles

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


def _find_sccs(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Find strongly connected components using Tarjan's algorithm.

    Returns list of SCCs, each SCC is a list of node IDs.
    Nodes in a cycle are grouped together.
    """
    node_to_idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)

    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for src, dst in edges:
        if src in node_to_idx and dst in node_to_idx:
            adj[node_to_idx[src]].append(node_to_idx[dst])

    indices: list[int | None] = [None] * n
    lowlinks: list[int] = [0] * n
    on_stack: list[bool] = [False] * n
    stack: list[int] = []
    index = 0
    sccs: list[list[str]] = []

    def strongconnect(v: int) -> None:
        nonlocal index
        indices[v] = index
        lowlinks[v] = index
        index += 1
        stack.append(v)
        on_stack[v] = True

        for w in adj[v]:
            if indices[w] is None:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif on_stack[w]:
                lowlinks[v] = min(lowlinks[v], lowlinks[w])

        if lowlinks[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc.append(nodes[w])
                if w == v:
                    break
            sccs.append(scc)

    for v in range(n):
        if indices[v] is None:
            strongconnect(v)

    return sccs


def _collapse_to_dag(
    nodes: list[str],
    edges: list[tuple[str, str, float | None, float | None]],
    sccs: list[list[str]],
) -> tuple[list[str], list[tuple[str, str, float]], dict[str, list[str]]]:
    """Collapse SCCs into meta-nodes forming a DAG.

    Returns:
        Tuple of (meta_nodes, dag_edges, meta_node_members)
        where meta_node_members maps meta_node_id -> [original_node_ids]
    """
    scc_set: dict[str, str] = {}
    for scc in sccs:
        for node in scc:
            scc_set[node] = ",".join(sorted(scc))

    meta_map: dict[str, str] = {}
    for scc in sccs:
        meta_id = ",".join(sorted(scc))
        meta_map[meta_id] = meta_id

    meta_members: dict[str, list[str]] = {}
    for scc in sccs:
        meta_id = ",".join(sorted(scc))
        meta_members[meta_id] = list(scc)

    meta_edges: set[tuple[str, str, float]] = set()
    edge_meta_map: dict[tuple[str, str], float] = {}
    for src, dst, prob, conf in edges:
        src_meta = scc_set.get(src)
        dst_meta = scc_set.get(dst)
        if src_meta is None or dst_meta is None:
            continue
        if src_meta == dst_meta:
            continue
        key = (src_meta, dst_meta)
        p = float(prob) if prob is not None else (float(conf) if conf is not None else 0.5)
        if key in edge_meta_map:
            edge_meta_map[key] = max(edge_meta_map[key], p)
        else:
            edge_meta_map[key] = p

    for (src_meta, dst_meta), prob in edge_meta_map.items():
        meta_edges.add((src_meta, dst_meta, prob))

    meta_nodes = sorted(meta_members.keys())
    dag_edges = sorted(meta_edges, key=lambda x: (x[0], x[1]))

    return meta_nodes, dag_edges, meta_members


def _build_transition_matrix(
    conn: "DuckDBPyConnection",
    *,
    edge_types: list[str] | None = None,
    state_nodes: list[str] | None = None,
    semantic_roles: "SemanticRoles | None" = None,
    collapse_sccs: bool = False,
) -> tuple[list[str], Any, list[str], list[str], list[list[str]]]:
    """Build transition matrix from OHM edges.

    Returns:
        Tuple of (node_list, transition_matrix, transient_states, absorbing_states, sccs)
        transition_matrix is a NumPy array if numpy is available, else None.
        sccs is a list of strongly connected components (cycles) found.
    """
    _require_numpy()

    if edge_types is None:
        if semantic_roles is not None:
            edge_types = semantic_roles.state_transitions_list()
        else:
            edge_types = ["CAUSES", "TRANSITIONS_TO"]

    reader = _coerce_reader(conn)
    _edge_records = reader.get_edges(edge_types=edge_types)

    if state_nodes is not None:
        _node_set_filter = set(state_nodes)
        _edge_records = [e for e in _edge_records if e.from_node in _node_set_filter and e.to_node in _node_set_filter]

    edges = [(e.from_node, e.to_node, e.probability, e.confidence) for e in _edge_records]

    if not edges:
        return [], None, [], [], []

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

    raw_edges = [(e[0], e[1]) for e in edges]
    sccs = _find_sccs(nodes, raw_edges)

    if collapse_sccs and any(len(scc) > 1 for scc in sccs):
        meta_nodes, dag_edges, meta_members = _collapse_to_dag(nodes, edges, sccs)

        meta_to_idx = {m: i for i, m in enumerate(meta_nodes)}
        meta_n = len(meta_nodes)

        meta_out_degree: dict[str, float] = {}
        for src, dst, prob in dag_edges:
            p = float(prob)
            meta_out_degree[src] = meta_out_degree.get(src, 0.0) + p

        matrix = np.zeros((meta_n, meta_n), dtype=np.float64)
        for src, dst, prob in dag_edges:
            i = meta_to_idx[src]
            j = meta_to_idx[dst]
            p = float(prob)
            total = meta_out_degree[src]
            if total > 0:
                matrix[i, j] = p / total
            else:
                matrix[i, j] = p

        for idx in range(meta_n):
            row_sum = matrix[idx].sum()
            if row_sum > 0 and row_sum != 1.0:
                matrix[idx] /= row_sum

        meta_has_outgoing: set[str] = set()
        for src, _, _ in dag_edges:
            meta_has_outgoing.add(src)

        meta_absorbing = [m for m in meta_nodes if m not in meta_has_outgoing]
        meta_transient = [m for m in meta_nodes if m in meta_has_outgoing]

        for meta_id in meta_absorbing:
            i = meta_to_idx[meta_id]
            matrix[i, i] = 1.0

        return meta_nodes, matrix, meta_transient, meta_absorbing, sccs

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

    return nodes, matrix, transient, absorbing, sccs


def markov_absorbing_risk(
    conn: "DuckDBPyConnection",
    start_node: str,
    *,
    edge_types: list[str] | None = None,
    state_nodes: list[str] | None = None,
    semantic_roles: "SemanticRoles | None" = None,
) -> dict[str, Any]:
    """Compute absorption probabilities from a start node.

    Uses absorbing Markov chain theory: N = (I - Q)^(-1), B = N @ R
    where Q = transient-to-transient, R = transient-to-absorbing.

    Args:
        conn: Active DuckDB connection.
        start_node: Node ID to compute absorption from.
        edge_types: Edge types to treat as transitions.
        state_nodes: Optional restrict to specific node IDs.
        semantic_roles: Optional role-to-edge-type mapping overrides.

    Returns:
        Dict with 'method', 'start_node', 'absorption_probabilities',
        'transient_states', 'absorbing_states', 'n_states', 'sccs'.
    """
    _require_numpy()

    reader = _coerce_reader(conn)
    nodes, matrix, transient, absorbing, sccs = _build_transition_matrix(
        reader,
        edge_types=edge_types,
        state_nodes=state_nodes,
        semantic_roles=semantic_roles,
    )

    if not nodes:
        # No transition edges found. Check if start_node exists in ohm_nodes.
        existing = reader.get_nodes(ids=[start_node])
        if existing:
            return {
                "method": "markov_absorbing_risk",
                "start_node": start_node,
                "absorption_probabilities": {start_node: 1.0},
                "transient_states": [],
                "absorbing_states": [start_node],
                "n_states": 1,
                "sccs": [],
            }
        return {
            "method": "markov_absorbing_risk",
            "start_node": start_node,
            "absorption_probabilities": {},
            "transient_states": [],
            "absorbing_states": [],
            "n_states": 0,
            "sccs": [],
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
            "sccs": sccs,
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
            "sccs": sccs,
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
                "sccs": sccs,
            }
        return {
            "method": "markov_absorbing_risk",
            "start_node": start_node,
            "absorption_probabilities": {},
            "transient_states": [],
            "absorbing_states": absorbing,
            "n_states": len(nodes),
            "sccs": sccs,
            "error": "no transient states",
        }

    transient_idx = [node_to_idx[n] for n in transient]
    absorbing_idx = [node_to_idx[n] for n in absorbing]

    Q = matrix[np.ix_(transient_idx, transient_idx)]
    R = matrix[np.ix_(transient_idx, absorbing_idx)]

    n_t = len(transient)
    eye_n = np.eye(n_t)
    try:
        N = np.linalg.inv(eye_n - Q)
    except np.linalg.LinAlgError:
        nodes_scc, matrix_scc, transient_scc, absorbing_scc, sccs_new = _build_transition_matrix(
            reader,
            edge_types=edge_types,
            state_nodes=state_nodes,
            semantic_roles=semantic_roles,
            collapse_sccs=True,
        )
        if not nodes_scc:
            return {
                "method": "markov_absorbing_risk",
                "start_node": start_node,
                "absorption_probabilities": {},
                "transient_states": transient,
                "absorbing_states": absorbing,
                "n_states": len(nodes),
                "sccs": sccs,
                "error": "singular matrix — SCC collapse also failed",
            }
        node_to_idx_scc = {n: i for i, n in enumerate(nodes_scc)}
        original_start = start_node
        if start_node not in nodes_scc:
            for scc in sccs:
                if start_node in scc:
                    start_node = ",".join(sorted(scc))
                    break
        if start_node not in node_to_idx_scc:
            return {
                "method": "markov_absorbing_risk",
                "start_node": original_start,
                "absorption_probabilities": {},
                "transient_states": transient,
                "absorbing_states": absorbing,
                "n_states": len(nodes),
                "sccs": sccs,
                "error": "singular matrix — collapsed start node not in DAG",
            }
        transient_idx_scc = [node_to_idx_scc[n] for n in transient_scc]
        absorbing_idx_scc = [node_to_idx_scc[n] for n in absorbing_scc]
        Q_scc = matrix_scc[np.ix_(transient_idx_scc, transient_idx_scc)]
        R_scc = matrix_scc[np.ix_(transient_idx_scc, absorbing_idx_scc)]
        n_t_scc = len(transient_scc)
        eye_n_scc = np.eye(n_t_scc)
        N_scc = np.linalg.inv(eye_n_scc - Q_scc)
        B_scc = N_scc @ R_scc
        if start_node in transient_scc:
            start_t_idx_scc = transient_scc.index(start_node)
            absorption_probs_scc = {}
            for j, abs_node in enumerate(absorbing_scc):
                absorption_probs_scc[abs_node] = round(float(B_scc[start_t_idx_scc, j]), 6)
        else:
            absorption_probs_scc = {start_node: 1.0}
        return {
            "method": "markov_absorbing_risk",
            "start_node": original_start,
            "absorption_probabilities": absorption_probs_scc,
            "transient_states": transient_scc,
            "absorbing_states": absorbing_scc,
            "n_states": len(nodes_scc),
            "sccs": sccs,
            "scc_collapsed": True,
            "collapsed_sccs": sccs_new,
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
        "sccs": sccs,
    }


def markov_expected_steps(
    conn: "DuckDBPyConnection",
    start_node: str,
    *,
    target_state: str | None = None,
    edge_types: list[str] | None = None,
    state_nodes: list[str] | None = None,
    semantic_roles: "SemanticRoles | None" = None,
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
        semantic_roles: Optional role-to-edge-type mapping overrides.

    Returns:
        Dict with 'method', 'start_node', 'expected_steps',
        'expected_steps_per_state', 'transient_states', 'absorbing_states', 'sccs'.
    """
    _require_numpy()

    reader = _coerce_reader(conn)

    nodes, matrix, transient, absorbing, sccs = _build_transition_matrix(
        reader,
        edge_types=edge_types,
        state_nodes=state_nodes,
        semantic_roles=semantic_roles,
    )

    if not nodes:
        existing = reader.get_nodes(ids=[start_node])
        if existing:
            return {
                "method": "markov_expected_steps",
                "start_node": start_node,
                "expected_steps": 0.0,
                "expected_steps_per_state": {},
                "transient_states": [],
                "absorbing_states": [start_node],
                "n_states": 1,
                "sccs": [],
            }
        return {
            "method": "markov_expected_steps",
            "start_node": start_node,
            "expected_steps": 0.0,
            "expected_steps_per_state": {},
            "transient_states": [],
            "absorbing_states": [],
            "n_states": 0,
            "sccs": [],
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
            "sccs": sccs,
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
            "sccs": sccs,
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
            "sccs": sccs,
        }

    transient_idx = [node_to_idx[n] for n in transient]
    Q = matrix[np.ix_(transient_idx, transient_idx)]

    n_t = len(transient)
    eye_n = np.eye(n_t)
    try:
        N = np.linalg.inv(eye_n - Q)
    except np.linalg.LinAlgError:
        nodes_scc, matrix_scc, transient_scc, absorbing_scc, sccs_new = _build_transition_matrix(
            reader,
            edge_types=edge_types,
            state_nodes=state_nodes,
            semantic_roles=semantic_roles,
            collapse_sccs=True,
        )
        if not nodes_scc:
            return {
                "method": "markov_expected_steps",
                "start_node": start_node,
                "expected_steps": 0.0,
                "expected_steps_per_state": {},
                "transient_states": transient,
                "absorbing_states": absorbing,
                "n_states": len(nodes),
                "sccs": sccs,
                "error": "singular matrix — SCC collapse also failed",
            }
        node_to_idx_scc = {n: i for i, n in enumerate(nodes_scc)}
        if start_node not in nodes_scc:
            for scc in sccs:
                if start_node in scc:
                    start_node = ",".join(sorted(scc))
                    break
        if start_node not in node_to_idx_scc:
            return {
                "method": "markov_expected_steps",
                "start_node": start_node,
                "expected_steps": 0.0,
                "expected_steps_per_state": {},
                "transient_states": transient,
                "absorbing_states": absorbing,
                "n_states": len(nodes),
                "sccs": sccs,
                "error": "singular matrix — collapsed start node not in DAG",
            }
        transient_idx_scc = [node_to_idx_scc[n] for n in transient_scc]
        Q_scc = matrix_scc[np.ix_(transient_idx_scc, transient_idx_scc)]
        n_t_scc = len(transient_scc)
        eye_n_scc = np.eye(n_t_scc)
        N_scc = np.linalg.inv(eye_n_scc - Q_scc)
        ones_scc = np.ones(n_t_scc)
        t_scc = N_scc @ ones_scc
        steps_per_state_scc = {}
        for i, state in enumerate(transient_scc):
            steps_per_state_scc[state] = round(float(t_scc[i]), 4)
        start_t_idx_scc = transient_scc.index(start_node)
        expected_scc = round(float(t_scc[start_t_idx_scc]), 4)
        return {
            "method": "markov_expected_steps",
            "start_node": start_node,
            "expected_steps": expected_scc,
            "expected_steps_per_state": steps_per_state_scc,
            "transient_states": transient_scc,
            "absorbing_states": absorbing_scc,
            "n_states": len(nodes_scc),
            "sccs": sccs,
            "scc_collapsed": True,
            "collapsed_sccs": sccs_new,
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
        "sccs": sccs,
    }

    if target_state is not None:
        if target_state in absorbing:
            absorption = markov_absorbing_risk(
                conn,
                start_node,
                edge_types=edge_types,
                state_nodes=state_nodes,
                semantic_roles=semantic_roles,
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
