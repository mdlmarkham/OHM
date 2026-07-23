"""Causal structure discovery from observation data.

Discovers candidate causal edges from OHM observation data using
constraint-based (PC) and score-based (GES) methods from causal-learn.

OHM-od01.4: Structure Learning — Causal Discovery from Observation Data.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

logger = logging.getLogger(__name__)


def _require_numpy() -> None:
    if not NUMPY_AVAILABLE:
        raise ImportError("numpy is required for causal discovery. Install with: pip install numpy")


def _build_observation_matrix(
    conn,
    node_ids: list[str],
    *,
    min_observations: int = 5,
) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    """Build an observation matrix from OHM observations.

    Args:
        conn: DuckDB connection or GraphReader.
        node_ids: Node IDs to include in discovery.
        min_observations: Minimum observations per node (default 5).

    Returns:
        Tuple of (data_matrix, valid_node_ids, metadata).
        data_matrix is (n_observations x n_nodes) binary matrix.
        metadata includes skipped nodes and observation counts.
    """
    from ohm.graph_reader import coerce_reader

    reader = coerce_reader(conn)

    # Collect observations for each node
    node_obs = {}
    metadata = {"requested": node_ids, "skipped": {}, "observation_counts": {}}

    for node_id in node_ids:
        obs = reader.get_observations(node_id)
        metadata["observation_counts"][node_id] = len(obs)
        if len(obs) >= min_observations:
            # Binary: value > 0.5 → 1 (good), else → 0 (bad)
            values = []
            for o in obs:
                v = o.value if hasattr(o, "value") else o.get("value")
                values.append(1 if v is not None and v > 0.5 else 0)
            node_obs[node_id] = values
        else:
            metadata["skipped"][node_id] = f"insufficient observations ({len(obs)} < {min_observations})"

    valid_nodes = list(node_obs.keys())
    if len(valid_nodes) < 2:
        return np.array([]).reshape(0, 0), valid_nodes, metadata

    # Align: use the shortest observation sequence (truncated)
    min_len = min(len(v) for v in node_obs.values())
    if min_len < min_observations:
        metadata["warning"] = f"Common sequence length ({min_len}) below minimum ({min_observations})"

    # Build matrix (n_observations x n_nodes)
    data = np.array([node_obs[n][:min_len] for n in valid_nodes], dtype=np.float64).T
    return data, valid_nodes, metadata


def discover_pc(
    conn,
    node_ids: list[str],
    *,
    alpha: float = 0.05,
    min_observations: int = 5,
    indep_test: str = "gsq",
) -> dict[str, Any]:
    """Discover causal structure using PC algorithm.

    Constraint-based discovery: test conditional independence between all
    pairs, build skeleton, orient edges via v-structures.

    Note: For binary observation data, use 'gsq' (G-squared) or 'chisq'
    instead of 'fisherz' (which requires continuous data).

    Args:
        conn: DuckDB connection or GraphReader.
        node_ids: Nodes to include in discovery.
        alpha: Significance level for independence tests (default 0.05).
        min_observations: Minimum observations per node (default 5).
        indep_test: Independence test name (gsq, chisq, fisherz).

    Returns:
        Dict with candidate_edges, skeleton, metadata.
    """
    _require_numpy()
    data, valid_nodes, metadata = _build_observation_matrix(conn, node_ids, min_observations=min_observations)

    if len(valid_nodes) < 2:
        return {
            "method": "pc",
            "n_nodes": len(valid_nodes),
            "n_observations": int(data.shape[0]) if data.size > 0 else 0,
            "candidate_edges": [],
            "metadata": metadata,
            "error": "Need at least 2 nodes with sufficient observations",
        }

    try:
        from causallearn.search.ConstraintBased import PC

        # Use gsq/chisq for binary data, fisherz for continuous
        test_name = indep_test
        if test_name not in ("gsq", "chisq", "fisherz"):
            test_name = "gsq"

        cg = PC.pc(data, alpha=alpha, indep_test=test_name, node_names=valid_nodes)

        # Extract edges from the discovered graph
        edges = []

        # Use find_fully_directed for directed edges
        directed = cg.find_fully_directed()
        for pair in directed:
            i, j = pair
            if i < len(valid_nodes) and j < len(valid_nodes):
                edges.append(
                    {
                        "from": valid_nodes[i],
                        "to": valid_nodes[j],
                        "edge_type": "directed",
                        "confidence": round(1 - alpha, 2),
                        "provenance": "structure_learning",
                        "method": "pc",
                    }
                )

        # Use find_undirected for undirected edges (skeleton)
        undirected = cg.find_undirected()
        for pair in undirected:
            i, j = pair
            if i < len(valid_nodes) and j < len(valid_nodes):
                edges.append(
                    {
                        "from": valid_nodes[i],
                        "to": valid_nodes[j],
                        "edge_type": "undirected",
                        "confidence": round(1 - alpha, 2),
                        "provenance": "structure_learning",
                        "method": "pc",
                    }
                )

        metadata["alpha"] = alpha
        metadata["indep_test"] = test_name

        return {
            "method": "pc",
            "n_nodes": len(valid_nodes),
            "n_observations": int(data.shape[0]),
            "candidate_edges": edges,
            "skeleton_size": len(edges),
            "metadata": metadata,
        }

    except Exception as e:
        logger.error("PC discovery failed: %s", e, exc_info=True)
        return {
            "method": "pc",
            "n_nodes": len(valid_nodes),
            "n_observations": int(data.shape[0]) if data.size > 0 else 0,
            "candidate_edges": [],
            "metadata": metadata,
            "error": str(e),
        }


def discover_ges(
    conn,
    node_ids: list[str],
    *,
    min_observations: int = 5,
    score_class: str = "local_score_BIC",
) -> dict[str, Any]:
    """Discover causal structure using Greedy Equivalence Search.

    Score-based discovery: search over DAG equivalence classes,
    scoring by BIC or other criteria.

    Args:
        conn: DuckDB connection or GraphReader.
        node_ids: Nodes to include in discovery.
        min_observations: Minimum observations per node (default 5).
        score_class: Score function (local_score_BIC, local_score_BDeu).

    Returns:
        Dict with candidate_edges, metadata.
    """
    _require_numpy()
    data, valid_nodes, metadata = _build_observation_matrix(conn, node_ids, min_observations=min_observations)

    if len(valid_nodes) < 2:
        return {
            "method": "ges",
            "n_nodes": len(valid_nodes),
            "n_observations": int(data.shape[0]) if data.size > 0 else 0,
            "candidate_edges": [],
            "metadata": metadata,
            "error": "Need at least 2 nodes with sufficient observations",
        }

    try:
        from causallearn.search.ScoreBased import GES

        cg = GES.ges(data, score_func=score_class, node_names=valid_nodes)

        # Extract directed edges from GES result
        edges = []
        directed = cg.find_fully_directed()
        for pair in directed:
            i, j = pair
            if i < len(valid_nodes) and j < len(valid_nodes):
                edges.append(
                    {
                        "from": valid_nodes[i],
                        "to": valid_nodes[j],
                        "edge_type": "directed",
                        "confidence": 0.7,
                        "provenance": "structure_learning",
                        "method": "ges",
                    }
                )

        undirected = cg.find_undirected()
        for pair in undirected:
            i, j = pair
            if i < len(valid_nodes) and j < len(valid_nodes):
                edges.append(
                    {
                        "from": valid_nodes[i],
                        "to": valid_nodes[j],
                        "edge_type": "undirected",
                        "confidence": 0.6,
                        "provenance": "structure_learning",
                        "method": "ges",
                    }
                )

        metadata["score_class"] = score_class

        return {
            "method": "ges",
            "n_nodes": len(valid_nodes),
            "n_observations": int(data.shape[0]),
            "candidate_edges": edges,
            "skeleton_size": len(edges),
            "metadata": metadata,
        }

    except Exception as e:
        logger.error("GES discovery failed: %s", e, exc_info=True)
        return {
            "method": "ges",
            "n_nodes": len(valid_nodes),
            "n_observations": int(data.shape[0]) if data.size > 0 else 0,
            "candidate_edges": [],
            "metadata": metadata,
            "error": str(e),
        }


def discover_causal(
    conn,
    node_ids: list[str] | None = None,
    *,
    method: str = "pc",
    alpha: float = 0.05,
    min_observations: int = 5,
    indep_test: str = "gsq",
    score_class: str = "local_score_BIC",
) -> dict[str, Any]:
    """Discover causal structure from observation data.

    Main entry point for causal discovery. Selects method (PC or GES)
    and returns candidate edges for agent review.

    Args:
        conn: DuckDB connection or GraphReader.
        node_ids: Nodes to include. If None, auto-selects nodes with ≥ min_observations.
        method: Discovery method (pc, ges, or both).
        alpha: Significance level for PC (default 0.05).
        min_observations: Minimum observations per node (default 5).
        indep_test: Independence test for PC (gsq, chisq, fisherz).
        score_class: Score function for GES.

    Returns:
        Dict with candidate_edges, method, metadata.
    """
    _require_numpy()
    from ohm.graph_reader import coerce_reader

    reader = coerce_reader(conn)

    if node_ids is None:
        all_nodes = reader.get_all_nodes()
        all_ids = [n.id if hasattr(n, "id") else n.get("id", n.get("node_id", "")) for n in all_nodes]
        obs_counts = reader.get_observation_counts(all_ids)
        node_ids = [nid for nid, cnt in obs_counts.items() if cnt >= min_observations]
        if len(node_ids) < 2:
            return {
                "method": method,
                "n_nodes": len(node_ids),
                "candidate_edges": [],
                "metadata": {"observation_counts": obs_counts},
                "error": f"Only {len(node_ids)} nodes have ≥{min_observations} observations; need ≥2",
            }

    results = {}

    if method in ("pc", "both"):
        results["pc"] = discover_pc(
            conn,
            node_ids,
            alpha=alpha,
            min_observations=min_observations,
            indep_test=indep_test,
        )

    if method in ("ges", "both"):
        try:
            results["ges"] = discover_ges(
                conn,
                node_ids,
                min_observations=min_observations,
                score_class=score_class,
            )
        except (TypeError, ValueError) as e:
            # GES can fail with numpy compatibility errors (causal-learn + numpy 2.x)
            results["ges"] = {"candidate_edges": [], "ges_error": str(e)}
            logger.warning(f"GES discovery failed: {e}")

    if method == "pc":
        pc_result = results["pc"]
        if pc_result.get("error") or not pc_result.get("candidate_edges"):
            logger.info("PC produced no edges, falling back to GES")
            try:
                results["ges"] = discover_ges(
                    conn,
                    node_ids,
                    min_observations=min_observations,
                    score_class=score_class,
                )
            except (TypeError, ValueError) as e:
                results["ges"] = {"candidate_edges": [], "ges_error": str(e)}
                logger.warning(f"GES fallback failed: {e}")
            ges_result = results["ges"]
            if ges_result.get("candidate_edges"):
                ges_result["fallback_from"] = "pc"
                ges_result["pc_error"] = pc_result.get("error", "no candidate edges found")
                return ges_result
        return pc_result

    if method == "both":
        pc_edges = results.get("pc", {}).get("candidate_edges", [])
        ges_edges = results.get("ges", {}).get("candidate_edges", [])

        seen = set()
        merged = []
        for e in pc_edges:
            key = (e["from"], e["to"])
            if key not in seen:
                seen.add(key)
                merged.append(e)
        for e in ges_edges:
            key = (e["from"], e["to"])
            if key not in seen:
                seen.add(key)
                e["confidence"] = round(e.get("confidence", 0.7) * 0.8, 2)
                merged.append(e)

        return {
            "method": "both",
            "n_nodes": len(node_ids),
            "candidate_edges": merged,
            "pc_results": results.get("pc", {}),
            "ges_results": results.get("ges", {}),
        }

    return results.get(method, {})
