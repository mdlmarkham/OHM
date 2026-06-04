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

ADR-008: Probability and confidence are distinct attributes.
  - probability = P(effect|cause), the causal strength
  - confidence = belief in the edge's existence, modulates leak probability
  - When probability is NULL: use confidence * default_probability
  - Leak probability is modulated by average parent confidence:
    leak = leak_probability * (1 - avg_confidence)

ADR-009: NEGATES edges have inverted probability semantics.
  - A NEGATES edge from A to B means: when A is "bad", P(B=bad) *decreases*
  - In noisy-OR: NEGATES propagates failure when parent is "good" (state 1)
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from ohm.validation import validate_identifier
from ohm.graph_reader import coerce_reader, raw_conn
from ohm.semantic_roles import SemanticRoles

_coerce_reader = coerce_reader
_raw_conn = raw_conn

logger = logging.getLogger(__name__)

_MAX_BAYESIAN_NETWORK_CACHE_SIZE = 50


class _LRUBayesianCache(dict):
    """dict subclass that evicts the oldest entry when _maxsize is exceeded.

    OHM-od01.15: The module-level Bayesian network cache grew without bound
    in long-running ohmd. This subclass drops the first-inserted (oldest)
    key when len() > maxsize, providing bounded memory use while preserving
    the dict interface used by all existing callers (``in``, ``[]``, ``.clear()``).
    """

    def __init__(self, maxsize: int = _MAX_BAYESIAN_NETWORK_CACHE_SIZE):
        super().__init__()
        self._maxsize = maxsize
        self._key_order: list[tuple] = []

    def __setitem__(self, key, value):
        if key not in self:
            self._key_order.append(key)
        super().__setitem__(key, value)
        while len(self._key_order) > self._maxsize:
            oldest = self._key_order.pop(0)
            if oldest in self:
                super().__delitem__(oldest)

    def clear(self):
        super().clear()
        self._key_order.clear()


# Module-level cache for Bayesian network construction (OHM-omr)
# Key: (tuple(sorted(edge_types)), tuple(sorted(layers)) if layers else None, max_nodes)
# Value: (generation_at_cache_time, result_dict)
# Invalidated when graph_generation counter increments.
_bayesian_network_cache: _LRUBayesianCache = _LRUBayesianCache()

# Module-level cache for VariableElimination instances (OHM-a689.3).
# Keyed by a hash of the model's CPD values and structure.
# This avoids creating 2 VE instances per causal_intervention call when
# the same model is reused (e.g., VoI loop computes ATE for N candidates
# against the same pre-built network).
_ve_cache: dict[int, "VariableElimination"] = {}
_MAX_VE_CACHE_SIZE = 20

MAX_CPT_PARENTS = 8

