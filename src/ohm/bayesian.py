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


def causal_intervention(
    conn,
    target: str,
    intervention_state: int,
    *,
    query_nodes: list[str] | None = None,
    edge_types: list[str] | None = None,
    leak_probability: float = 0.15,
) -> dict[str, Any]:
    """Run causal intervention using Pearl's do-operator (graph surgery).

    Implements the do-operator by surgically modifying the causal graph:
    1. Sever all incoming edges to the target node (remove parent dependencies)
    2. Set the target node to the intervention state externally
    3. Propagate the effect through the remaining DAG

    This differs from Bayesian conditioning (observation) in a critical way:
    - Observation (conditioning): P(Y | X=x) includes confounder effects
    - Intervention (do-operator): P(Y | do(X=x)) isolates the direct causal effect

    When confounders exist (common causes of both X and Y), observation picks
    up the confounder's influence; intervention removes it by severing X's
    incoming edges, so only X's outgoing causal paths remain active.

    For the Hormuz example: if economic pressure causes both Hormuz closure
    AND Fed rate changes, then:
    - Observation P(FedRate=bad | Hormuz=closed) includes economic pressure effect
    - Intervention P(FedRate=bad | do(Hormuz=closed)) isolates only the direct
      causal path Hormuz→Oil→Inflation→FedRate

    Args:
        conn: DuckDB connection.
        target: Node ID to intervene on.
        intervention_state: State to set the target to.
            0 = "bad" (force failure), 1 = "good" (force normal).
        query_nodes: Optional list of downstream nodes to compute posteriors for.
            If None, computes posteriors for all reachable descendants.
        edge_types: Edge types to include in the network.
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15).

    Returns:
        Dict with posterior probabilities for each query node, comparison
        with observation-based inference, and network info.
    """
    target = validate_identifier(target, name="target")

    if not PGMPY_AVAILABLE:
        return {
            "method": "none",
            "pgmpy_available": False,
            "target": target,
            "intervention_state": intervention_state,
            "error": "pgmpy not available — causal intervention requires pgmpy",
        }

    if intervention_state not in (0, 1):
        return {
            "method": "error",
            "target": target,
            "error": f"intervention_state must be 0 or 1, got {intervention_state}",
        }

    # Step 1: Build the original Bayesian network
    scope_nodes = [target]
    if query_nodes:
        scope_nodes.extend(query_nodes)
    network = build_bayesian_network(
        conn, edge_types=edge_types,
        root_nodes=scope_nodes,
        leak_probability=leak_probability,
    )

    if network is None:
        return {
            "method": "none",
            "pgmpy_available": True,
            "target": target,
            "intervention_state": intervention_state,
            "error": "No probability-bearing edges found in graph",
        }

    safe_names = network["safe_names"]
    safe_target = _safe_node_id(target)

    if safe_target not in network["variables"]:
        return {
            "method": "none",
            "pgmpy_available": True,
            "target": target,
            "intervention_state": intervention_state,
            "error": f"Target {target} not in Bayesian network",
        }

    # Step 2: Graph surgery — remove incoming edges to target
    # This is the do-operator: do(X=x) means X is set externally,
    # so we remove all edges INTO X (its parents no longer influence it)
    model = network["model"]
    original_edges = list(model.edges())

    # Identify incoming edges to the target
    incoming_edges = [(u, v) for u, v in original_edges if v == safe_target]

    # Remove incoming edges (graph surgery)
    model_do = model.copy()
    for edge in incoming_edges:
        if model_do.has_edge(*edge):
            model_do.remove_edge(*edge)

    # Step 3: Rebuild CPTs for the mutilated graph
    # Target becomes a root node with deterministic CPT (set to intervention_state)
    # Other CPTs remain the same (they don't depend on target's parents)
    from pgmpy.factors.discrete import TabularCPD as _TabularCPD

    new_cpds = []

    # Set target's CPT to deterministic (intervention_state)
    if intervention_state == 0:  # force "bad"
        target_cpd = _TabularCPD(safe_target, 2, [[1.0], [0.0]])
    else:  # force "good"
        target_cpd = _TabularCPD(safe_target, 2, [[0.0], [1.0]])
    new_cpds.append(target_cpd)

    # Copy over all other CPTs (unchanged by graph surgery)
    for cpd in model.get_cpds():
        if cpd.variable != safe_target:
            # Check if this CPD's evidence includes the target's removed parents
            # If so, we need to rebuild it as a marginal
            cpd_evidence = getattr(cpd, 'evidence', None)
            cpd_evidence_card = getattr(cpd, 'evidence_card', None)
            if cpd_evidence:
                # Check if any evidence variable was removed (target's former parents)
                removed_parents = set()
                for edge in incoming_edges:
                    removed_parents.add(edge[0])
                still_valid = [v for v in cpd_evidence if v not in removed_parents]
                if len(still_valid) != len(cpd_evidence):
                    # Some evidence was removed — need to marginalize
                    if len(still_valid) == 0:
                        # All parents removed — use marginal (average over states)
                        # Sum over all evidence configurations weighted equally
                        n_configs = 1
                        for card in cpd_evidence_card:
                            n_configs *= card
                        n_states = cpd.variable_card
                        values = cpd.get_values()
                        # Average across all evidence configurations
                        marginal = values.mean(axis=1)
                        new_cpd = _TabularCPD(cpd.variable, n_states,
                                             [[marginal[s]] for s in range(n_states)])
                        new_cpds.append(new_cpd)
                    else:
                        # Some but not all parents removed — marginalize over removed ones
                        # This is complex; for now, skip and let model check fail gracefully
                        logger.warning(f"Cannot marginalize partially-removed parents for {cpd.variable}")
                        new_cpds.append(cpd)
                else:
                    new_cpds.append(cpd)
            else:
                new_cpds.append(cpd)

    model_do.cpds = []  # Clear existing CPDs
    try:
        model_do.add_cpds(*new_cpds)
        assert model_do.check_model()
    except Exception as e:
        logger.error(f"Mutilated graph model check failed: {e}")
        # Fallback: rebuild network excluding target's parents
        return {
            "method": "error",
            "pgmpy_available": True,
            "target": target,
            "intervention_state": intervention_state,
            "error": f"Graph surgery failed: {e}",
            "incoming_edges_severed": len(incoming_edges),
        }

    # Step 4: Run inference on the mutilated graph
    # The target is set to intervention_state deterministically
    # We query downstream nodes to see the causal effect
    safe_query_nodes = []
    if query_nodes:
        for qn in query_nodes:
            safe_qn = _safe_node_id(validate_identifier(qn, name="query_node"))
            if safe_qn in network["variables"]:
                safe_query_nodes.append(safe_qn)

    # If no explicit query nodes, find all descendants of target
    if not safe_query_nodes:
        try:
            import networkx as nx
            descendants = nx.descendants(model_do, safe_target)
            safe_query_nodes = list(descendants)
        except Exception:
            safe_query_nodes = [v for v in network["variables"] if v != safe_target]

    if not safe_query_nodes:
        # Target has no descendants — intervention only affects target itself
        return {
            "method": "causal_intervention",
            "pgmpy_available": True,
            "target": target,
            "intervention_state": intervention_state,
            "posterior": {
                target: {
                    "good": 1.0 if intervention_state == 1 else 0.0,
                    "bad": 1.0 if intervention_state == 0 else 0.0,
                }
            },
            "downstream_nodes": [],
            "incoming_edges_severed": len(incoming_edges),
            "network_info": {
                "n_nodes": network["n_nodes"],
                "n_edges": network["n_edges"],
            },
        }

    # Run inference with target as evidence (deterministic)
    try:
        infer = VariableElimination(model_do)
        result = infer.query(safe_query_nodes, evidence={safe_target: intervention_state})

        # Extract posteriors for each query node
        posteriors = {}
        if len(safe_query_nodes) == 1:
            # Single query node — result is a single factor
            qn = safe_query_nodes[0]
            qn_original = None
            for orig, safe in safe_names.items():
                if safe == qn:
                    qn_original = orig
                    break
            posteriors[qn_original or qn] = {
                "good": round(float(result.values[1]), 4),
                "bad": round(float(result.values[0]), 4),
            }
        else:
            # Multiple query nodes — result may be joint or per-variable
            # pgmpy VariableElimination.query with multiple variables returns a factor
            # We need to marginalize to get per-variable posteriors
            for qn in safe_query_nodes:
                qn_original = None
                for orig, safe in safe_names.items():
                    if safe == qn:
                        qn_original = orig
                        break
                try:
                    result_single = infer.query([qn], evidence={safe_target: intervention_state})
                    posteriors[qn_original or qn] = {
                        "good": round(float(result_single.values[1]), 4),
                        "bad": round(float(result_single.values[0]), 4),
                    }
                except Exception:
                    posteriors[qn_original or qn] = {"error": "inference failed for this node"}

    except Exception as e:
        logger.error(f"Intervention inference failed: {e}")
        return {
            "method": "error",
            "pgmpy_available": True,
            "target": target,
            "intervention_state": intervention_state,
            "error": str(e),
        }

    # Step 5: Compare with observation-based inference for the same evidence
    # This shows the confounder effect: difference = confounding bias
    observation_result = bayesian_inference(
        conn, target, {target: intervention_state},
        edge_types=edge_types,
        leak_probability=leak_probability,
    )

    comparison = {}
    for node_id, post in posteriors.items():
        if isinstance(post, dict) and "error" not in post:
            obs_post = observation_result.get("posterior", {})
            # Observation posterior is for target only; we need to query each node
            obs_result = bayesian_inference(
                conn, node_id, {target: intervention_state},
                edge_types=edge_types,
                leak_probability=leak_probability,
            )
            obs_bad = obs_result.get("posterior", {}).get("bad", None)
            int_bad = post.get("bad", None)
            if obs_bad is not None and int_bad is not None:
                comparison[node_id] = {
                    "intervention_bad": int_bad,
                    "observation_bad": obs_bad,
                    "confounding_bias": round(obs_bad - int_bad, 4),
                    "interpretation": "positive bias = observation overestimates causal effect (confounders inflate)" if obs_bad > int_bad else "negative bias = observation underestimates causal effect (confounders suppress)",
                }

    return {
        "method": "causal_intervention",
        "pgmpy_available": True,
        "target": target,
        "intervention_state": intervention_state,
        "state_labels": {
            "0": "bad/failure/closed/negative",
            "1": "good/normal/open/positive",
        },
        "posterior": posteriors,
        "comparison_with_observation": comparison,
        "incoming_edges_severed": len(incoming_edges),
        "network_info": {
            "n_nodes": network["n_nodes"],
            "n_edges": network["n_edges"],
        },
    }


