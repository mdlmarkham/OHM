"""
OHM Bayesian Inference Engine — pgmpy Variable Elimination.

Converts OHM graph edges into a BayesianNetwork (DAG), populates CPTs from
edge probabilities and confidences, and runs exact inference to compute
posterior probabilities given observed evidence.

This replaces the heuristic compound_confidence() with proper Bayesian
propagation through the network structure.

State convention:
  0 = "bad" (failure, closed, negative, threat active)
  1 = "good" (normal, open, positive, threat absent)

Noisy-OR gate for multi-parent nodes: P(child=bad) = 1 - Π(1 - p_i)
where p_i is the edge probability from parent i and the parent is in "bad" state.
"""

import logging
from typing import Any

from ohm.validation import validate_identifier

logger = logging.getLogger(__name__)

try:
    from pgmpy.models import DiscreteBayesianNetwork as BayesianNetwork
    from pgmpy.factors.discrete import TabularCPD
    from pgmpy.inference import VariableElimination
    PGMPY_AVAILABLE = True
except ImportError:
    PGMPY_AVAILABLE = False
    logger.info("pgmpy not available — Bayesian inference disabled. Install with: pip install pgmpy")


def _safe_node_id(node_id: str) -> str:
    """Convert OHM node IDs to pgmpy-safe variable names (alphanumeric + underscore)."""
    return node_id.replace("-", "_").replace(".", "_").replace("/", "_").replace(":", "_")