from .pert import compute_pert_mean as _compute_pert_mean
from .pert import compute_pert_variance as _compute_pert_variance
from .pert import scale_pert_variance as _scale_pert_variance

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
    edge_probabilities: dict[tuple[str, str], float] | None = None,
    preferred_edges: set[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Remove edges that create cycles using topological sort.

    Iteratively removes the edge whose removal breaks the most cycles,
    preferring to remove low-probability edges when probability data is
    available (ADR-008: probability reflects causal strength).

    When *preferred_edges* is provided, avoids removing those edges during
    cycle-breaking (e.g. the queried cause→effect edge). Falls back to
    removing a preferred edge only if all cycle candidates are preferred.

    Args:
        edges: List of (from, to) node pairs.
        edge_probabilities: Optional mapping of (from, to) -> probability.
            When provided, edges in cycles are removed by lowest probability
            first. When not provided, the edge in the most cycles is removed.
        preferred_edges: Optional set of (from, to) edges to preserve.
            The cycle breaker avoids removing these edges when alternatives exist.
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
    # Prefer removing low-probability edges when probability data is available
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
            # Choose which edge to remove
            cycle_edges = [e for e in edge_cycle_count if e in set(edges_list)]
            # Never remove a preferred edge if a non-preferred alternative exists
            candidates = ([e for e in cycle_edges if e not in preferred_edges] if preferred_edges else cycle_edges) or cycle_edges  # fall back to all cycle edges if all are preferred
            if edge_probabilities:
                # Prefer removing the lowest-probability edge among candidates
                worst = min(candidates, key=lambda e: edge_probabilities.get(e, 0.5))
            else:
                # Fall back to removing the edge in the most cycles
                worst = max(candidates, key=lambda e: edge_cycle_count.get(e, 0))
            edges_list.remove(worst)
        except (nx.NetworkXError, StopIteration):
            break

    return edges_list


def _build_soft_evidence_factors(
    reader,
    model,
    safe_names: dict[str, str],
    *,
    soft_edge_types: list[str] | None = None,
    layers: list[str] | None = None,
    default_confidence: float = 0.7,
) -> list:
    """Convert SUPPORTS/APPLIES_TO edges into pgmpy TabularCPD virtual evidence factors.

    Each soft evidence edge creates a virtual evidence factor on its target node:
    - SUPPORTS edge with confidence c: P(virtual_obs | target=good) = c, P(virtual_obs | target=bad) = 1-c
    - APPLIES_TO edge with confidence c: same treatment, slightly weaker default

    Virtual evidence factors are passed to VariableElimination.query() via the
    ``virtual_evidence`` parameter, which applies Jeffrey's rule (likelihood
    weighting) without adding structural edges to the DAG.

    Args:
        reader: Graph reader instance.
        model: The pgmpy BayesianNetwork model (for node membership checks).
        safe_names: Mapping from original node IDs to safe pgmpy names.
        soft_edge_types: Edge types to treat as soft evidence (default: SUPPORTS, APPLIES_TO).
        layers: Optional layer filter.
        default_confidence: Confidence to use when edge has no confidence (default 0.7).

    Returns:
        List of TabularCPD objects suitable for virtual_evidence parameter.
    """
    if not PGMPY_AVAILABLE:
        return []

    if soft_edge_types is None:
        soft_edge_types = ["SUPPORTS", "APPLIES_TO"]

    model_nodes = set(model.nodes())
    soft_records = reader.get_edges(edge_types=soft_edge_types, layers=layers)
    if not soft_records:
        return []

    factors = []
    for edge in soft_records:
        if edge.to_node not in safe_names:
            continue
        safe_target = safe_names[edge.to_node]
        if safe_target not in model_nodes:
            continue

        confidence = float(edge.confidence) if edge.confidence is not None else default_confidence
        confidence = max(0.01, min(0.99, confidence))

        if edge.edge_type == "SUPPORTS":
            lik_good = confidence
            lik_bad = 1.0 - confidence
        elif edge.edge_type == "APPLIES_TO":
            lik_good = confidence * 0.9
            lik_bad = 1.0 - confidence * 0.9
        else:
            lik_good = confidence
            lik_bad = 1.0 - confidence

        factor = TabularCPD(
            safe_target, 2,
            [[lik_bad], [lik_good]],
        )
        factors.append(factor)

    return factors


def build_bayesian_network(
    conn,
    *,
    root_nodes: list[str] | None = None,
    edge_types: list[str] | None = None,
    layers: list[str] | None = None,
    max_nodes: int = 50,
    leak_probability: float = 0.15,
    default_probability: float = 0.5,
    root_prior: float = 0.3,
    semantic_roles: SemanticRoles | None = None,
    preferred_edges: set[tuple[str, str]] | None = None,
    customer_id: str | None = None,
    half_life_days: float = 0.0,
    observation_window_days: float | None = None,
    include_soft_evidence: bool = False,
    soft_edge_types: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build a BayesianNetwork from OHM edges with probability/confidence values.

    Scans the OHM graph for edges with probability/confidence values and constructs
    a pgmpy BayesianNetwork with CPTs derived from edge weights. Handles cycles
    by removing minimum edges to form a valid DAG.

    Uses noisy-OR gate with leak probability for multi-parent CPTs:
        P(child=bad | parents) = 1 - (1 - leak) * Π_i(1 - p_i * I(parent_i = bad))

    ADR-008: Probability and confidence are distinct attributes.
        - probability = P(effect|cause), the causal strength
        - confidence = belief in the edge's existence, modulates leak probability
        - When probability is NULL: use confidence * default_probability
        - When both are NULL: use default_probability
        - Leak probability is modulated by average parent confidence:
          leak = leak_probability * (1 - avg_confidence)

    ADR-009: NEGATES edges have inverted probability semantics.
        - A NEGATES edge from A to B means: when A is "bad", P(B=bad) *decreases*
        - In noisy-OR: NEGATES propagates failure when parent is "good" (state 1),
          not when parent is "bad" (state 0)

    Args:
        conn: DuckDB connection (daemon's own connection).
        root_nodes: Optional list of root node IDs to scope the network.
        edge_types: Edge types to include (default: CAUSES, DEPENDS_ON,
            THREATENS, EXPECTED_LIKELIHOOD, NEGATES).
        layers: Optional list of layers to include (e.g., ["L3", "L4"]).
            If None, includes all layers.
        max_nodes: Maximum number of nodes to include.
        leak_probability: Baseline probability of bad outcome when all parents
            are good (default 0.15). Modulated by average parent confidence.
        default_probability: Probability to use for edges without probability
            or confidence values (default 0.5).
        root_prior: Default prior probability for root nodes (P(bad)) when
            no observations exist (default 0.3). Can be set to 0.5 for
            uniform priors or derived from domain knowledge.

    Returns:
        Dict with 'model', 'nodes', 'edges', 'variables' or None if
        pgmpy is unavailable or no edges found.
    """
    if not PGMPY_AVAILABLE:
        logger.warning("pgmpy not available — cannot build Bayesian network")
        return None

    reader = _coerce_reader(conn)

    # Cache key based on query parameters (OHM-omr)
    # All parameters that affect CPT values AND the node scope must be included
    # to prevent stale cache hits. root_nodes was previously omitted, causing
    # a network scoped to one set of nodes to be returned for a different set
    # (e.g., /inference?target=X cached network reused for /ate?cause=A&effect=B).
    # customer_id (OHM-g4os) prevents cross-tenant cache bleed — two tenants
    # with identical node IDs must get independent cached networks.
    cache_key = (
        customer_id,
        tuple(sorted(edge_types)) if edge_types else None,
        tuple(sorted(layers)) if layers else None,
        tuple(sorted(root_nodes)) if root_nodes else None,
        # preferred_edges changes which cycle edges are removed, so it must be
        # part of the key — a cache hit for the same nodes but different
        # preferred_edges would return a network built with the wrong DAG.
        tuple(sorted(preferred_edges)) if preferred_edges else None,
        max_nodes,
        root_prior,
        leak_probability,
        default_probability,
        half_life_days,
        include_soft_evidence,
        tuple(sorted(soft_edge_types)) if soft_edge_types else None,
    )

    # Check cache — invalidate if graph_generation has changed
    if cache_key in _bayesian_network_cache:
        cached_generation, cached_result = _bayesian_network_cache[cache_key]
        current_gen = reader.get_graph_generation()
        if current_gen == cached_generation:
            logger.debug("Bayesian network cache hit for key=%s", cache_key)
            return cached_result
        else:
            logger.debug("Cache invalidated: generation %d -> %d", cached_generation, current_gen)

    if edge_types is None:
        # ADR-009: NEGATES edges have inverted probability semantics
        # (negative evidence: parent=bad *reduces* child=bad probability)
        if semantic_roles is not None:
            edge_types = semantic_roles.bayesian_list()
        else:
            edge_types = ["CAUSES", "DEPENDS_ON", "THREATENS", "EXPECTED_LIKELIHOOD", "NEGATES"]

    # Fetch edges via reader (ADR-008: prob and conf are distinct; ADR-013: PERT)
    _edge_records = reader.get_edges(edge_types=edge_types, layers=layers)
    rows = [(e.from_node, e.to_node, e.edge_type, e.probability, e.confidence, e.probability_p05, e.probability_p50, e.probability_p95, e.confidence_p05, e.confidence_p50, e.confidence_p95) for e in _edge_records]

    if not rows:
        logger.info("No edges found — cannot build Bayesian network")
        return None

    # Collect all involved nodes and edges, deduplicating by (from, to) pair
    # ADR-008: probability and confidence are distinct attributes.
    # ADR-009: NEGATES edges have inverted probability semantics.
    # ADR-013: PERT three-point estimation for probability distributions.
    #
    # Effective probability computation:
    #   PERT available: probability = (p05 + 4*p50 + p95) / 6
    #   PERT unavailable, both set:  effective_prob = probability * confidence
    #   PERT unavailable, only prob: effective_prob = probability
    #   PERT unavailable, only conf: effective_prob = confidence * default_probability
    #   PERT unavailable, neither:   effective_prob = default_probability
    node_ids = set()
    seen_edges: dict[tuple[str, str], dict] = {}
    for row in rows:
        (from_node, to_node, edge_type, raw_probability, raw_confidence, prob_p05, prob_p50, prob_p95, conf_p05, conf_p50, conf_p95) = row
        node_ids.add(from_node)
        node_ids.add(to_node)

        # ADR-013: PERT three-point estimation
        # When PERT values are provided, derive probability from PERT mean
        has_pert_probability = prob_p50 is not None
        has_pert_confidence = conf_p50 is not None

        if has_pert_probability:
            has_full_pert_probability = prob_p05 is not None and prob_p95 is not None
            p50 = float(prob_p50)
            if has_full_pert_probability:
                p05 = float(prob_p05)
                p95 = float(prob_p95)
            else:
                # Only p50 given: ±20% defaults create fake precision for CPTs.
                # Use wider ±40% spread to encode more uncertainty, or just use
                # p50 directly when defaults would be too tight.
                if p50 < 0.1 or p50 > 0.9:
                    p05 = max(0.01, p50 * 0.6)
                    p95 = min(0.99, p50 * 1.4)
                else:
                    p05 = p50 * 0.6
                    p95 = min(1.0, p50 * 1.4)
            raw_probability = (p05 + 4 * p50 + p95) / 6.0
            has_explicit_probability = True
        else:
            has_explicit_probability = raw_probability is not None

        if has_pert_confidence:
            has_full_pert_confidence = conf_p05 is not None and conf_p95 is not None
            c50 = float(conf_p50)
            if has_full_pert_confidence:
                c05 = float(conf_p05)
                c95 = float(conf_p95)
            else:
                if c50 < 0.1 or c50 > 0.9:
                    c05 = max(0.01, c50 * 0.6)
                    c95 = min(0.99, c50 * 1.4)
                else:
                    c05 = c50 * 0.6
                    c95 = min(1.0, c50 * 1.4)
            raw_confidence = (c05 + 4 * c50 + c95) / 6.0
            has_explicit_confidence = True
        else:
            has_explicit_confidence = raw_confidence is not None

        # Compute effective probability per ADR-008
        if has_explicit_probability and has_explicit_confidence:
            # Both set: effective_prob = probability * confidence
            prob_val = float(raw_probability) * float(raw_confidence)
            conf_val = float(raw_confidence)
        elif has_explicit_probability:
            # Only probability: use it directly
            prob_val = float(raw_probability)
            conf_val = float(raw_probability)  # confidence defaults to probability
        elif has_explicit_confidence:
            # Only confidence: confidence modulates default_probability
            prob_val = float(raw_confidence) * default_probability
            conf_val = float(raw_confidence)
        else:
            # Neither: use default
            prob_val = default_probability
            conf_val = default_probability

        key = (from_node, to_node)
        edge_dict = {
            "from": from_node,
            "to": to_node,
            "type": edge_type,
            "probability": prob_val,
            "confidence": conf_val,
            "has_explicit_probability": has_explicit_probability,
            "is_negates": edge_type == "NEGATES",
        }
        # Keep highest probability edge per (from, to) pair
        if key not in seen_edges or prob_val > seen_edges[key]["probability"]:
            seen_edges[key] = edge_dict

    edges = list(seen_edges.values())

    # Scope to root_nodes if specified
    if root_nodes:
        included = set(root_nodes)
        # BFS to find reachable nodes — 6 rounds captures paths up to 12 hops,
        # expanding simultaneously from all root_nodes in both edge directions.
        for _ in range(6):
            new_nodes = set()
            for e in edges:
                if e["from"] in included or e["to"] in included:
                    new_nodes.add(e["from"])
                    new_nodes.add(e["to"])
            if not (new_nodes - included):
                break  # Converged
            included |= new_nodes
        node_ids &= included
        edges = [e for e in edges if e["from"] in node_ids and e["to"] in node_ids]

    if len(node_ids) > max_nodes:
        logger.warning(f"Network too large ({len(node_ids)} nodes > {max_nodes}). Truncating.")
        # OHM-u60: deterministic truncation — keep nodes with highest degree
        # (most connections), always preserving root_nodes if specified.
        # Sort by degree descending, then by node ID ascending for tiebreaking.
        root_set = set(root_nodes) if root_nodes else set()
        degree: dict[str, int] = {}
        for e in edges:
            degree[e["from"]] = degree.get(e["from"], 0) + 1
            degree[e["to"]] = degree.get(e["to"], 0) + 1
        # Root nodes are always kept (degree doesn't matter)
        kept = {n for n in root_set if n in node_ids}
        remaining = sorted(
            (n for n in node_ids if n not in kept),
            key=lambda n: (-degree.get(n, 0), n),
        )
        kept |= set(remaining[: max_nodes - len(kept)])
        node_ids = kept
        edges = [e for e in edges if e["from"] in node_ids and e["to"] in node_ids]

    # Convert to safe variable names
    safe_names = {n: _safe_node_id(n) for n in node_ids}

    # Build pgmpy network edges — deduplicate and check for cycles
    model_edge_tuples = list(dict.fromkeys((safe_names[e["from"]], safe_names[e["to"]]) for e in edges if e["from"] in safe_names and e["to"] in safe_names))

    # Remove cycles to form valid DAG
    try:
        import networkx as nx  # type: ignore  # noqa: F401

        has_nx = True
    except ImportError:
        has_nx = False

    if has_nx and len(model_edge_tuples) > 0:
        # Build probability map for cycle-breaking: prefer removing low-probability edges
        edge_prob_map: dict[tuple[str, str], float] = {}
        for e in edges:
            sf = safe_names.get(e["from"])
            st = safe_names.get(e["to"])
            if sf and st:
                edge_prob_map[(sf, st)] = e.get("probability", default_probability)
        safe_preferred: set[tuple[str, str]] | None = None
        if preferred_edges:
            safe_preferred = {(_safe_node_id(a), _safe_node_id(b)) for a, b in preferred_edges}
        model_edge_tuples = _find_acyclic_subgraph(
            model_edge_tuples,
            edge_probabilities=edge_prob_map,
            preferred_edges=safe_preferred,
        )

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
                    if e["probability"] > existing[i]["probability"]:
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

    # Only build CPDs for nodes actually in the model (not nodes that were
    # removed during cycle elimination or truncation). Using model.nodes()
    # prevents "CPD defined on variable not in the model" errors (OHM-7309).
    model_node_set = set(model.nodes())

    # Get prior probabilities for root nodes
    node_priors = {}
    root_safe_names = set()
    now = datetime.now()
    for node_id in node_ids:
        safe = safe_names[node_id]
        if safe not in model_node_set:
            continue  # Node was excluded from model edges — skip its CPD
        if safe not in parent_edges:
            root_safe_names.add(safe)
            # Get prior from probability-scaled observations with temporal decay
            _obs = [
                o for o in reader.get_observations(node_id)
                if o.value is not None and o.scale in ("probability", "unknown")
            ]
            # Filter to observation_window_days if specified
            if observation_window_days is not None and observation_window_days > 0:
                cutoff = now - timedelta(days=observation_window_days)
                _obs = [
                    o for o in _obs
                    if o.created_at and datetime.fromisoformat(str(o.created_at)) >= cutoff
                ]
            if _obs:
                total_weight = 0.0
                weighted_sum = 0.0
                for o in _obs:
                    weight = 1.0
                    if half_life_days > 0.0 and o.created_at:
                        try:
                            obs_time = datetime.fromisoformat(str(o.created_at))
                            age_days = max(0.0, (now - obs_time).total_seconds() / 86400.0)
                            weight = 0.5 ** (age_days / half_life_days)
                        except (ValueError, TypeError):
                            weight = 1.0
                    weighted_sum += float(o.value) * weight
                    total_weight += weight
                prior = weighted_sum / total_weight if total_weight > 0 else None
            else:
                prior = None
            node_priors[safe] = float(prior) if prior is not None else root_prior

    # Build CPTs
    cpds = []

    # Root node CPTs (no parents)
    for safe_name in root_safe_names:
        prior = node_priors.get(safe_name, root_prior)
        # Clamp prior to [0, 1] — observations could average outside range
        prior = max(0.0, min(1.0, prior))
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
    # ADR-008: confidence modulates leak probability.
    #   Higher confidence → lower leak (more of the probability is explained by parents).
    #   leak_i = (1 - confidence_i) * leak_probability
    #   Overall leak = Π_i(leak_i) = Π_i((1 - confidence_i) * leak_probability)
    #   Simplified: leak = leak_probability * (1 - avg_confidence)
    #
    # ADR-009: NEGATES edges have inverted probability semantics.
    #   A NEGATES edge from A to B means: when A is "bad", P(B=bad) *decreases*.
    #   In noisy-OR: NEGATES edges propagate failure when the parent is "good" (state 1),
    #   not when the parent is "bad" (state 0). This is the inverse of CAUSES.
    DEFAULT_LEAK = leak_probability  # Baseline probability of bad outcome when parents are good

    # OHM-a689: Track capped edges for model cleanup.
    # When parent capping removes edges, those edges must also be removed
    # from the model to prevent pgmpy check_model() failing with
    # "CPD doesn't have proper parents associated with it".
    capped_parent_edges: list[tuple[str, str]] = []

    for child_safe, pedges in parent_edges.items():
        parents = [safe_names[e["from"]] for e in pedges]
        n_parents = len(parents)

        # ADR-008: Modulate leak by average parent confidence.
        # Higher confidence → lower leak (more probability explained by parents).
        # leak = leak_probability * (1 - avg_confidence)
        avg_confidence = sum(e["confidence"] for e in pedges) / len(pedges) if pedges else 0.0
        leak = DEFAULT_LEAK * (1.0 - avg_confidence)

        # OHM-a689: Cap parents to prevent CPT explosion (2^16 = 65,536 entries).
        # When a node has > MAX_CPT_PARENTS parents, keep the top N by confidence
        # and aggregate the rest into a residual leak adjustment.
        residual_leak = 0.0
        if n_parents > MAX_CPT_PARENTS:
            pedges_sorted = sorted(pedges, key=lambda e: e["confidence"], reverse=True)
            residual_edges = pedges_sorted[MAX_CPT_PARENTS:]
            pedges = pedges_sorted[:MAX_CPT_PARENTS]
            n_parents = len(pedges)
            parents = [safe_names[e["from"]] for e in pedges]
            # Aggregate residual parents into a combined failure probability.
            # Use noisy-OR aggregation: 1 - Π(1 - p_i) for residual parents only.
            residual_survival = 1.0
            for e in residual_edges:
                residual_survival *= 1.0 - e["probability"]
            residual_leak = 1.0 - residual_survival

            # Track capped edges for model cleanup.
            # These edges are removed from the CPT but still in model.edges(),
            # which causes pgmpy check_model() to fail.
            for e in residual_edges:
                capped_parent_edges.append((safe_names[e["from"]], child_safe))

        # Combine residual leak with base leak.
        # If we dropped parents, their combined effect is added to leak.
        if residual_leak > 0.0:
            leak = 1.0 - (1.0 - leak) * (1.0 - residual_leak)
            leak = max(1e-6, min(0.5, leak))

        # Clamp leak to [1e-6, 0.5] to avoid degenerate CPTs.
        # OHM-m0h: Previous floor of 0.01 destroyed confidence modulation —
        # high-confidence edges (leak ~0.0075) were raised to 0.01, making
        # them indistinguishable from moderate-confidence edges.
        leak = max(1e-6, min(0.5, leak))

        n_configs = 2**n_parents
        true_row = []  # P(child=1="good")
        false_row = []  # P(child=0="bad")

        for config in range(n_configs):
            # config bit i = 1 means parent i is "good" (no failure propagated)
            # Noisy-OR with leak: P(bad) = 1 - (1-leak) * Π(1 - p_i * I(parent=bad))
            # ADR-009: NEGATES edges invert the parent state check.
            survival = 1.0  # P(child survives = stays good)
            # ADR-008: Use probability (effective probability per ADR-008) in CPT construction.
            # This properly models that confidence modulates the causal strength.
            for i, e in enumerate(pedges):
                parent_state = (config >> (n_parents - 1 - i)) & 1
                edge_prob = e["probability"]  # effective probability per ADR-008
                is_negates = e.get("is_negates", False)

                if is_negates:
                    # ADR-009: NEGATES — parent in "good" state propagates failure.
                    # When parent is "good" (1), the negation effect activates:
                    # the parent being good *reduces* the child's chance of being good.
                    if parent_state == 1:  # parent is "good" → negation activates
                        survival *= 1 - edge_prob
                    # When parent is "bad" (0), negation doesn't activate → no effect
                else:
                    # Standard CAUSES/DEPENDS_ON/THREATENS: parent in "bad" state
                    # propagates failure with probability edge_prob
                    if parent_state == 0:  # parent is "bad"
                        survival *= 1 - edge_prob
                    # Parent in "good" state (1) means no failure from this parent
            # Apply leak: even when all parents are good, there's a baseline risk
            p_good = (1 - leak) * survival
            p_bad = 1 - p_good
            # Clamp to [0, 1] — floating point accumulation can produce
            # negative values when many parents multiply (1 - p_i) terms.
            false_row.append(round(max(0.0, min(1.0, p_bad)), 6))
            true_row.append(round(max(0.0, min(1.0, p_good)), 6))

        if n_parents == 0:
            # Leaf node with no parents — use leak as prior
            # Clamp leak to ensure valid CPT
            cpd = TabularCPD(child_safe, 2, [[max(0.0, min(1.0, leak))], [max(0.0, min(1.0, 1 - leak))]])
        else:
            # pgmpy expects row 0 = state 0 (bad), row 1 = state 1 (good)
            # Ensure all CPT values are valid probabilities (floating point accumulation fix)
            clamped_false = [max(0.0, min(1.0, v)) for v in false_row]
            clamped_true = [max(0.0, min(1.0, v)) for v in true_row]
            # Normalize each column to sum to 1.0
            for i in range(len(clamped_false)):
                col_sum = clamped_false[i] + clamped_true[i]
                if col_sum > 0:
                    clamped_false[i] /= col_sum
                    clamped_true[i] /= col_sum
                else:
                    clamped_false[i] = 0.5
                    clamped_true[i] = 0.5
            cpd = TabularCPD(child_safe, 2, [clamped_false, clamped_true], evidence=parents, evidence_card=[2] * n_parents)
        cpds.append(cpd)

    # OHM-a689: Remove capped parent edges from the model before adding CPDs.
    # When parent capping removes edges, those edges must be removed from the model
    # to prevent pgmpy check_model() failing with "CPD doesn't have proper parents".
    if capped_parent_edges:
        model.remove_edges_from(capped_parent_edges)
        logger.info(f"Removed {len(capped_parent_edges)} capped edges from Bayesian model")

    model.add_cpds(*cpds)

    try:
        assert model.check_model()
    except Exception as e:
        logger.error(f"Bayesian network model check failed: {e}")
        # Try with just root nodes as fallback
        return None

    result = {
        "model": model,
        "nodes": list(node_ids),
        "edges": edges,
        "variables": list(safe_names.values()),
        "safe_names": safe_names,
        "root_nodes": list(root_safe_names),
        "n_nodes": len(node_ids),
        "n_edges": len(model_edge_tuples),
    }

    if include_soft_evidence:
        soft_types = soft_edge_types or ["SUPPORTS", "APPLIES_TO"]
        soft_factors = _build_soft_evidence_factors(
            reader, model, safe_names,
            soft_edge_types=soft_types,
            layers=layers,
        )
        result["soft_evidence_factors"] = soft_factors
        result["soft_edge_types"] = soft_types
    else:
        result["soft_evidence_factors"] = []
        result["soft_edge_types"] = []

    # Store in module-level cache with current generation (OHM-omr)
    current_gen = reader.get_graph_generation()
    _bayesian_network_cache[cache_key] = (current_gen, result)
    logger.debug("Cached Bayesian network for key=%s at generation %d", cache_key, current_gen)
    return result


def bayesian_inference(
    conn,
    target: str,
    evidence: dict[str, int],
    *,
    edge_types: list[str] | None = None,
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    half_life_days: float = 0.0,
    observation_window_days: float | None = None,
    include_soft_evidence: bool = False,
    soft_edge_types: list[str] | None = None,
    customer_id: str | None = None,
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
        layers: Optional list of layers to include (e.g., ["L3", "L4"]).
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15). Critical for realistic priors.

    Returns:
        Dict with posterior probabilities, network info, and method.
        Falls back to heuristic cascade if pgmpy is unavailable.
    """
    target = validate_identifier(target, name="target")
    reader = _coerce_reader(conn)

    if not PGMPY_AVAILABLE:
        from ohm.queries import query_cascade_scenario

        cascade = query_cascade_scenario(_raw_conn(conn), target, failure_probability=1.0)
        return {
            "method": "heuristic_cascade",
            "pgmpy_available": False,
            "target": target,
            "evidence": evidence,
            "cascade": cascade,
        }

    # Build the Bayesian network scoped around target and evidence nodes
    scope_nodes = [target] + list(evidence.keys())
    network = build_bayesian_network(reader, edge_types=edge_types, layers=layers, root_nodes=scope_nodes, root_prior=root_prior, half_life_days=half_life_days, observation_window_days=observation_window_days, include_soft_evidence=include_soft_evidence, soft_edge_types=soft_edge_types, customer_id=customer_id)

    if network is None:
        return {
            "method": "none",
            "pgmpy_available": True,
            "target": target,
            "evidence": evidence,
            "error": "No probability-bearing edges found in graph",
        }

    model = network["model"]
    network["safe_names"]

    # Only include evidence nodes actually in the model — BFS scope may include nodes
    # that were excluded by cycle-breaking and are not in model.nodes()
    model_node_set = set(network["model"].nodes())
    safe_evidence = {}
    for node_id, state in evidence.items():
        safe = _safe_node_id(validate_identifier(node_id, name="evidence_node"))
        if safe in model_node_set:
            safe_evidence[safe] = int(state)

    safe_target = _safe_node_id(target)

    if safe_target not in model_node_set:
        return {
            "method": "none",
            "pgmpy_available": True,
            "target": target,
            "evidence": evidence,
            "error": f"Target node {target} not in Bayesian network (network has {network['n_nodes']} nodes)",
        }

    # Run Variable Elimination
    soft_factors = network.get("soft_evidence_factors", [])
    query_kwargs: dict[str, Any] = {"variables": [safe_target], "evidence": safe_evidence}
    if soft_factors:
        query_kwargs["virtual_evidence"] = soft_factors
    try:
        # OHM-a689.3: Cache VE instance
        _ve_key = id(model)
        if _ve_key not in _ve_cache:
            if len(_ve_cache) >= _MAX_VE_CACHE_SIZE:
                _ve_cache.clear()
            _ve_cache[_ve_key] = VariableElimination(model)
        infer = _ve_cache[_ve_key]
        result = infer.query(**query_kwargs)

        # Extract probabilities: state 0 = "bad", state 1 = "good"
        p_bad = float(result.values[0])
        p_good = float(result.values[1])

        # Return node-keyed posterior for consistency with causal_intervention:
        # {"posterior": {"node_id": {"good": X, "bad": Y}}}
        return {
            "method": "bayesian_variable_elimination",
            "pgmpy_available": True,
            "target": target,
            "evidence": evidence,
            "posterior": {
                target: {
                    "good": round(p_good, 4),
                    "bad": round(p_bad, 4),
                }
            },
            "soft_evidence": {
                "enabled": include_soft_evidence,
                "n_factors": len(soft_factors),
                "edge_types": network.get("soft_edge_types", []),
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
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    semantic_roles: SemanticRoles | None = None,
    preferred_edges: set[tuple[str, str]] | None = None,
    include_soft_evidence: bool = False,
    soft_edge_types: list[str] | None = None,
    customer_id: str | None = None,
    # Internal: pre-built Bayesian network for VoI batch optimization (OHM-27).
    # When provided, graph construction is skipped and this network is used directly.
    _pre_built_network: dict[str, Any] | None = None,
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
    reader = _coerce_reader(conn)

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
    if _pre_built_network is not None:
        network = _pre_built_network
    else:
        # Auto-derive preferred_edges from query_nodes: protect target→query_node
        # direction so the cycle breaker never removes the queried causal path.
        auto_preferred: set[tuple[str, str]] = set()
        if query_nodes:
            for qn in query_nodes:
                auto_preferred.add((target, qn))
        effective_preferred = (preferred_edges or set()) | auto_preferred

        scope_nodes = [target]
        if query_nodes:
            scope_nodes.extend(query_nodes)
        network = build_bayesian_network(
            reader,
            edge_types=edge_types,
            layers=layers,
            root_nodes=scope_nodes,
            leak_probability=leak_probability,
            root_prior=root_prior,
            semantic_roles=semantic_roles,
            preferred_edges=effective_preferred or None,
            include_soft_evidence=include_soft_evidence,
            soft_edge_types=soft_edge_types,
            customer_id=customer_id,
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

    # Set target's CPT to deterministic (intervention_state).
    # do-calculus graph surgery severs edges INTO the target only — no other
    # node's CPT changes because no other node had the target as a *parent*.
    if intervention_state == 0:  # force "bad"
        target_cpd = _TabularCPD(safe_target, 2, [[1.0], [0.0]])
    else:  # force "good"
        target_cpd = _TabularCPD(safe_target, 2, [[0.0], [1.0]])
    new_cpds.append(target_cpd)

    # All other CPTs are unchanged — they condition on their own parents, none
    # of which were modified by severing edges that pointed TO the target.
    for cpd in model.get_cpds():
        if cpd.variable != safe_target:
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
    model_nodes = set(model_do.nodes())
    safe_query_nodes = []
    missing_query_nodes = []
    if query_nodes:
        for qn in query_nodes:
            safe_qn = _safe_node_id(validate_identifier(qn, name="query_node"))
            # Verify node is in the model (not just in BFS scope) — some BFS-scoped
            # nodes may have been excluded from the model by cycle-breaking
            if safe_qn in model_nodes:
                safe_query_nodes.append(safe_qn)
            elif safe_qn in network["variables"]:
                # In BFS scope but not in model — report as missing
                missing_query_nodes.append(qn)
            else:
                missing_query_nodes.append(qn)

    # If no explicit query nodes, find all model nodes reachable from target
    if not safe_query_nodes:
        if missing_query_nodes:
            return {
                "method": "error",
                "pgmpy_available": True,
                "target": target,
                "intervention_state": intervention_state,
                "error": (f"Query node(s) {missing_query_nodes} not in Bayesian network (network has {network['n_nodes']} nodes: {network['nodes'][:10]}{'...' if len(network['nodes']) > 10 else ''})"),
                "incoming_edges_severed": len(incoming_edges),
            }
        try:
            import networkx as nx

            descendants = nx.descendants(model_do, safe_target)
            # Only include nodes actually in the model
            safe_query_nodes = [v for v in descendants if v in model_nodes]
        except Exception:
            # Fall back to all model nodes except the target
            safe_query_nodes = [v for v in model_nodes if v != safe_target]

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

    # Run inference per query node — query each individually to avoid a joint
    # distribution that is slow and can fail if any node is not connected.
    # Each individual query returns a marginal distribution for that node.
    # OHM-a689.3: Cache VE instances to avoid creating 2 per call.
    _ve_key_do = id(model_do)
    if _ve_key_do not in _ve_cache:
        if len(_ve_cache) >= _MAX_VE_CACHE_SIZE:
            _ve_cache.clear()
        _ve_cache[_ve_key_do] = VariableElimination(model_do)
    infer = _ve_cache[_ve_key_do]
    soft_factors = network.get("soft_evidence_factors", [])
    posteriors = {}
    for qn in safe_query_nodes:
        qn_original = next((orig for orig, safe in safe_names.items() if safe == qn), qn)
        try:
            do_kwargs: dict[str, Any] = {"variables": [qn], "evidence": {safe_target: intervention_state}}
            if soft_factors:
                do_kwargs["virtual_evidence"] = soft_factors
            result_single = infer.query(**do_kwargs)
            posteriors[qn_original] = {
                "good": round(float(result_single.values[1]), 4),
                "bad": round(float(result_single.values[0]), 4),
            }
        except Exception as e:
            posteriors[qn_original] = {"error": f"inference failed: {e}"}

    # Step 5: Compare with observation-based inference for the same evidence
    # This shows the confounder effect: difference = confounding bias
    # Reuse the already-built network instead of rebuilding per query node (OHM-1p8)
    comparison = {}
    try:
        obs_model = network["model"]
        # OHM-a689.3: Cache observation VE instance
        _obs_ve_key = id(obs_model)
        if _obs_ve_key not in _ve_cache:
            if len(_ve_cache) >= _MAX_VE_CACHE_SIZE:
                _ve_cache.clear()
            _ve_cache[_obs_ve_key] = VariableElimination(obs_model)
        obs_infer = _ve_cache[_obs_ve_key]
        for node_id, post in posteriors.items():
            if isinstance(post, dict) and "error" not in post:
                safe_qn = None
                for orig, safe in safe_names.items():
                    if orig == node_id:
                        safe_qn = safe
                        break
                if not safe_qn:
                    continue
                try:
                    obs_kwargs: dict[str, Any] = {"variables": [safe_qn], "evidence": {safe_target: intervention_state}}
                    if soft_factors:
                        obs_kwargs["virtual_evidence"] = soft_factors
                    obs_result = obs_infer.query(**obs_kwargs)
                    obs_bad = round(float(obs_result.values[0]), 4)
                    int_bad = post.get("bad", None)
                    if int_bad is not None:
                        comparison[node_id] = {
                            "intervention_bad": int_bad,
                            "observation_bad": obs_bad,
                            "confounding_bias": round(obs_bad - int_bad, 4),
                            "interpretation": "positive bias = observation overestimates causal effect (confounders inflate)" if obs_bad > int_bad else "negative bias = observation underestimates causal effect (confounders suppress)",
                        }
                except Exception:
                    # Skip nodes where observation inference fails
                    pass
    except Exception as e:
        logger.warning(f"Observation comparison failed: {e}")

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
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    semantic_roles: SemanticRoles | None = None,
    include_soft_evidence: bool = False,
    soft_edge_types: list[str] | None = None,
    customer_id: str | None = None,
    # Internal: pre-built network for VoI batch optimization (OHM-27).
    # When provided, graph construction is skipped and this network is used
    # for both do(bad) and do(good) interventions.
    _pre_built_network: dict[str, Any] | None = None,
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
        layers: Optional list of layers to include (e.g., ["L3", "L4"]).
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15).

    Returns:
        Dict with ATE, do(bad) and do(good) posteriors, risk ratio, and interpretation.
    """
    cause = validate_identifier(cause, name="cause")
    effect = validate_identifier(effect, name="effect")
    reader = _coerce_reader(conn)

    if not PGMPY_AVAILABLE:
        return {
            "method": "none",
            "pgmpy_available": False,
            "cause": cause,
            "effect": effect,
            "error": "pgmpy not available — ATE requires pgmpy",
        }

    # Compute interventional distributions: do(cause=bad) and do(cause=good)
    # Pass the queried cause→effect as a preferred edge so the cycle breaker
    # avoids removing it (it prefers removing backward/feedback edges instead).
    _preferred = {(cause, effect)}
    do_bad = causal_intervention(
        reader,
        cause,
        0,
        query_nodes=[effect],
        edge_types=edge_types,
        layers=layers,
        leak_probability=leak_probability,
        root_prior=root_prior,
        semantic_roles=semantic_roles,
        preferred_edges=_preferred,
        include_soft_evidence=include_soft_evidence,
        soft_edge_types=soft_edge_types,
        customer_id=customer_id,
        _pre_built_network=_pre_built_network,
    )
    do_good = causal_intervention(
        reader,
        cause,
        1,
        query_nodes=[effect],
        edge_types=edge_types,
        layers=layers,
        leak_probability=leak_probability,
        root_prior=root_prior,
        semantic_roles=semantic_roles,
        preferred_edges=_preferred,
        include_soft_evidence=include_soft_evidence,
        soft_edge_types=soft_edge_types,
        customer_id=customer_id,
        _pre_built_network=_pre_built_network,
    )

    # Extract posteriors
    p_bad_do_bad = do_bad.get("posterior", {}).get(effect, {}).get("bad")
    p_bad_do_good = do_good.get("posterior", {}).get(effect, {}).get("bad")

    if p_bad_do_bad is None or p_bad_do_good is None:
        error_msg = do_bad.get("error") or do_good.get("error")
        if not error_msg:
            bad_keys = list(do_bad.get("posterior", {}).keys())
            good_keys = list(do_good.get("posterior", {}).keys())
            error_msg = f"Effect node '{effect}' not found in intervention posteriors. do(bad) has keys {bad_keys}, do(good) has keys {good_keys}. The effect node may not be in the Bayesian network."
        return {
            "method": "error",
            "cause": cause,
            "effect": effect,
            "error": error_msg,
        }

    ate = p_bad_do_bad - p_bad_do_good

    # When ATE rounds to 0, check if cause actually has a directed path to effect.
    # If not, the zero is spurious (disconnected subgraphs) rather than a genuine
    # null effect — return a diagnostic error instead of a misleading ATE=0.
    if abs(ate) < 1e-6:
        try:
            import networkx as nx

            if _pre_built_network is not None:
                _net = _pre_built_network
            else:
                # Rebuild the scoped network to get the DAG — pass preferred_edges so
                # the cycle breaker uses the same DAG as the actual VE computation.
                _net = build_bayesian_network(
                    reader,
                    root_nodes=[cause, effect],
                    edge_types=edge_types,
                    layers=layers,
                    leak_probability=leak_probability,
                    root_prior=root_prior,
                    semantic_roles=semantic_roles,
                    preferred_edges=_preferred,
                    customer_id=customer_id,
                )
            if _net is not None:
                _model = _net["model"]
                _safe_cause = _safe_node_id(cause)
                _safe_effect = _safe_node_id(effect)
                _cause_in_net = _safe_cause in _net["variables"]
                _effect_in_net = _safe_effect in _net["variables"]
                if not _cause_in_net or not _effect_in_net:
                    missing = []
                    if not _cause_in_net:
                        missing.append(f"cause '{cause}' (not an edge endpoint in the graph)")
                    if not _effect_in_net:
                        missing.append(f"effect '{effect}' (not an edge endpoint in the graph)")
                    return {
                        "method": "error",
                        "cause": cause,
                        "effect": effect,
                        "error": (f"ATE cannot be computed: {'; '.join(missing)}. Network has {_net['n_nodes']} nodes. Check that both node IDs exist and have causal edges."),
                        "network_nodes": _net.get("nodes", [])[:20],
                    }
                _g = nx.DiGraph(_model.edges())
                if _safe_cause in _g and _safe_effect in _g:
                    if not nx.has_path(_g, _safe_cause, _safe_effect):
                        return {
                            "method": "error",
                            "cause": cause,
                            "effect": effect,
                            "error": (f"No directed path from '{cause}' to '{effect}' in the Bayesian network ({_net['n_nodes']} nodes). Both nodes are present but not causally connected — add a CAUSES/DEPENDS_ON edge chain between them."),
                            "network_nodes": _net.get("nodes", [])[:20],
                        }
        except Exception:
            pass  # Diagnostic check failed — return ATE=0 result as-is

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
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    customer_id: str | None = None,
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
        layers: Optional list of layers to include (e.g., ["L3", "L4"]).
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15).

    Returns:
        Dict with E-value, risk ratio, ATE, and robustness assessment.
    """
    import math

    cause = validate_identifier(cause, name="cause")
    effect = validate_identifier(effect, name="effect")
    reader = _coerce_reader(conn)

    # First compute ATE to get risk ratio
    ate_result = compute_ate(
        reader,
        cause,
        effect,
        edge_types=edge_types,
        layers=layers,
        leak_probability=leak_probability,
        root_prior=root_prior,
        customer_id=customer_id,
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

    # Confounder perturbation analysis (VanderWeele & Ding bounding approach)
    # For a confounder with strength s (RR of confounder-outcome association),
    # the bias-adjusted RR is bounded by RR/s (positive confounding).
    # This gives a proper sensitivity interval rather than an ad-hoc formula.
    # Confounder strength s >= 1.0 represents how strongly the confounder
    # is associated with the outcome (s=1 means no confounding).
    perturbation_levels = [1.0, 1.2, 1.5, 2.0, 3.0, 5.0, 10.0]
    perturbation_results = []
    for conf_strength in perturbation_levels:
        if conf_strength == 1.0:
            # No confounding — original estimate
            adjusted_ate = round(ate, 4)
        elif risk_ratio > 0 and conf_strength > 1.0:
            # VanderWeele & Ding bounding: adjusted RR = RR / s
            # where s is the confounder-outcome RR (>= 1.0)
            # Convert back to probability scale for ATE
            adjusted_rr = risk_ratio / conf_strength
            # ATE = P(bad|do(bad)) - P(bad|do(good))
            # Adjusted: use adjusted RR to bound the effect
            if p_bad_do_good > 0:
                adjusted_p_bad_do_bad = min(1.0, p_bad_do_good * adjusted_rr)
                adjusted_ate = round(adjusted_p_bad_do_bad - p_bad_do_good, 4)
            else:
                adjusted_ate = round(ate, 4)
        else:
            adjusted_ate = round(ate, 4)

        perturbation_results.append(
            {
                "confounder_strength": conf_strength,
                "adjusted_ate": adjusted_ate,
                "ate_zero": abs(adjusted_ate) < 1e-6,
            }
        )

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
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    max_network_size: int = 10,
    customer_id: str | None = None,
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

    cause = validate_identifier(cause, name="cause")
    effect = validate_identifier(effect, name="effect")
    reader = _coerce_reader(conn)

    if not PGMPY_AVAILABLE:
        return {
            "method": "none",
            "pgmpy_available": False,
            "cause": cause,
            "effect": effect,
            "error": "pgmpy not available — adjustment sets require pgmpy",
        }

    # Build the Bayesian network
    try:
        network = build_bayesian_network(
            reader,
            edge_types=edge_types,
            layers=layers,
            leak_probability=leak_probability,
            root_prior=root_prior,
            customer_id=customer_id,
        )
    except Exception as e:
        return {"method": "none", "cause": cause, "effect": effect, "error": f"Failed to build Bayesian network: {e}"}
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
        return {"method": "none", "cause": cause, "effect": effect, "error": f"Node {cause} not found in Bayesian network"}
    if safe_effect not in bn.nodes():
        return {"method": "none", "cause": cause, "effect": effect, "error": f"Node {effect} not found in Bayesian network"}

    # Use pgmpy's CausalInference to find adjustment sets
    from pgmpy.inference import CausalInference

    try:
        ci = CausalInference(bn)
    except Exception as e:
        return {"method": "none", "cause": cause, "effect": effect, "error": f"CausalInference init failed (CPD missing after subgraph pruning): {e}"}
    len(bn.nodes())
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
        nx.DiGraph(bn.edges())
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
    layers: list[str] | None = None,
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

    reader = _coerce_reader(conn)

    # Candidate edge types that might indicate causal relationships
    candidate_types = ["DEPENDS_ON", "APPLIES_TO", "REFINES", "INFLUENCES", "EXPECTED_LIKELIHOOD"]

    # Find all edges of candidate types with confidence set (respect layer scope)
    _cand_records = reader.get_edges(edge_types=candidate_types, layers=layers)
    candidate_edges = [(e.from_node, e.to_node, e.edge_type, e.confidence) for e in _cand_records if e.confidence is not None]
    candidate_edges.sort(key=lambda r: r[3], reverse=True)

    # Find all existing CAUSES edges (scope to same layers to avoid cross-domain checks)
    _causes_records = reader.get_edges(edge_types=["CAUSES"], layers=layers)
    causes_edges = {(e.from_node, e.to_node) for e in _causes_records}

    # Find candidate pairs that don't have a CAUSES edge yet.
    # When layer-scoped, restrict to pairs where at least one endpoint is in the
    # existing CAUSES graph for that layer — avoids surfacing entirely foreign
    # node pairs whose edges happen to be tagged with the same layer.
    causal_nodes = {n for pair in causes_edges for n in pair}
    candidates = []
    for from_node, to_node, edge_type, confidence in candidate_edges:
        if confidence >= min_confidence and (from_node, to_node) not in causes_edges:
            if layers and (from_node not in causal_nodes and to_node not in causal_nodes):
                continue  # Neither endpoint is in the scoped causal graph
            reverse_exists = (to_node, from_node) in causes_edges
            candidates.append(
                {
                    "from": from_node,
                    "to": to_node,
                    "existing_edge_type": edge_type,
                    "confidence": float(confidence) if confidence else None,
                    "reverse_causes_exists": reverse_exists,
                    "suggestion": f"Consider adding CAUSES edge from {from_node} to {to_node} (currently: {edge_type})",
                }
            )

    # Find nodes with no causal parents but high centrality
    # These may have unmeasured causes (latent confounders)
    # Build a NetworkX graph from CAUSES edges
    G = nx.DiGraph()
    node_set = set()
    for e in _causes_records:
        G.add_edge(e.from_node, e.to_node)
        node_set.add(e.from_node)
        node_set.add(e.to_node)

    # When layer-scoped, restrict the "disconnected" node universe to nodes
    # already in the CAUSES graph for that layer. This prevents foreign-domain
    # nodes (whose edges happen to be tagged with the same layer) from appearing
    # in the disconnected list. Without a layer filter, still include candidate-
    # edge endpoints for breadth.
    if layers:
        scoped_node_ids = node_set.copy()
    else:
        scoped_node_ids = node_set.copy()
        for from_node, to_node, _et, _conf in candidate_edges:
            scoped_node_ids.add(from_node)
            scoped_node_ids.add(to_node)

    all_nodes = {n.id: {"label": n.label, "type": n.type} for n in reader.get_nodes(ids=list(scoped_node_ids) if scoped_node_ids else None)}

    # Nodes with CAUSES children but no CAUSES parents = root cause candidates
    root_causes = []
    for node in node_set:
        if G.in_degree(node) == 0 and G.out_degree(node) > 0:
            node_info = all_nodes.get(node, {})
            root_causes.append(
                {
                    "id": node,
                    "label": node_info.get("label", node),
                    "type": node_info.get("type", "unknown"),
                    "out_degree": G.out_degree(node),
                    "suggestion": f"{node} is a root cause with {G.out_degree(node)} causal children — verify no unmeasured confounders",
                }
            )

    # Nodes not in any CAUSES edge = potentially missing causal structure
    # When layer-scoped, only include nodes in the scoped set
    disconnected = []
    for node_id, node_info in all_nodes.items():
        if node_id not in node_set:
            disconnected.append(
                {
                    "id": node_id,
                    "label": node_info.get("label", node_id),
                    "type": node_info.get("type", "unknown"),
                    "suggestion": f"{node_id} has no CAUSES edges — consider adding causal relationships",
                }
            )

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
            f"Found {len(candidates)} candidate CAUSES relationships from non-causal edges. {len(root_causes)} root cause nodes and {len(disconnected)} nodes disconnected from the causal graph. Review candidates and add CAUSES edges where appropriate."
        ),
    }


def pert_mean(p05: float, p50: float, p95: float) -> float:
    """Compute PERT mean estimate. Delegates to ohm.pert.compute_pert_mean."""
    return _compute_pert_mean(p05, p50, p95)


def pert_variance(p05: float, p95: float) -> float:
    """Compute PERT variance estimate. Delegates to ohm.pert.compute_pert_variance."""
    return _compute_pert_variance(p05, p95)


def compute_voi(
    conn,
    decision_nodes: list[str] | None = None,
    *,
    edge_types: list[str] | None = None,
    layers: list[str] | None = None,
    top: int | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    timeout: float | None = None,
    semantic_roles: SemanticRoles | None = None,
    min_observations: int = 0,
    include_soft_evidence: bool = False,
    soft_edge_types: list[str] | None = None,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """Compute Value of Information (VoI) for research prioritization.

    OHM-6mv.1: For each decision node (where being wrong matters), traverse
    causal paths backward to find root cause ancestors. For each ancestor,
    compute: VoI = uncertainty × sensitivity_to_decision.

    uncertainty = 1 - mean_confidence (how uncertain are we about this node?)
    sensitivity = |ATE| (how much does this node affect the decision?)

    Nodes with high VoI are the best targets for research: reducing their
    uncertainty would most improve downstream decision quality.

    Args:
        conn: DuckDB connection.
        decision_nodes: List of decision node IDs. If None, auto-detect
            nodes with type='decision' and utility_scale > 0.
        edge_types: Edge types to include (default: CAUSES, INFLUENCES, ENABLES, DEPENDS_ON).
        layers: Optional list of layers to include (e.g., ["L3", "L4"]).
        top: Return only the top N nodes by VoI score. None = all.
        leak_probability: Baseline probability for Bayesian inference.
        root_prior: Prior probability for root nodes.
        timeout: Maximum seconds for ATE computation. Candidates not computed
            within this window fall back to path-confidence sensitivity.
        min_observations: Minimum observation count for reliable VoI estimates.
            Nodes below this threshold are flagged with low_data_warning=True.
            Default 0 (no flagging). Recommended: 3.

    Returns:
        Dict with:
        - method: "value_of_information"
        - decision_nodes: list of decision node IDs used
        - rankings: list of {node_id, label, voi_score, uncertainty, sensitivity,
          sensitivity_method, downstream_decisions, low_data_warning} sorted by voi_score desc
        - n_candidates: total number of candidate nodes
        - mixed_sensitivity_methods: True when rankings mix ATE and path_confidence
          (values are non-comparable; standardize with ?min_ate=1 or add more causal edges)
    """
    if edge_types is None:
        if semantic_roles is not None:
            edge_types = semantic_roles.causal_list()
        else:
            edge_types = ["CAUSES", "INFLUENCES", "ENABLES", "DEPENDS_ON"]

    reader = _coerce_reader(conn)

    # Find decision nodes if not specified
    if decision_nodes is None:
        _dec_nodes = reader.get_nodes(node_type="decision")
        decision_nodes = [n.id for n in _dec_nodes if n.utility_scale is None or n.utility_scale > 0]

    if not decision_nodes:
        return {
            "method": "value_of_information",
            "decision_nodes": [],
            "rankings": [],
            "n_candidates": 0,
            "mixed_sensitivity_methods": False,
            "sensitivity_methods_used": [],
            "message": "No decision nodes found. Create nodes with type='decision' or specify decision_nodes.",
        }

    # Get all CAUSES/INFLUENCES/ENABLES edges for causal traversal
    _edge_records = reader.get_edges(edge_types=edge_types, layers=layers)
    edges = [(e.from_node, e.to_node, e.confidence, e.probability, e.probability_p05, e.probability_p50, e.probability_p95, e.confidence_p05, e.confidence_p50, e.confidence_p95) for e in _edge_records]

    # Build adjacency maps with clear naming:
    # forward_adj: cause → effects (parent → children)
    # reverse_adj: effect → causes (child → parents)
    # edge_pert_variance: stores PERT variance for each edge for VoI uncertainty
    forward_adj: dict[str, list[str]] = {}
    reverse_adj: dict[str, list[str]] = {}
    edge_confidences: dict[tuple[str, str], float] = {}
    edge_pert_variance: dict[tuple[str, str], float] = {}

    for row in edges:
        from_node, to_node, raw_conf, raw_prob, prob_p05, prob_p50, prob_p95, conf_p05, conf_p50, conf_p95 = row
        # from_node CAUSES/INFLUENCES to_node → forward: from_node → to_node
        if from_node not in forward_adj:
            forward_adj[from_node] = []
        forward_adj[from_node].append(to_node)

        if to_node not in reverse_adj:
            reverse_adj[to_node] = []
        reverse_adj[to_node].append(from_node)

        # Compute effective confidence using PERT if available
        eff_conf = 0.7  # default
        if conf_p50 is not None:
            c05_val = float(conf_p05) if conf_p05 is not None else float(conf_p50) * 0.8
            c50_val = float(conf_p50)
            c95_val = float(conf_p95) if conf_p95 is not None else min(1.0, float(conf_p50) * 1.2)
            eff_conf = (c05_val + 4 * c50_val + c95_val) / 6.0
        elif raw_conf is not None:
            eff_conf = float(raw_conf)

        edge_confidences[(from_node, to_node)] = eff_conf

        # Compute PERT variance for uncertainty signal (ADR-013)
        # If PERT columns exist, use variance as uncertainty; else None
        p05 = None
        p95 = None
        if prob_p05 is not None and prob_p95 is not None:
            p05 = float(prob_p05)
            p95 = float(prob_p95)
        elif conf_p05 is not None and conf_p95 is not None:
            p05 = float(conf_p05)
            p95 = float(conf_p95)

        if p05 is not None and p95 is not None:
            spread = p95 - p05
            scaled_variance = _scale_pert_variance(spread)
            edge_pert_variance[(from_node, to_node)] = scaled_variance
        else:
            edge_pert_variance[(from_node, to_node)] = None

    # For each decision node, find causal ancestors via BFS backward (following reverse_adj)
    all_ancestors: dict[str, set[str]] = {}
    for decision in decision_nodes:
        from collections import deque

        visited: set[str] = set()
        queue = deque([decision])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            for cause in reverse_adj.get(node, []):
                if cause not in visited:
                    queue.append(cause)
        visited.discard(decision)  # Don't include the decision itself
        all_ancestors[decision] = visited

    # Collect all candidate nodes (union of all ancestors)
    candidate_nodes = set()
    for ancestors in all_ancestors.values():
        candidate_nodes.update(ancestors)

    if not candidate_nodes:
        return {
            "method": "value_of_information",
            "decision_nodes": decision_nodes,
            "rankings": [],
            "n_candidates": 0,
            "mixed_sensitivity_methods": False,
            "sensitivity_methods_used": [],
            "message": "No causal ancestors found for the specified decision nodes.",
        }

    # Get node labels and confidence for each candidate
    _cand_node_records = reader.get_nodes(ids=list(candidate_nodes))
    node_info = {n.id: {"label": n.label, "confidence": n.confidence if n.confidence is not None else 0.5} for n in _cand_node_records}

    # Fetch decision-node utility: USD value takes precedence over dimensionless utility_scale.
    _dec_node_records = reader.get_nodes(ids=list(decision_nodes))
    decision_utility: dict[str, dict] = {
        n.id: {
            "utility_usd_per_day": n.utility_usd_per_day,
            "utility_currency": n.utility_currency,
            "utility_scale": n.utility_scale,
        }
        for n in _dec_node_records
    }

    # Determine VoI units from decision nodes.
    _usd_count = sum(1 for d in decision_nodes if decision_utility.get(d, {}).get("utility_usd_per_day") is not None)
    if _usd_count == len(decision_nodes):
        voi_units = "usd"
    elif _usd_count == 0:
        voi_units = "dimensionless"
    else:
        voi_units = "mixed"

    # Get observation counts for each candidate (proxy for information quality)
    obs_counts: dict[str, int] = {node_id: len(reader.get_observations(node_id)) for node_id in candidate_nodes}

    # Build shared Bayesian networks per decision node (OHM-27).
    # Instead of building a fresh network for each (candidate, decision) pair in
    # compute_ate, build ONE network per decision that covers all its causal
    # ancestors. Each ATE call then deep-copies and does surgery on the shared
    # pre-built model — avoiding N individual ~500ms network builds.
    _voi_networks: dict[str, dict[str, Any]] = {}
    for decision in decision_nodes:
        _candidates = [n for n in candidate_nodes if n in all_ancestors.get(decision, set())]
        if _candidates:
            _all_preferred: set[tuple[str, str]] = {(c, decision) for c in _candidates}
            _shared = build_bayesian_network(
                reader,
                edge_types=edge_types,
                layers=layers,
                root_nodes=[decision] + _candidates,
                leak_probability=leak_probability,
                root_prior=root_prior,
                semantic_roles=semantic_roles,
                preferred_edges=_all_preferred or None,
                include_soft_evidence=include_soft_evidence,
                soft_edge_types=soft_edge_types,
                customer_id=customer_id,
            )
            if _shared is not None:
                _voi_networks[decision] = _shared

    # Compute VoI for each candidate node
    # Track whether we used ATE or path-confidence fallback for each ranking
    import time as _time

    _deadline = (_time.monotonic() + timeout) if timeout else None
    rankings = []
    for node_id in candidate_nodes:
        info = node_info.get(node_id, {"label": node_id, "confidence": 0.5})
        confidence = info["confidence"]

        # Uncertainty: how uncertain are we about this node?
        # Per ADR-013: use max PERT variance across edges from this node toward decisions.
        # If any edge has PERT data, use the max variance. If no PERT data on any edge,
        # fall back to 1 - confidence.
        # NOTE: ohm_nodes has DEFAULT 1.0 for confidence, so confidence=1.0 means
        # "no explicit belief set", NOT "perfectly known". Treat it as 50% uncertain
        # rather than 0%, otherwise ALL unrated nodes get VoI=0.
        def _conf_to_uncertainty(conf: float) -> float:
            return 0.5 if conf >= 1.0 else max(0.0, 1.0 - conf)

        node_uncertainties = []
        for decision in decision_nodes:
            if node_id in all_ancestors.get(decision, set()):
                path_variance = _max_edge_pert_variance_toward(node_id, decision, forward_adj, edge_pert_variance)
                if path_variance is not None:
                    node_uncertainties.append(path_variance)
                else:
                    node_uncertainties.append(_conf_to_uncertainty(confidence))

        # Use maximum uncertainty across all paths (conservative estimate).
        # Note asymmetry with sensitivity: uncertainty uses max() while
        # sensitivity sums across decisions. This is intentional — uncertainty
        # is a property of the node (we take the worst case), while sensitivity
        # is additive because a node that affects multiple decisions is more
        # valuable to research (the same observation reduces uncertainty for
        # all downstream decisions simultaneously).
        uncertainty = max(node_uncertainties) if node_uncertainties else _conf_to_uncertainty(confidence)

        # Sensitivity: how much does this node affect decisions?
        # Per ADR-013: use |ATE(ancestor → decision)| when possible,
        # falling back to path-weighted confidence product when ATE is unavailable.
        # If deadline exceeded, skip ATE for remaining candidates.
        _ate_allowed = _deadline is None or _time.monotonic() < _deadline
        sensitivity = 0.0
        sensitivity_method = "path_confidence"  # default fallback
        downstream_decisions = []
        for decision in decision_nodes:
            if node_id in all_ancestors.get(decision, set()):
                # Try ATE first (model-based causal effect)
                if not _ate_allowed:
                    ate_result = {"method": "timeout_fallback"}
                else:
                    ate_result = compute_ate(
                        reader,
                        node_id,
                        decision,
                        edge_types=edge_types,
                        layers=layers,
                        leak_probability=leak_probability,
                        root_prior=root_prior,
                        semantic_roles=semantic_roles,
                        customer_id=customer_id,
                        _pre_built_network=_voi_networks.get(decision),
                    )
                _du = decision_utility.get(decision, {})
                _usd = _du.get("utility_usd_per_day")
                _util = _usd if _usd is not None else (_du.get("utility_scale") or 0.5)
                if ate_result.get("method") == "model_based_ate":
                    ate_value = ate_result["ate"]
                    sensitivity += abs(ate_value) * _util
                    sensitivity_method = "ate"
                else:
                    # Fallback: path confidence (minimum edge confidence along best path)
                    path_conf = _path_confidence(node_id, decision, forward_adj, edge_confidences)
                    if path_conf is not None:
                        sensitivity += path_conf * _util
                    else:
                        sensitivity += 0.1 * _util
                downstream_decisions.append(decision)

        # VoI = uncertainty × sensitivity
        voi_score = uncertainty * sensitivity

        obs_count = obs_counts.get(node_id, 0)
        low_data = min_observations > 0 and obs_count < min_observations
        rankings.append(
            {
                "node_id": node_id,
                "label": info["label"],
                "voi_score": round(voi_score, 4),
                "uncertainty": round(uncertainty, 4),
                "sensitivity": round(sensitivity, 4),
                "sensitivity_method": sensitivity_method,
                "confidence": round(confidence, 4),
                "observation_count": obs_count,
                "downstream_decisions": downstream_decisions,
                "n_downstream_decisions": len(downstream_decisions),
                **({"low_data_warning": True, "low_data_note": (f"Only {obs_count} observation(s) — VoI estimate unreliable (min_observations={min_observations})")} if low_data else {}),
            }
        )

    # Sort by VoI score descending
    rankings.sort(key=lambda r: r["voi_score"], reverse=True)

    if top is not None:
        rankings = rankings[:top]

    methods_used = set(r["sensitivity_method"] for r in rankings)
    mixed = len(methods_used) > 1

    return {
        "method": "value_of_information",
        "decision_nodes": decision_nodes,
        "rankings": rankings,
        "n_candidates": len(candidate_nodes),
        "units": voi_units,
        "sensitivity_methods_used": sorted(methods_used),
        **(
            {
                "mixed_sensitivity_methods": True,
                "mixed_methods_note": (
                    "Rankings mix ATE (causal effect size, ~0.02-0.12) and "
                    "path_confidence (edge confidence product, ~0.3-0.5) — "
                    "values are not directly comparable. Check sensitivity_method "
                    "per entry. Add more CAUSES edges or increase timeout for full ATE coverage."
                ),
            }
            if mixed
            else {"mixed_sensitivity_methods": False}
        ),
    }


def generate_voi_tasks(
    conn,
    *,
    agent: str | None = None,
    decision_nodes: list[str] | None = None,
    layers: list[str] | None = None,
    top: int = 5,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """Generate research tasks from VoI rankings, matched to agent expertise.

    OHM-6mv.5: For each under-observed node in the VoI rankings, compute a
    gap_score = (1 - mean_confidence) × downstream_impact. Match to agents
    via tag overlap (CAPABLE_OF, VALUES, GOALS edges). Return ranked research
    targets with suggested actions.

    Args:
        conn: DuckDB connection.
        agent: Optional agent name to filter tasks by expertise match.
        decision_nodes: List of decision node IDs. If None, auto-detect.
        layers: Optional list of layers to include.
        top: Maximum number of tasks to return (default 5).
        leak_probability: Baseline probability for Bayesian inference.
        root_prior: Prior probability for root nodes.

    Returns:
        Dict with:
        - method: "voi_task_assignment"
        - agent: agent name (if specified)
        - tasks: list of research task dicts sorted by gap_score descending
        - n_candidates: total number of candidate nodes
    """
    reader = _coerce_reader(conn)

    # Step 1: Compute VoI rankings
    voi_result = compute_voi(
        reader,
        decision_nodes=decision_nodes,
        layers=layers,
        top=None,  # Get all candidates, we'll filter later
        leak_probability=leak_probability,
        root_prior=root_prior,
        customer_id=customer_id,
    )

    if not voi_result.get("rankings"):
        return {
            "method": "voi_task_assignment",
            "agent": agent,
            "tasks": [],
            "n_candidates": 0,
            "message": voi_result.get("message", "No VoI rankings available."),
        }

    # Step 2: Get agent expertise profile (if agent specified)
    agent_tags: set[str] = set()
    agent_capable_types: set[str] = set()  # node types the agent can handle
    agent_capable_nodes: set[str] = set()  # specific nodes agent is CAPABLE_OF
    agent_workload: int = 0  # open tasks already assigned to agent
    if agent:
        from ohm.validation import validate_identifier

        safe_agent = validate_identifier(agent, name="agent")

        # Capability edges: CAPABLE_OF targets contribute both as tags and as
        # type hints (if the target is a node whose type we can look up)
        _cap_edge_types = ["CAPABLE_OF", "VALUES", "GOALS", "INTERESTED_IN"]
        _all_cap_edges = reader.get_edges(edge_types=_cap_edge_types)
        for e in _all_cap_edges:
            if e.from_node == safe_agent:
                agent_tags.add(e.to_node.lower())
                if e.edge_type == "CAPABLE_OF":
                    agent_capable_nodes.add(e.to_node)

        # Resolve node types for CAPABLE_OF targets
        if agent_capable_nodes:
            _cap_nodes = reader.get_nodes(ids=list(agent_capable_nodes))
            agent_capable_types = {n.type.lower() for n in _cap_nodes if n.type}

        # Agent node tags
        _agent_nodes = reader.get_nodes(ids=[safe_agent])
        if _agent_nodes and _agent_nodes[0].tags:
            try:
                tags_list = _agent_nodes[0].tags
                if isinstance(tags_list, list):
                    agent_tags.update(t.lower() for t in tags_list if isinstance(t, str))
            except (ValueError, TypeError):
                pass

        # Workload: count open tasks assigned to this agent
        _assigned_edges = reader.get_edges(edge_types=["ASSIGNED_TO"])
        agent_workload = sum(1 for e in _assigned_edges if e.to_node == safe_agent)

    # Step 3: Build research tasks from VoI rankings
    tasks = []
    for ranking in voi_result["rankings"]:
        node_id = ranking["node_id"]
        label = ranking.get("label", node_id)
        voi_score = ranking["voi_score"]
        uncertainty = ranking["uncertainty"]
        sensitivity = ranking["sensitivity"]
        confidence = ranking.get("confidence", 0.5)
        obs_count = ranking.get("observation_count", 0)
        downstream = ranking.get("downstream_decisions", [])

        # Gap score: uncertainty × sensitivity.
        # Note: uncertainty already incorporates (1-confidence) for non-PERT nodes,
        # so we do NOT multiply by (1-confidence) again (OHM#4 — double-counting bug).
        # For PERT nodes, uncertainty comes from PERT variance, which is independent of confidence.
        gap_score = uncertainty * sensitivity

        # Retrieve node metadata for capability matching
        _node_records = reader.get_nodes(ids=[node_id])
        _nr = _node_records[0] if _node_records else None
        node_type = (_nr.type or "").lower() if _nr else ""
        node_tags: set[str] = set()
        if _nr and _nr.tags:
            try:
                tags_list = _nr.tags
                if isinstance(tags_list, list):
                    node_tags = {t.lower() for t in tags_list if isinstance(t, str)}
            except (ValueError, TypeError):
                pass

        # Connected concept labels for tag broadening (reuse already-fetched causal edges)
        _concept_edge_types = ["CAUSES", "INFLUENCES", "ENABLES", "DEPENDS_ON"]
        _concept_records = reader.get_edges(edge_types=_concept_edge_types)
        concept_labels = {e.to_node.lower() for e in _concept_records if e.from_node == node_id}

        # Multi-signal capability match score (0.0–1.0)
        all_node_tokens = node_tags | concept_labels | {label.lower(), node_id.lower()}
        if agent_tags:
            matched_tags = list(agent_tags & all_node_tokens)
            tag_overlap = len(matched_tags) / max(len(agent_tags), 1)
        else:
            matched_tags = []
            tag_overlap = 1.0

        # Bonus: direct type match via CAPABLE_OF
        type_match = node_type in agent_capable_types if agent_capable_types else False
        node_match = node_id in agent_capable_nodes if agent_capable_nodes else False
        capability_score = min(1.0, tag_overlap + (0.3 if type_match else 0.0) + (0.2 if node_match else 0.0))

        # Skip if agent filter and no capability signal at all
        if agent and capability_score == 0.0:
            continue

        # Workload penalty: reduce score slightly for heavily-loaded agents
        workload_factor = max(0.5, 1.0 - agent_workload * 0.05) if agent else 1.0

        final_score = gap_score * capability_score * workload_factor

        # Suggest research action
        if obs_count == 0:
            suggested_research = f"Observe {label}: no observations exist yet"
        elif obs_count < 3:
            suggested_research = f"Add {3 - obs_count} more observations to {label}: only {obs_count} observation(s)"
        elif confidence < 0.3:
            suggested_research = f"Challenge low-confidence claims about {label}: confidence={confidence:.2f}"
        elif confidence < 0.6:
            suggested_research = f"Validate moderate-confidence claims about {label}: confidence={confidence:.2f}"
        else:
            suggested_research = f"Refine understanding of {label}: confidence={confidence:.2f}"

        tasks.append(
            {
                "node_id": node_id,
                "label": label,
                "voi_score": voi_score,
                "gap_score": round(gap_score, 4),
                "final_score": round(final_score, 4),
                "uncertainty": uncertainty,
                "sensitivity": sensitivity,
                "confidence": confidence,
                "observation_count": obs_count,
                "downstream_decisions": downstream,
                "n_downstream_decisions": len(downstream),
                "matched_tags": matched_tags,
                "tag_overlap": round(tag_overlap, 4),
                "capability_score": round(capability_score, 4),
                "type_match": type_match,
                "agent_workload": agent_workload if agent else None,
                "suggested_research": suggested_research,
            }
        )

    # Sort by final_score descending (highest value + capability + availability first)
    tasks.sort(key=lambda t: t["final_score"], reverse=True)

    # Limit to top N
    tasks = tasks[:top]

    return {
        "method": "voi_task_assignment",
        "agent": agent,
        "tasks": tasks,
        "n_candidates": voi_result["n_candidates"],
    }


def _max_edge_pert_variance_toward(
    source: str,
    target: str,
    forward_adj: dict[str, list[str]],
    edge_pert_variance: dict[tuple[str, str], float],
) -> float | None:
    """Find the maximum PERT variance among edges along any path from source to target.

    Uses BFS with memoized DP to find the maximum edge variance along any valid
    path. A path only contributes if ALL edges on it have PERT variance data.

    For each node, we track two things via BFS:
    - best_max_var: the maximum edge variance seen along any all-pert path reaching it
    - all_have_pert: whether every edge on that best path has PERT data

    We keep exploring when a new path yields a higher max variance than the
    current best for that node. Complexity: O(V + E) amortized instead of
    exponential path enumeration.
    """
    from collections import deque

    _MAX_DEPTH = 10

    # best[node] = (max_edge_var, all_have_pert) — best path reaching this node
    best: dict[str, tuple[float, bool]] = {}
    best[source] = (0.0, True)

    queue: deque[tuple[str, float, bool, int]] = deque([(source, 0.0, True, 0)])

    while queue:
        node, max_var, all_have_pert, depth = queue.popleft()
        if depth >= _MAX_DEPTH:
            continue

        current_best = best.get(node)
        if current_best is not None and (current_best[0] > max_var or (current_best[0] == max_var and current_best[1] and not all_have_pert)):
            continue

        for neighbor in forward_adj.get(node, []):
            edge_var = edge_pert_variance.get((node, neighbor), None)
            new_all_have_pert = all_have_pert and (edge_var is not None)

            if edge_var is not None:
                new_max_var = max(max_var, edge_var)
            else:
                new_max_var = max_var

            prev = best.get(neighbor)
            improved = prev is None or new_max_var > prev[0] or (new_max_var == prev[0] and new_all_have_pert and not prev[1])
            if improved:
                best[neighbor] = (new_max_var, new_all_have_pert)
                queue.append((neighbor, new_max_var, new_all_have_pert, depth + 1))

    target_entry = best.get(target)
    if target_entry is not None and target_entry[1]:
        return target_entry[0] if target_entry[0] > 0.0 else None
    return None


def _path_confidence(
    source: str,
    target: str,
    forward_adj: dict[str, list[str]],
    edge_confidences: dict[tuple[str, str], float],
) -> float | None:
    """Find the minimum edge confidence along the best path from source to target.

    Uses BFS to find a path from source to target following forward adjacency
    (cause → effect), then returns the minimum edge confidence along that path.
    Returns None if no path exists.
    """
    from collections import deque

    queue = deque([(source, [source])])
    visited = {source}

    while queue:
        node, path = queue.popleft()
        for neighbor in forward_adj.get(node, []):
            if neighbor == target:
                full_path = path + [neighbor]
                # Compute minimum edge confidence along path
                min_conf = float("inf")
                for i in range(len(full_path) - 1):
                    edge_conf = edge_confidences.get((full_path[i], full_path[i + 1]), 0.5)
                    min_conf = min(min_conf, edge_conf)
                return min_conf if min_conf != float("inf") else 0.5
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    return None  # No path found


class BayesianContext:
    """Context manager that builds a Bayesian network once and reuses it.

    OHM-7bc: Instead of rebuilding the network for every inference call,
    BayesianContext builds it once and exposes methods that reuse the cached
    network. This avoids redundant database queries and network construction
    when performing multiple analyses on the same graph.

    Usage:
        with BayesianContext(conn, edge_types=["CAUSES"], layers=["L3"]) as ctx:
            result1 = ctx.inference("outcome", {"cause": 1})
            result2 = ctx.intervention("cause", 0)
            ate = ctx.ate("cause", "outcome")

    Args:
        conn: DuckDB connection.
        edge_types: Edge types to include in the network.
        layers: Optional list of layers to include.
        root_nodes: Optional list of root node IDs to scope the network.
        max_nodes: Maximum number of nodes to include.
        leak_probability: Baseline probability of bad outcome when all
            parents are good (default 0.15).
        default_probability: Probability for edges without values (default 0.5).
        root_prior: Default prior for root nodes (default 0.3).
    """

    def __init__(
        self,
        conn,
        *,
        edge_types: list[str] | None = None,
        layers: list[str] | None = None,
        root_nodes: list[str] | None = None,
        max_nodes: int = 50,
        leak_probability: float = 0.15,
        default_probability: float = 0.5,
        root_prior: float = 0.3,
        include_soft_evidence: bool = False,
        soft_edge_types: list[str] | None = None,
    ):
        self._reader = _coerce_reader(conn)
        self._edge_types = edge_types
        self._layers = layers
        self._leak_probability = leak_probability
        self._default_probability = default_probability
        self._root_prior = root_prior

        # Build the network once
        self._network = build_bayesian_network(
            self._reader,
            root_nodes=root_nodes,
            edge_types=edge_types,
            layers=layers,
            max_nodes=max_nodes,
            leak_probability=leak_probability,
            default_probability=default_probability,
            root_prior=root_prior,
            include_soft_evidence=include_soft_evidence,
            soft_edge_types=soft_edge_types,
        )

    @property
    def network(self) -> dict[str, Any] | None:
        """The cached Bayesian network dict, or None if no edges found."""
        return self._network

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def inference(self, target: str, evidence: dict[str, int]) -> dict[str, Any]:
        """Run Bayesian inference reusing the cached network.

        Args:
            target: Node ID to compute posterior for.
            evidence: Dict mapping node IDs to observed states (0=bad, 1=good).

        Returns:
            Dict with posterior probabilities, network info, and method.
        """
        if self._network is None:
            return {
                "method": "none",
                "pgmpy_available": PGMPY_AVAILABLE,
                "target": target,
                "evidence": evidence,
                "error": "No probability-bearing edges found in graph",
            }

        target = validate_identifier(target, name="target")
        self._network["safe_names"]
        safe_target = _safe_node_id(target)

        if safe_target not in self._network["variables"]:
            return {
                "method": "none",
                "pgmpy_available": PGMPY_AVAILABLE,
                "target": target,
                "evidence": evidence,
                "error": f"Target node {target} not in Bayesian network",
            }

        # Convert evidence to safe names
        safe_evidence = {}
        for node_id, state in evidence.items():
            safe = _safe_node_id(validate_identifier(node_id, name="evidence_node"))
            if safe in self._network["variables"]:
                safe_evidence[safe] = int(state)

        model = self._network["model"]

        try:
            _ve_key = id(model)
            if _ve_key not in _ve_cache:
                if len(_ve_cache) >= _MAX_VE_CACHE_SIZE:
                    _ve_cache.clear()
                _ve_cache[_ve_key] = VariableElimination(model)
            infer = _ve_cache[_ve_key]
            soft_factors = self._network.get("soft_evidence_factors", [])
            query_kwargs: dict[str, Any] = {"variables": [safe_target], "evidence": safe_evidence}
            if soft_factors:
                query_kwargs["virtual_evidence"] = soft_factors
            result = infer.query(**query_kwargs)

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
                    "n_nodes": self._network["n_nodes"],
                    "n_edges": self._network["n_edges"],
                    "root_nodes": self._network["root_nodes"],
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
                "pgmpy_available": PGMPY_AVAILABLE,
                "target": target,
                "evidence": evidence,
                "error": str(e),
            }

    def intervention(self, target: str, intervention_state: int, *, query_nodes: list[str] | None = None) -> dict[str, Any]:
        """Run causal intervention reusing the cached network.

        Args:
            target: Node ID to intervene on.
            intervention_state: State to set the target to (0=bad, 1=good).
            query_nodes: Optional list of downstream nodes to query.

        Returns:
            Dict with posterior probabilities and comparison with observation.
        """
        if self._network is None:
            return {
                "method": "none",
                "pgmpy_available": PGMPY_AVAILABLE,
                "target": target,
                "intervention_state": intervention_state,
                "error": "No probability-bearing edges found in graph",
            }

        if intervention_state not in (0, 1):
            return {
                "method": "error",
                "target": target,
                "error": f"intervention_state must be 0 or 1, got {intervention_state}",
            }

        target = validate_identifier(target, name="target")
        safe_names = self._network["safe_names"]
        safe_target = _safe_node_id(target)

        if safe_target not in self._network["variables"]:
            return {
                "method": "none",
                "pgmpy_available": True,
                "target": target,
                "intervention_state": intervention_state,
                "error": f"Target {target} not in Bayesian network",
            }

        network = self._network
        model = network["model"]
        original_edges = list(model.edges())

        # Identify and sever incoming edges to target (do-operator)
        incoming_edges = [(u, v) for u, v in original_edges if v == safe_target]
        model_do = model.copy()
        for edge in incoming_edges:
            if model_do.has_edge(*edge):
                model_do.remove_edge(*edge)

        # Rebuild CPDs for the mutilated graph
        from pgmpy.factors.discrete import TabularCPD as _TabularCPD

        new_cpds = []

        # Set target's CPT to deterministic (intervention_state)
        if intervention_state == 0:
            target_cpd = _TabularCPD(safe_target, 2, [[1.0], [0.0]])
        else:
            target_cpd = _TabularCPD(safe_target, 2, [[0.0], [1.0]])
        new_cpds.append(target_cpd)

        # Copy over all other CPTs (unchanged by graph surgery)
        for cpd in model.get_cpds():
            if cpd.variable != safe_target:
                cpd_evidence = getattr(cpd, "evidence", None)
                getattr(cpd, "evidence_card", None)
                if cpd_evidence:
                    removed_parents = set(e[0] for e in incoming_edges)
                    still_valid = [v for v in cpd_evidence if v not in removed_parents]
                    if len(still_valid) != len(cpd_evidence):
                        if len(still_valid) == 0:
                            n_states = cpd.variable_card
                            values = cpd.get_values()
                            marginal = values.mean(axis=1)
                            new_cpd = _TabularCPD(cpd.variable, n_states, [[marginal[s]] for s in range(n_states)])
                            new_cpds.append(new_cpd)
                        else:
                            logger.warning(f"Cannot marginalize partially-removed parents for {cpd.variable}")
                            new_cpds.append(cpd)
                    else:
                        new_cpds.append(cpd)
                else:
                    new_cpds.append(cpd)

        model_do.cpds = []
        try:
            model_do.add_cpds(*new_cpds)
            assert model_do.check_model()
        except Exception as e:
            logger.error(f"Mutilated graph model check failed: {e}")
            return {
                "method": "error",
                "pgmpy_available": True,
                "target": target,
                "intervention_state": intervention_state,
                "error": f"Graph surgery failed: {e}",
                "incoming_edges_severed": len(incoming_edges),
            }

        # Determine query nodes
        safe_query_nodes = []
        missing_query_nodes = []
        if query_nodes:
            for qn in query_nodes:
                safe_qn = _safe_node_id(validate_identifier(qn, name="query_node"))
                if safe_qn in network["variables"]:
                    safe_query_nodes.append(safe_qn)
                else:
                    missing_query_nodes.append(qn)

        if not safe_query_nodes:
            if missing_query_nodes:
                return {
                    "method": "error",
                    "pgmpy_available": True,
                    "target": target,
                    "intervention_state": intervention_state,
                    "error": (f"Query node(s) {missing_query_nodes} not in Bayesian network (network has {network['n_nodes']} nodes)"),
                    "incoming_edges_severed": len(incoming_edges),
                }
            try:
                import networkx as nx

                descendants = nx.descendants(model_do, safe_target)
                safe_query_nodes = list(descendants)
            except Exception:
                safe_query_nodes = [v for v in network["variables"] if v != safe_target]

        if not safe_query_nodes:
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

        # Run inference on the mutilated graph
        soft_factors = network.get("soft_evidence_factors", [])
        try:
            _ve_key_do = id(model_do)
            if _ve_key_do not in _ve_cache:
                if len(_ve_cache) >= _MAX_VE_CACHE_SIZE:
                    _ve_cache.clear()
                _ve_cache[_ve_key_do] = VariableElimination(model_do)
            infer = _ve_cache[_ve_key_do]
            do_kwargs: dict[str, Any] = {"variables": safe_query_nodes, "evidence": {safe_target: intervention_state}}
            if soft_factors:
                do_kwargs["virtual_evidence"] = soft_factors
            result = infer.query(**do_kwargs)

            posteriors = {}
            if len(safe_query_nodes) == 1:
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
                for qn in safe_query_nodes:
                    qn_original = None
                    for orig, safe in safe_names.items():
                        if safe == qn:
                            qn_original = orig
                            break
                    try:
                        single_kwargs: dict[str, Any] = {"variables": [qn], "evidence": {safe_target: intervention_state}}
                        if soft_factors:
                            single_kwargs["virtual_evidence"] = soft_factors
                        result_single = infer.query(**single_kwargs)
                        posteriors[qn_original or qn] = {
                            "good": round(float(result_single.values[1]), 4),
                            "bad": round(float(result_single.values[0]), 4),
                        }
                    except Exception:
                        posteriors[qn_original or qn] = {"error": "inference failed for this node"}

            # Observation comparison using original model
            comparison = {}
            try:
                obs_model = network["model"]
                _obs_ve_key = id(obs_model)
                if _obs_ve_key not in _ve_cache:
                    if len(_ve_cache) >= _MAX_VE_CACHE_SIZE:
                        _ve_cache.clear()
                    _ve_cache[_obs_ve_key] = VariableElimination(obs_model)
                obs_infer = _ve_cache[_obs_ve_key]
                for node_id, post in posteriors.items():
                    if isinstance(post, dict) and "error" not in post:
                        safe_qn = None
                        for orig, safe in safe_names.items():
                            if orig == node_id:
                                safe_qn = safe
                                break
                        if not safe_qn:
                            continue
                        try:
                            obs_kwargs: dict[str, Any] = {"variables": [safe_qn], "evidence": {safe_target: intervention_state}}
                            if soft_factors:
                                obs_kwargs["virtual_evidence"] = soft_factors
                            obs_result = obs_infer.query(**obs_kwargs)
                            obs_bad = round(float(obs_result.values[0]), 4)
                            int_bad = post.get("bad", None)
                            if int_bad is not None:
                                comparison[node_id] = {
                                    "intervention_bad": int_bad,
                                    "observation_bad": obs_bad,
                                    "confounding_bias": round(obs_bad - int_bad, 4),
                                    "interpretation": "positive bias = observation overestimates causal effect" if obs_bad > int_bad else "negative bias = observation underestimates causal effect",
                                }
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"Observation comparison failed: {e}")

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
        except Exception as e:
            logger.error(f"Causal intervention failed: {e}")
            return {
                "method": "error",
                "pgmpy_available": True,
                "target": target,
                "intervention_state": intervention_state,
                "error": str(e),
            }

    def ate(self, cause: str, effect: str) -> dict[str, Any]:
        """Compute Average Treatment Effect reusing the cached network.

        Args:
            cause: Node ID for the treatment variable.
            effect: Node ID for the outcome variable.

        Returns:
            Dict with ATE, risk ratio, and network info.
        """
        if self._network is None:
            return {
                "method": "none",
                "pgmpy_available": PGMPY_AVAILABLE,
                "cause": cause,
                "effect": effect,
                "error": "No probability-bearing edges found in graph",
            }

        # Use intervention() twice instead of calling compute_ate() which
        # would rebuild the network each time
        do_bad = self.intervention(cause, 0, query_nodes=[effect])
        do_good = self.intervention(cause, 1, query_nodes=[effect])

        # Extract posteriors
        p_bad_do_bad = do_bad.get("posterior", {}).get(effect, {}).get("bad")
        p_bad_do_good = do_good.get("posterior", {}).get(effect, {}).get("bad")

        if p_bad_do_bad is None or p_bad_do_good is None:
            error_msg = do_bad.get("error") or do_good.get("error")
            if not error_msg:
                bad_keys = list(do_bad.get("posterior", {}).keys())
                good_keys = list(do_good.get("posterior", {}).keys())
                error_msg = f"Effect node '{effect}' not found in intervention posteriors. do(bad) has keys {bad_keys}, do(good) has keys {good_keys}. The effect node may not be in the Bayesian network."
            return {
                "method": "error",
                "cause": cause,
                "effect": effect,
                "error": error_msg,
            }

        ate = p_bad_do_bad - p_bad_do_good
        risk_ratio = p_bad_do_bad / p_bad_do_good if p_bad_do_good > 0 else float("inf")

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
            "network_info": {
                "n_nodes": self._network["n_nodes"],
                "n_edges": self._network["n_edges"],
                "uses_cached_network": True,
            },
        }

    def sensitivity(self, cause: str, effect: str) -> dict[str, Any]:
        """Compute sensitivity analysis (E-value) reusing the cached network.

        Args:
            cause: Node ID for the treatment variable.
            effect: Node ID for the outcome variable.

        Returns:
            Dict with E-value, risk ratio, and robustness assessment.
        """
        import math

        if self._network is None:
            return {
                "method": "none",
                "pgmpy_available": PGMPY_AVAILABLE,
                "cause": cause,
                "effect": effect,
                "error": "No probability-bearing edges found in graph",
            }

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")

        # Use self.ate() which now uses cached network
        ate_result = self.ate(cause, effect)

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
            robustness_desc = f"Moderate robustness — confounder needs RR>={e_value:.2f} with both cause and effect"
        elif e_value < 3.0:
            robustness = "strong"
            robustness_desc = f"Strong robustness — confounder needs RR>={e_value:.2f} with both cause and effect"
        else:
            robustness = "very_strong"
            robustness_desc = f"Very strong robustness — confounder needs RR>={e_value:.2f} with both cause and effect"

        # Perturbation analysis
        perturbation_levels = [1.0, 1.2, 1.5, 2.0, 3.0, 5.0, 10.0]
        perturbation_results = []
        for conf_strength in perturbation_levels:
            if conf_strength == 1.0:
                adjusted_ate = round(ate, 4)
            elif risk_ratio > 0 and conf_strength > 1.0:
                adjusted_rr = risk_ratio / conf_strength
                if p_bad_do_good > 0:
                    adjusted_p_bad_do_bad = min(1.0, p_bad_do_good * adjusted_rr)
                    adjusted_ate = round(adjusted_p_bad_do_bad - p_bad_do_good, 4)
                else:
                    adjusted_ate = round(ate, 4)
            else:
                adjusted_ate = round(ate, 4)

            perturbation_results.append(
                {
                    "confounder_strength": conf_strength,
                    "adjusted_ate": adjusted_ate,
                    "ate_zero": abs(adjusted_ate) < 1e-6,
                }
            )

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
            "network_info": {
                "n_nodes": self._network["n_nodes"],
                "n_edges": self._network["n_edges"],
                "uses_cached_network": True,
            },
        }

    def adjustment_sets(self, cause: str, effect: str, *, max_network_size: int = 10) -> dict[str, Any]:
        """Find valid backdoor/frontdoor adjustment sets reusing the cached network.

        Args:
            cause: Node ID for the treatment variable.
            effect: Node ID for the outcome variable.
            max_network_size: Maximum network size for adjustment set search.

        Returns:
            Dict with adjustment sets and network info.
        """
        return find_adjustment_sets(
            self._reader,
            cause,
            effect,
            edge_types=self._edge_types,
            layers=self._layers,
            leak_probability=self._leak_probability,
            root_prior=self._root_prior,
            max_network_size=max_network_size,
        )