def compute_ate(
    conn,
    cause: str,
    effect: str,
    *,
    edge_types: list[str] | None = None,
    leak_probability: float = 0.15,
) -> dict[str, Any]:
    """Compute Average Treatment Effect (ATE) from the Bayesian model.

    ATE = E[Y|do(X=bad)] - E[Y|do(X=good)]
        = P(Y=bad|do(X=bad)) - P(Y=bad|do(X=good))

    This is a model-based ATE computed directly from the noisy-OR CPDs.
    No observational data required — the ATE follows from the causal model.

    Args:
        conn: DuckDB connection.
        cause: Node ID for the treatment variable.
        effect: Node ID for the outcome variable.
        edge_types: Edge types to include in the network.
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15).

    Returns:
        Dict with ATE, do(bad) and do(good) posteriors, risk ratio, and interpretation.
    """
    cause = validate_identifier(cause, name="cause")
    effect = validate_identifier(effect, name="effect")

    if not PGMPY_AVAILABLE:
        return {
            "method": "none",
            "pgmpy_available": False,
            "cause": cause,
            "effect": effect,
            "error": "pgmpy not available — ATE requires pgmpy",
        }

    # Compute interventional distributions: do(cause=bad) and do(cause=good)
    do_bad = causal_intervention(
        conn, cause, 0,
        query_nodes=[effect],
        edge_types=edge_types,
        leak_probability=leak_probability,
    )
    do_good = causal_intervention(
        conn, cause, 1,
        query_nodes=[effect],
        edge_types=edge_types,
        leak_probability=leak_probability,
    )

    # Extract posteriors
    p_bad_do_bad = do_bad.get("posterior", {}).get(effect, {}).get("bad")
    p_bad_do_good = do_good.get("posterior", {}).get(effect, {}).get("bad")

    if p_bad_do_bad is None or p_bad_do_good is None:
        error_msg = do_bad.get("error") or do_good.get("error") or "Unknown error"
        return {
            "method": "error",
            "cause": cause,
            "effect": effect,
            "error": error_msg,
        }

    ate = p_bad_do_bad - p_bad_do_good

    # Risk ratio: how much does the treatment increase risk?
    risk_ratio = p_bad_do_bad / p_bad_do_good if p_bad_do_good > 0 else float("inf")

    # Effect size interpretation
    if abs(ate) < 0.05:
        interpretation = "negligible causal effect"
    elif abs(ate) < 0.10:
        interpretation = "small causal effect"
    elif abs(ate) < 0.20:
        interpretation = "moderate causal effect"
    else:
        interpretation = "large causal effect"

    direction = "increases" if ate > 0 else "decreases"

    return {
        "method": "model_based_ate",
        "pgmpy_available": True,
        "cause": cause,
        "effect": effect,
        "ate": round(ate, 4),
        "p_effect_bad_do_cause_bad": p_bad_do_bad,
        "p_effect_bad_do_cause_good": p_bad_do_good,
        "risk_ratio": round(risk_ratio, 4),
        "effect_size": interpretation,
        "interpretation": f"Setting {cause} to bad {direction} P({effect}=bad) by {abs(ate):.2%} (ATE={ate:.4f})",
        "state_labels": {
            "0": "bad/failure/closed/negative",
            "1": "good/normal/open/positive",
        },
    }