def _find_acyclic_subgraph(
    edges: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Remove edges that create cycles using topological sort.

    Iteratively removes the edge whose removal breaks the most cycles,
    until the remaining edges form a DAG.
    """
    # Build adjacency and try topological sort
    import networkx as nx  # type: ignore

    G = nx.DiGraph()
    G.add_edges_from(edges)
    try:
        list(nx.topological_sort(G))
        return edges  # Already a DAG
    except nx.NetworkXUnfeasible:
        pass

    # Remove minimum feedback arc set (greedy approximation)
    # Remove edges that are in the most cycles
    edges_list = list(edges)
    while True:
        G = nx.DiGraph()
        G.add_edges_from(edges_list)
        try:
            list(nx.topological_sort(G))
            return edges_list
        except nx.NetworkXUnfeasible:
            pass
        # Find edges in cycles and remove the one with lowest probability
        # (we don't have probability here, so remove the one that breaks most cycles)
        try:
            cycles = list(nx.simple_cycles(G))
            if not cycles:
                break
            # Count how many cycles each edge participates in
            edge_cycle_count: dict[tuple[str, str], int] = {}
            for cycle in cycles:
                for i in range(len(cycle)):
                    e = (cycle[i], cycle[(i + 1) % len(cycle)])
                    edge_cycle_count[e] = edge_cycle_count.get(e, 0) + 1
            # Remove edge in most cycles
            worst = max(edge_cycle_count, key=edge_cycle_count.get)  # type: ignore
            edges_list.remove(worst)
        except (nx.NetworkXError, StopIteration):
            break

    return edges_list


def build_bayesian_network(
    conn,
    *,
    root_nodes: list[str] | None = None,
    edge_types: list[str] | None = None,
    max_nodes: int = 50,
    leak_probability: float = 0.15,
) -> dict[str, Any] | None:
    """Build a BayesianNetwork from OHM edges with probability/confidence values.

    Scans the OHM graph for edges with probability/confidence values and constructs
    a pgmpy BayesianNetwork with CPTs derived from edge weights. Handles cycles
    by removing minimum edges to form a valid DAG.

    Uses noisy-OR gate with leak probability for multi-parent CPTs:
        P(child=bad | parents) = 1 - (1 - leak) * Π_i(1 - p_i * I(parent_i = bad))

    Args:
        conn: DuckDB connection (daemon's own connection).
        root_nodes: Optional list of root node IDs to scope the network.
        edge_types: Edge types to include (default: CAUSES, DEPENDS_ON,
            THREATENS, EXPECTED_LIKELIHOOD).
        max_nodes: Maximum number of nodes to include.
        leak_probability: Baseline probability of bad outcome when all parents
            are good (default 0.15). Critical for realistic priors.

    Returns:
        Dict with 'model', 'nodes', 'edges', 'variables' or None if
        pgmpy is unavailable or no probability edges exist.
    """
    if not PGMPY_AVAILABLE:
        logger.warning("pgmpy not available — cannot build Bayesian network")
        return None

    if edge_types is None:
        edge_types = ["CAUSES", "DEPENDS_ON", "THREATENS", "EXPECTED_LIKELIHOOD"]

    # Find edges with probability/confidence values
    placeholders = ",".join(["?"] * len(edge_types))
    query = f"""
        SELECT from_node, to_node, edge_type,
               COALESCE(probability, confidence) as prob,
               confidence
        FROM ohm_edges
        WHERE edge_type IN ({placeholders})
        AND deleted_at IS NULL
        AND COALESCE(probability, confidence) IS NOT NULL
        ORDER BY from_node, to_node
    """
    rows = conn.execute(query, edge_types).fetchall()

    if not rows:
        logger.info("No probability-bearing edges found — cannot build Bayesian network")
        return None

    # Collect all involved nodes and edges
    node_ids = set()
    edges = []
    for row in rows:
        from_node, to_node, edge_type, prob, confidence = row
        node_ids.add(from_node)
        node_ids.add(to_node)
        prob_val = float(prob) if prob is not None else float(confidence) if confidence is not None else 0.5
        edges.append({
            "from": from_node,
            "to": to_node,
            "type": edge_type,
            "probability": prob_val,
            "confidence": float(confidence) if confidence is not None else 0.5,
        })

    # Scope to root_nodes if specified
    if root_nodes:
        included = set(root_nodes)
        # BFS to find reachable nodes
        for _ in range(3):  # 3 hops
            new_nodes = set()
            for e in edges:
                if e["from"] in included or e["to"] in included:
                    new_nodes.add(e["from"])
                    new_nodes.add(e["to"])
            included |= new_nodes
        node_ids &= included
        edges = [e for e in edges if e["from"] in node_ids and e["to"] in node_ids]

    if len(node_ids) > max_nodes:
        logger.warning(f"Network too large ({len(node_ids)} nodes > {max_nodes}). Truncating.")
        node_ids = set(list(node_ids)[:max_nodes])
        edges = [e for e in edges if e["from"] in node_ids and e["to"] in node_ids]

    # Convert to safe variable names
    safe_names = {n: _safe_node_id(n) for n in node_ids}

    # Build pgmpy network edges — deduplicate and check for cycles
    model_edge_tuples = list(dict.fromkeys(
        (safe_names[e["from"]], safe_names[e["to"]]) for e in edges
        if e["from"] in safe_names and e["to"] in safe_names
    ))

    # Remove cycles to form valid DAG
    try:
        import networkx as nx  # type: ignore
        has_nx = True
    except ImportError:
        has_nx = False

    if has_nx and len(model_edge_tuples) > 0:
        model_edge_tuples = _find_acyclic_subgraph(model_edge_tuples)

    if not model_edge_tuples:
        # Single-node network (no edges) — still valid for priors
        pass

    # Group edges by child node for CPT construction
    # For multi-parent edges, keep only the highest-probability edge per (parent, child)
    parent_edges: dict[str, list[dict]] = {}  # safe_child -> [edge dicts]
    seen_parent_child = set()
    for e in edges:
        safe_child = safe_names.get(e["to"])
        safe_parent = safe_names.get(e["from"])
        if not safe_child or not safe_parent:
            continue
        # Only include edges that survived cycle removal
        if (safe_parent, safe_child) not in set(model_edge_tuples):
            continue
        key = (safe_parent, safe_child)
        if key in seen_parent_child:
            # Keep highest probability edge
            existing = parent_edges[safe_child]
            for i, ex in enumerate(existing):
                if safe_names.get(ex["from"]) == safe_parent:
                    if e["probability"] > ex["probability"]:
                        existing[i] = e
                    break
        else:
            seen_parent_child.add(key)
            if safe_child not in parent_edges:
                parent_edges[safe_child] = []
            parent_edges[safe_child].append(e)

    # Build the model
    model = BayesianNetwork()
    if model_edge_tuples:
        model.add_edges_from(model_edge_tuples)
    else:
        # Single node — add it
        for safe in safe_names.values():
            model.add_node(safe)
            break

    # Get prior probabilities for root nodes
    node_priors = {}
    root_safe_names = set()
    for node_id in node_ids:
        safe = safe_names[node_id]
        if safe not in parent_edges:
            root_safe_names.add(safe)
            # Get prior from observations or default
            prior = conn.execute(
                "SELECT AVG(value) FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL",
                [node_id]
            ).fetchone()[0]
            node_priors[safe] = float(prior) if prior is not None else 0.3  # default prior: 30% "bad"

    # Build CPTs
    cpds = []

    # Root node CPTs (no parents)
    for safe_name in root_safe_names:
        prior = node_priors.get(safe_name, 0.3)
        cpd = TabularCPD(safe_name, 2, [[prior], [1 - prior]])
        cpds.append(cpd)

    # Child node CPTs (conditioned on parents) using noisy-OR gate with leak
    #
    # The leak probability represents the baseline rate of a bad outcome
    # even when no parent is in the "bad" state. Without it, P(bad|parents=good) = 0
    # which makes the prior incorrectly dominate.
    #
    # Noisy-OR with leak:
    #   P(child=bad | parents) = 1 - (1 - leak) * Π_i(1 - p_i * I(parent_i = bad))
    #
    # When all parents are "good": P(bad) = leak
    # When any parent is "bad": P(bad) increases by (1 - leak) * p_i
    DEFAULT_LEAK = leak_probability  # Baseline probability of bad outcome when parents are good

    for child_safe, pedges in parent_edges.items():
        parents = [safe_names[e["from"]] for e in pedges]
        n_parents = len(parents)

        # Use confidence as leak probability if available, otherwise default
        # Higher confidence → lower leak (more of the probability is explained by parents)
        leak = DEFAULT_LEAK

        n_configs = 2 ** n_parents
        true_row = []  # P(child=1="good")
        false_row = []  # P(child=0="bad")

        for config in range(n_configs):
            # config bit i = 1 means parent i is "good" (no failure propagated)
            # Noisy-OR with leak: P(bad) = 1 - (1-leak) * Π(1 - p_i * I(parent=bad))
            survival = 1.0  # P(child survives = stays good)
            for i, e in enumerate(pedges):
                parent_state = (config >> (n_parents - 1 - i)) & 1
                edge_prob = e["probability"]
                # Parent in "bad" state (0) propagates failure with probability edge_prob
                if parent_state == 0:  # parent is "bad"
                    survival *= (1 - edge_prob)
                # Parent in "good" state (1) means no failure from this parent
            # Apply leak: even when all parents are good, there's a baseline risk
            p_good = (1 - leak) * survival
            p_bad = 1 - p_good
            false_row.append(round(p_bad, 6))
            true_row.append(round(p_good, 6))

        if n_parents == 0:
            # Leaf node with no parents — use leak as prior
            cpd = TabularCPD(child_safe, 2, [[leak], [1 - leak]])
        else:
            # pgmpy expects row 0 = state 0 (bad), row 1 = state 1 (good)
            cpd = TabularCPD(child_safe, 2, [false_row, true_row],
                            evidence=parents, evidence_card=[2] * n_parents)
        cpds.append(cpd)

    model.add_cpds(*cpds)

    try:
        assert model.check_model()
    except Exception as e:
        logger.error(f"Bayesian network model check failed: {e}")
        # Try with just root nodes as fallback
        return None

    return {
        "model": model,
        "nodes": list(node_ids),
        "edges": edges,
        "variables": list(safe_names.values()),
        "safe_names": safe_names,
        "root_nodes": list(root_safe_names),
        "n_nodes": len(node_ids),
        "n_edges": len(model_edge_tuples),
    }


def bayesian_inference(
    conn,
    target: str,
    evidence: dict[str, int],
    *,
    edge_types: list[str] | None = None,
    leak_probability: float = 0.15,
) -> dict[str, Any]:
    """Run Bayesian inference on the OHM graph.

    Given observed evidence (node states), compute posterior probabilities
    for the target node using Variable Elimination.

    Args:
        conn: DuckDB connection.
        target: Node ID to compute posterior for.
        evidence: Dict mapping node IDs to observed states.
            State 0 = "bad" (failure, closed, negative).
            State 1 = "good" (normal, open, positive).
        edge_types: Edge types to include in the network.
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15). Critical for realistic priors.

    Returns:
        Dict with posterior probabilities, network info, and method.
        Falls back to heuristic cascade if pgmpy is unavailable.
    """
    target = validate_identifier(target, name="target")

    if not PGMPY_AVAILABLE:
        from ohm.queries import query_cascade_scenario
        cascade = query_cascade_scenario(conn, target, failure_probability=1.0)
        return {
            "method": "heuristic_cascade",
            "pgmpy_available": False,
            "target": target,
            "evidence": evidence,
            "cascade": cascade,
        }

    # Build the Bayesian network scoped around target and evidence nodes
    scope_nodes = [target] + list(evidence.keys())
    network = build_bayesian_network(conn, edge_types=edge_types,
                                      root_nodes=scope_nodes)

    if network is None:
        return {
            "method": "none",
            "pgmpy_available": True,
            "target": target,
            "evidence": evidence,
            "error": "No probability-bearing edges found in graph",
        }

    model = network["model"]
    safe_names = network["safe_names"]

    # Convert evidence to safe names
    safe_evidence = {}
    for node_id, state in evidence.items():
        safe = _safe_node_id(validate_identifier(node_id, name="evidence_node"))
        if safe in network["variables"]:
            safe_evidence[safe] = int(state)

    safe_target = _safe_node_id(target)

    if safe_target not in network["variables"]:
        return {
            "method": "none",
            "pgmpy_available": True,
            "target": target,
            "evidence": evidence,
            "error": f"Target node {target} not in Bayesian network (network has {network['n_nodes']} nodes)",
        }

    # Run Variable Elimination
    try:
        infer = VariableElimination(model)
        result = infer.query([safe_target], evidence=safe_evidence)

        # Extract probabilities: state 0 = "bad", state 1 = "good"
        p_bad = float(result.values[0])
        p_good = float(result.values[1])

        return {
            "method": "bayesian_variable_elimination",
            "pgmpy_available": True,
            "target": target,
            "evidence": evidence,
            "posterior": {
                "good": round(p_good, 4),
                "bad": round(p_bad, 4),
            },
            "network_info": {
                "n_nodes": network["n_nodes"],
                "n_edges": network["n_edges"],
                "root_nodes": network["root_nodes"],
            },
            "target_states": {
                "0": "bad/failure/closed/negative",
                "1": "good/normal/open/positive",
            },
        }
    except Exception as e:
        logger.error(f"Bayesian inference failed: {e}")
        return {
            "method": "error",
            "pgmpy_available": True,
            "target": target,
            "evidence": evidence,
            "error": str(e),
        }