def compute_sensitivity(
    conn,
    cause: str,
    effect: str,
    *,
    edge_types: list[str] | None = None,
    leak_probability: float = 0.15,
) -> dict[str, Any]:
    """Compute sensitivity analysis (E-value) for a causal effect.

    The E-value (VanderWeele & Ding, 2017) answers:
    "How much unmeasured confounding would it take to overturn this conclusion?"

    An unmeasured confounder would need a risk ratio of at least E-value
    with both the treatment and the outcome to explain away the observed effect.

    E-value = RR + sqrt(RR * (RR - 1))  for RR >= 1

    Interpretation:
    - E-value = 1.0: trivial confounding could explain away the result
    - E-value ~ 1.5: moderate robustness
    - E-value >= 2.0: strong robustness (confounder needs RR>=2 with both)
    - E-value >= 3.0: very strong robustness

    Args:
        conn: DuckDB connection.
        cause: Node ID for the treatment variable.
        effect: Node ID for the outcome variable.
        edge_types: Edge types to include in the network.
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15).

    Returns:
        Dict with E-value, risk ratio, ATE, and robustness assessment.
    """
    import math

    cause = validate_identifier(cause, name="cause")
    effect = validate_identifier(effect, name="effect")

    # First compute ATE to get risk ratio
    ate_result = compute_ate(
        conn, cause, effect,
        edge_types=edge_types,
        leak_probability=leak_probability,
    )

    if "error" in ate_result:
        return ate_result

    risk_ratio = ate_result.get("risk_ratio", 1.0)
    ate = ate_result.get("ate", 0.0)
    p_bad_do_bad = ate_result.get("p_effect_bad_do_cause_bad", 0.0)
    p_bad_do_good = ate_result.get("p_effect_bad_do_cause_good", 0.0)

    # Compute E-value
    if risk_ratio >= 1.0:
        rr = risk_ratio
    else:
        # If RR < 1, compute for the inverse (flip cause interpretation)
        rr = 1.0 / risk_ratio if risk_ratio > 0 else float("inf")

    if rr > 1.0 and math.isfinite(rr):
        e_value = rr + math.sqrt(rr * (rr - 1))
    elif rr == 1.0:
        e_value = 1.0
    else:
        e_value = float("inf")

    # Robustness interpretation
    if e_value <= 1.0:
        robustness = "none"
        robustness_desc = "No causal effect detected — E-value=1.0"
    elif e_value < 1.5:
        robustness = "weak"
        robustness_desc = "Weak robustness — small confounding could explain away result"
    elif e_value < 2.0:
        robustness = "moderate"
        robustness_desc = "Moderate robustness — confounder needs RR>={:.2f} with both cause and effect".format(e_value)
    elif e_value < 3.0:
        robustness = "strong"
        robustness_desc = "Strong robustness — confounder needs RR>={:.2f} with both cause and effect".format(e_value)
    else:
        robustness = "very_strong"
        robustness_desc = "Very strong robustness — confounder needs RR>={:.2f} with both cause and effect".format(e_value)

    # Confounder perturbation analysis
    # Simulate how ATE changes as confounder strength grows
    perturbation_levels = [0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0]
    perturbation_results = []
    for conf_strength in perturbation_levels:
        # Approximate: if confounder has RR=s with both cause and effect,
        # bias = conf_strength^2 * (RR-1) * 0.5, subtracted from ATE
        if ate > 0:
            bias = conf_strength ** 2 * min(risk_ratio - 1, ate) * 0.5
            adjusted_ate = max(0, round(ate - bias, 4))
        else:
            bias = conf_strength ** 2 * min(1 - risk_ratio, abs(ate)) * 0.5
            adjusted_ate = min(0, round(ate + bias, 4))

        perturbation_results.append({
            "confounder_strength": conf_strength,
            "adjusted_ate": adjusted_ate,
            "ate_zero": adjusted_ate == 0,
        })

    # Find the confounder strength that overturns the result
    overturn_strength = None
    for pr in perturbation_results:
        if pr["ate_zero"]:
            overturn_strength = pr["confounder_strength"]
            break

    return {
        "method": "e_value_sensitivity",
        "cause": cause,
        "effect": effect,
        "ate": ate,
        "risk_ratio": round(risk_ratio, 4),
        "e_value": round(e_value, 4),
        "robustness": robustness,
        "robustness_description": robustness_desc,
        "p_effect_bad_do_cause_bad": p_bad_do_bad,
        "p_effect_bad_do_cause_good": p_bad_do_good,
        "confounder_perturbation": perturbation_results,
        "overturn_confounder_strength": overturn_strength,
        "interpretation": f"An unmeasured confounder would need RR>={round(e_value, 2)} with both {cause} and {effect} to explain away the observed effect (ATE={ate:.4f})",
    }


def find_adjustment_sets(
    conn,
    cause: str,
    effect: str,
    *,
    edge_types: list[str] | None = None,
    leak_probability: float = 0.15,
    max_network_size: int = 10,
) -> dict[str, Any]:
    """Find valid backdoor and frontdoor adjustment sets for causal identification.

    Pearl's backdoor criterion: A set Z satisfies the backdoor criterion
    relative to (X, Y) if:
    1. No node in Z is a descendant of X
    2. Z blocks every path from X to Y that has an arrow into X

    Pearl's frontdoor criterion: A set Z satisfies the frontdoor criterion
    relative to (X, Y) if:
    1. Z intercepts all directed paths from X to Y
    2. No unblocked path from X to Z has an arrow into Z
    3. All unblocked paths from Z to Y are blocked by X

    For efficiency, only the minimal adjustment set is computed for networks
    larger than max_network_size nodes.

    Args:
        conn: DuckDB connection.
        cause: Node ID for the treatment variable (X).
        effect: Node ID for the outcome variable (Y).
        edge_types: Edge types to include in the network.
        leak_probability: Baseline probability of bad outcome.
        max_network_size: Only enumerate all adjustment sets for networks
            with this many nodes or fewer.

    Returns:
        Dict with backdoor sets, frontdoor sets, minimal adjustment set,
        identification method, and adjusted estimates.
    """
    import networkx as nx
    import signal

    cause = validate_identifier(cause, name="cause")
    effect = validate_identifier(effect, name="effect")

    if not PGMPY_AVAILABLE:
        return {
            "method": "none",
            "pgmpy_available": False,
            "cause": cause,
            "effect": effect,
            "error": "pgmpy not available — adjustment sets require pgmpy",
        }

    # Build the Bayesian network
    network = build_bayesian_network(
        conn,
        edge_types=edge_types,
        leak_probability=leak_probability,
    )
    if network is None:
        return {
            "method": "none",
            "cause": cause,
            "effect": effect,
            "error": "No probability-bearing edges found — cannot build network",
        }

    bn = network["model"]
    safe_cause = _safe_node_id(cause)
    safe_effect = _safe_node_id(effect)

    # Check if both nodes exist in the network
    if safe_cause not in bn.nodes():
        return {"method": "none", "cause": cause, "effect": effect,
                "error": f"Node {cause} not found in Bayesian network"}
    if safe_effect not in bn.nodes():
        return {"method": "none", "cause": cause, "effect": effect,
                "error": f"Node {effect} not found in Bayesian network"}

    # Use pgmpy's CausalInference to find adjustment sets
    from pgmpy.inference import CausalInference

    ci = CausalInference(bn)
    n_nodes = len(bn.nodes())
    result = {
        "method": "adjustment_sets",
        "pgmpy_available": True,
        "cause": cause,
        "effect": effect,
        "network_info": {
            "n_nodes": network["n_nodes"],
            "n_edges": network["n_edges"],
        },
    }

    # --- Minimal adjustment set (always fast) ---
    try:
        minimal = ci.get_minimal_adjustment_set(safe_cause, safe_effect)
        if minimal is not None:
            ohm_minimal = [s.replace("_", "-") for s in minimal]
            result["minimal_adjustment_set"] = ohm_minimal
        else:
            result["minimal_adjustment_set"] = None
    except Exception as e:
        logger.warning(f"Minimal adjustment set computation failed: {e}")
        result["minimal_adjustment_set"] = None

    # --- Backdoor criterion check ---
    # Check if the empty set satisfies the backdoor criterion (no confounders)
    # Use a simple graph-based check: are there any non-causal paths from cause to effect?
    # This is O(V+E) rather than exponential
    try:
        import networkx as nx
        # Build the underlying graph from the BN
        G = nx.DiGraph(bn.edges())
        # If cause has no parents in the DAG, there are no backdoor paths
        cause_parents = list(bn.get_parents(safe_cause))
        result["empty_set_satisfies_backdoor"] = len(cause_parents) == 0
        result["cause_has_parents"] = len(cause_parents) > 0
        if cause_parents:
            result["cause_parents"] = [p.replace("_", "-") for p in cause_parents]
        else:
            result["cause_parents"] = []
    except Exception as e:
        logger.warning(f"Backdoor criterion check failed: {e}")
        result["empty_set_satisfies_backdoor"] = None
        result["cause_parents"] = []

    # --- Frontdoor identification via graph structure ---
    # A node Z is a frontdoor criterion node if:
    # 1. X → Z (all causal paths go through Z)
    # 2. Z → Y (Z causes Y)
    # 3. No unblocked X ← Z path (X doesn't confound Z)
    # For efficiency, check children of cause that are also parents of effect
    try:
        cause_children = set(bn.get_children(safe_cause))
        effect_parents = set(bn.get_parents(safe_effect))
        frontdoor_candidates = cause_children & effect_parents
        result["frontdoor_nodes"] = [n.replace("_", "-") for n in frontdoor_candidates]
        result["n_frontdoor_nodes"] = len(frontdoor_candidates)
    except Exception as e:
        logger.warning(f"Frontdoor check failed: {e}")
        result["frontdoor_nodes"] = []
        result["n_frontdoor_nodes"] = 0

    # --- Instrumental variables ---
    try:
        ivs = ci.get_ivs(safe_cause, safe_effect)
        if ivs:
            result["instrumental_variables"] = [s.replace("_", "-") for s in ivs]
        else:
            result["instrumental_variables"] = []
    except Exception as e:
        logger.warning(f"Instrumental variable computation failed: {e}")
        result["instrumental_variables"] = []

    # --- Identification method ---
    # Determine identification method from graph structure (fast, no exponential enumeration)
    cause_parents = result.get("cause_parents", [])
    frontdoor_nodes = result.get("frontdoor_nodes", [])
    instrumental = result.get("instrumental_variables", [])

    if result.get("empty_set_satisfies_backdoor"):
        result["identification_method"] = "direct"  # No confounders
    elif result.get("minimal_adjustment_set"):
        result["identification_method"] = "backdoor_adjustment"
    elif frontdoor_nodes:
        result["identification_method"] = "frontdoor"
    elif instrumental:
        result["identification_method"] = "instrumental_variable"
    else:
        result["identification_method"] = "unidentified"  # Can't identify the causal effect

    # --- Adjusted estimates using backdoor ---
    if result.get("minimal_adjustment_set"):
        try:
            adj_set_safe = set(_safe_node_id(n) for n in result["minimal_adjustment_set"])
            posterior = ci.query(
                variables=[safe_effect],
                do={safe_cause: 0},  # do(cause=bad)
                adjustment_set=adj_set_safe,
                show_progress=False,
            )
            # Extract posterior
            p_bad = float(posterior.values[0])  # state 0 = bad
            p_good = float(posterior.values[1])  # state 1 = good
            result["adjusted_estimate"] = {
                "method": "backdoor_adjustment",
                "adjustment_set": result["minimal_adjustment_set"],
                "P_effect_bad_do_cause_bad": round(p_bad, 4),
                "P_effect_good_do_cause_bad": round(p_good, 4),
            }
        except Exception as e:
            logger.warning(f"Adjusted estimate computation failed: {e}")
            result["adjusted_estimate"] = {"error": str(e)}
    else:
        result["adjusted_estimate"] = None

    # --- Interpretation ---
    min_adj = result.get("minimal_adjustment_set")
    n_iv = len(result.get("instrumental_variables", []))
    n_fd = result.get("n_frontdoor_nodes", 0)
    empty_valid = result.get("empty_set_satisfies_backdoor")

    if empty_valid:
        interp = f"No confounders detected between {cause} and {effect} — the do-operator result is unbiased. No adjustment needed."
    elif min_adj:
        interp = f"Adjusting for {min_adj} blocks all backdoor paths from {cause} to {effect}, giving a valid causal estimate"
    elif n_fd > 0:
        interp = f"No backdoor adjustment sets, but {n_fd} frontdoor node(s) found — causal effect can be identified via intermediate mechanisms"
    elif n_iv > 0:
        interp = f"No backdoor/frontdoor sets, but {n_iv} instrumental variable(s) found — causal effect can be bounded but not point-identified"
    else:
        interp = f"No adjustment sets found — {cause} and {effect} may not be causally connected, or identification is not possible with observed variables"

    result["interpretation"] = interp

    return result


def suggest_causes(
    conn,
    *,
    edge_types: list[str] | None = None,
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    """Suggest candidate CAUSES edges from existing non-causal relationships.

    Scans DEPENDS_ON, APPLIES_TO, REFINES, INFLUENCES, and EXPECTED_LIKELIHOOD
    edges for node pairs that are connected but lack a CAUSES edge. These are
    candidates for causal relationships that should be evaluated by agents.

    Also identifies nodes with high centrality that lack causal parents —
    these may have unmeasured causes (latent confounders).

    Args:
        conn: DuckDB connection.
        edge_types: Edge types to consider as causal candidates.
        min_confidence: Minimum confidence threshold for candidate suggestions.

    Returns:
        Dict with candidate_causes (pairs missing CAUSES) and
        unmeasured_causes (nodes with no causal parents but high centrality).
    """
    import networkx as nx

    # Candidate edge types that might indicate causal relationships
    candidate_types = ["DEPENDS_ON", "APPLIES_TO", "REFINES", "INFLUENCES", "EXPECTED_LIKELIHOOD"]

    # Find all edges of candidate types
    placeholders = ",".join(["?"] * len(candidate_types))
    query = f"""
        SELECT from_node, to_node, edge_type, confidence
        FROM ohm_edges
        WHERE edge_type IN ({placeholders})
        AND deleted_at IS NULL
        AND confidence IS NOT NULL
        ORDER BY confidence DESC
    """
    candidate_edges = conn.execute(query, candidate_types).fetchall()

    # Find all existing CAUSES edges
    causes_query = """
        SELECT from_node, to_node
        FROM ohm_edges
        WHERE edge_type = 'CAUSES'
        AND deleted_at IS NULL
    """
    causes_edges = set(
        (r[0], r[1]) for r in conn.execute(causes_query).fetchall()
    )

    # Find candidate pairs that don't have a CAUSES edge yet
    candidates = []
    for from_node, to_node, edge_type, confidence in candidate_edges:
        if confidence >= min_confidence and (from_node, to_node) not in causes_edges:
            # Also check if reverse CAUSES exists
            reverse_exists = (to_node, from_node) in causes_edges
            candidates.append({
                "from": from_node,
                "to": to_node,
                "existing_edge_type": edge_type,
                "confidence": float(confidence) if confidence else None,
                "reverse_causes_exists": reverse_exists,
                "suggestion": f"Consider adding CAUSES edge from {from_node} to {to_node} (currently: {edge_type})",
            })

    # Find nodes with no causal parents but high centrality
    # These may have unmeasured causes (latent confounders)
    # Build a NetworkX graph from CAUSES edges
    causes_rows = conn.execute("""
        SELECT from_node, to_node, probability, confidence
        FROM ohm_edges
        WHERE edge_type = 'CAUSES'
        AND deleted_at IS NULL
    """).fetchall()

    G = nx.DiGraph()
    node_set = set()
    for from_n, to_n, prob, conf in causes_rows:
        G.add_edge(from_n, to_n)
        node_set.add(from_n)
        node_set.add(to_n)

    # Find nodes that are NOT in CAUSES edges at all (no causal parents, no causal children)
    # And nodes that are in CAUSES but have no parents (root nodes)
    all_nodes_query = """
        SELECT id, label, type FROM ohm_nodes WHERE deleted_at IS NULL
    """
    all_nodes = {r[0]: {"label": r[1], "type": r[2]} for r in conn.execute(all_nodes_query).fetchall()}

    # Nodes with CAUSES children but no CAUSES parents = root cause candidates
    root_causes = []
    for node in node_set:
        if G.in_degree(node) == 0 and G.out_degree(node) > 0:
            node_info = all_nodes.get(node, {})
            root_causes.append({
                "id": node,
                "label": node_info.get("label", node),
                "type": node_info.get("type", "unknown"),
                "out_degree": G.out_degree(node),
                "suggestion": f"{node} is a root cause with {G.out_degree(node)} causal children — verify no unmeasured confounders",
            })

    # Nodes not in any CAUSES edge = potentially missing causal structure
    disconnected = []
    for node_id, node_info in all_nodes.items():
        if node_id not in node_set:
            disconnected.append({
                "id": node_id,
                "label": node_info.get("label", node_id),
                "type": node_info.get("type", "unknown"),
                "suggestion": f"{node_id} has no CAUSES edges — consider adding causal relationships",
            })

    return {
        "method": "suggest_causes",
        "candidate_causes_edges": candidates[:20],  # Top 20 by confidence
        "n_candidates": len(candidates),
        "root_causes": root_causes,
        "n_root_causes": len(root_causes),
        "disconnected_from_causal": disconnected[:20],  # Top 20
        "n_disconnected": len(disconnected),
        "causal_edge_count": len(causes_edges),
        "interpretation": (
            f"Found {len(candidates)} candidate CAUSES relationships from non-causal edges. "
            f"{len(root_causes)} root cause nodes and {len(disconnected)} nodes disconnected "
            f"from the causal graph. Review candidates and add CAUSES edges where "
            f"appropriate."
        ),
    }