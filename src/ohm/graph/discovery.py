"""
OHM Causal Discovery Module (OHM-od01.4)

Discovers candidate causal relationships from observation data using:
- PC algorithm (constraint-based, causal-learn)
- GES (score-based, causal-learn)
- Correlation-based fallback (for sparse data)

All candidates go to a discovery queue for agent review — never auto-added.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryCandidate:
    """A candidate causal edge discovered from observation data."""

    from_node: str
    to_node: str
    method: str  # "pc", "ges", "correlation"
    confidence: float  # 0-1, from statistical test or score
    p_value: Optional[float] = None
    edge_type: str = "SUGGESTED_CAUSES"  # proposed edge type
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_node": self.from_node,
            "to_node": self.to_node,
            "method": self.method,
            "confidence": self.confidence,
            "p_value": self.p_value,
            "edge_type": self.edge_type,
            "metadata": self.metadata,
        }


@dataclass
class DiscoveryResult:
    """Result of a causal discovery run."""

    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    method: str = ""
    nodes_used: list[str] = field(default_factory=list)
    observation_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "method": self.method,
            "nodes_used": self.nodes_used,
            "observation_count": self.observation_count,
            "warnings": self.warnings,
            "total_candidates": len(self.candidates),
        }


def build_observation_matrix(
    observations: list[dict[str, Any]],
    node_ids: list[str],
    bin_days: int = 1,
) -> tuple[np.ndarray, list[str], list[str]]:
    """Build an N×T observation matrix from OHM observations.

    Bins observations by day, filling missing values with last known value
    (forward-fill). Returns (matrix, node_labels, time_labels).

    Args:
        observations: List of observation dicts with node_id, value, created_at.
        node_ids: Ordered list of node IDs to include.
        bin_days: Number of days per time bin (default 1).

    Returns:
        (matrix, node_labels, time_labels) where matrix is N×T float array.
    """
    if not observations or not node_ids:
        return np.array([]).reshape(0, 0), [], []

    node_index = {nid: i for i, nid in enumerate(node_ids)}
    n_nodes = len(node_ids)

    # Parse timestamps and bin by day
    obs_by_time: dict[str, dict[int, float]] = {}  # date_str -> {node_idx: value}

    for obs in observations:
        nid = obs.get("node_id", "")
        if nid not in node_index:
            continue

        created = obs.get("created_at", "")
        if not created:
            continue

        # Parse timestamp
        try:
            if isinstance(created, str):
                created = created.replace("Z", "+00:00")
                dt = datetime.fromisoformat(created)
            elif isinstance(created, datetime):
                dt = created
            else:
                continue
        except (ValueError, TypeError):
            continue

        # Bin by day
        from datetime import timedelta

        day_str = (dt - timedelta(days=dt.hour // bin_days if bin_days > 1 else 0)).strftime(
            "%Y-%m-%d"
        )
        if day_str not in obs_by_time:
            obs_by_time[day_str] = {}

        val = obs.get("value")
        if val is not None:
            obs_by_time[day_str][node_index[nid]] = float(val)

    if not obs_by_time:
        return np.array([]).reshape(n_nodes, 0), node_ids, []

    # Sort days
    sorted_days = sorted(obs_by_time.keys())
    n_days = len(sorted_days)

    # Build matrix with forward-fill
    matrix = np.full((n_nodes, n_days), np.nan)
    last_known = np.full(n_nodes, np.nan)

    for t, day in enumerate(sorted_days):
        day_obs = obs_by_time[day]
        for idx, val in day_obs.items():
            matrix[idx, t] = val
            last_known[idx] = val

        # Forward-fill missing values
        for idx in range(n_nodes):
            if np.isnan(matrix[idx, t]) and not np.isnan(last_known[idx]):
                matrix[idx, t] = last_known[idx]

    return matrix, node_ids, sorted_days


def discover_pc(
    matrix: np.ndarray,
    node_labels: list[str],
    alpha: float = 0.05,
    max_cond_vars: int = 3,
) -> DiscoveryResult:
    """Run PC algorithm for causal discovery.

    Args:
        matrix: N×T observation matrix.
        node_labels: Node ID for each row.
        alpha: Significance level for conditional independence tests.
        max_cond_vars: Maximum conditioning set size.

    Returns:
        DiscoveryResult with candidate edges.
    """
    result = DiscoveryResult(method="pc", nodes_used=node_labels)

    n_vars, n_obs = matrix.shape
    if n_vars < 3:
        result.warnings.append("PC requires at least 3 variables")
        return result

    if n_obs < 5:
        result.warnings.append(f"PC requires at least 5 time points, got {n_obs}")
        return result

    try:
        from causal_learn.search.ConstraintBased.PC import pc

        # Remove rows with all NaN
        valid_mask = ~np.all(np.isnan(matrix), axis=1)
        valid_matrix = matrix[valid_mask]
        valid_labels = [node_labels[i] for i in range(len(node_labels)) if valid_mask[i]]

        if valid_matrix.shape[0] < 3:
            result.warnings.append("Too few complete variables after NaN removal")
            return result

        # Interpolate remaining NaNs
        from scipy.interpolate import interp1d

        for i in range(valid_matrix.shape[0]):
            row = valid_matrix[i]
            nan_mask = np.isnan(row)
            if np.all(nan_mask):
                continue
            if np.any(nan_mask):
                valid_idx = np.where(~nan_mask)[0]
                if len(valid_idx) >= 2:
                    f = interp1d(valid_idx, row[valid_idx], kind="linear", fill_value="extrapolate")
                    row[nan_mask] = f(np.where(nan_mask)[0])
                elif len(valid_idx) == 1:
                    row[nan_mask] = row[valid_idx[0]]

        # Run PC
        cg = pc(valid_matrix.T, alpha=alpha, indep_test="fisherz", stable=True)

        # Extract edges from CG (causal graph)
        # cg.G is a GeneralGraph object
        edges = []
        graph = cg.G

        # Get adjacency matrix
        adj = np.array(graph.graph)
        n = adj.shape[0]

        for i in range(n):
            for j in range(i + 1, n):
                if adj[i, j] != 0 or adj[j, i] != 0:
                    # Edge exists between i and j
                    from_idx = i
                    to_idx = j

                    # Determine direction from edge marks
                    # -1 = tail (undirected), -2 = arrow (directed)
                    edge_type = "SUGGESTED_CAUSES"
                    confidence = 0.5  # Default confidence for PC-discovered edges

                    # Check for directed edge (i -> j)
                    if adj[j, i] == -1 and adj[i, j] == -1:
                        # Undirected edge (i - j)
                        edge_type = "SUGGESTED_CORRELATES_WITH"
                    elif adj[j, i] == -2:
                        # Directed edge (i -> j)
                        edge_type = "SUGGESTED_CAUSES"
                    elif adj[i, j] == -2:
                        # Directed edge (j -> i)
                        from_idx = j
                        to_idx = i
                        edge_type = "SUGGESTED_CAUSES"

                    # Compute correlation as confidence proxy
                    corr = np.corrcoef(valid_matrix[from_idx], valid_matrix[to_idx])[0, 1]
                    if not np.isnan(corr):
                        confidence = min(abs(corr), 0.95)

                    candidate = DiscoveryCandidate(
                        from_node=valid_labels[from_idx],
                        to_node=valid_labels[to_idx],
                        method="pc",
                        confidence=round(confidence, 3),
                        p_value=alpha,  # PC uses this as threshold
                        edge_type=edge_type,
                        metadata={
                            "correlation": round(float(corr), 3) if not np.isnan(corr) else None,
                            "n_obs": n_obs,
                            "n_vars": len(valid_labels),
                        },
                    )
                    edges.append(candidate)

        result.candidates = edges
        result.observation_count = n_obs
        result.warnings.append(
            f"PC discovered {len(edges)} candidate edges from {n_obs} time points across {len(valid_labels)} variables"
        )

    except ImportError:
        result.warnings.append("causal-learn not installed — falling back to correlation")
        return discover_correlation(matrix, node_labels)
    except Exception as e:
        logger.warning(f"PC algorithm failed: {e}")
        result.warnings.append(f"PC failed: {str(e)}")
        return discover_correlation(matrix, node_labels)

    return result


def discover_ges(
    matrix: np.ndarray,
    node_labels: list[str],
    max_parents: int = 5,
) -> DiscoveryResult:
    """Run GES (Greedy Equivalence Search) for causal discovery.

    Args:
        matrix: N×T observation matrix.
        node_labels: Node ID for each row.
        max_parents: Maximum parent set size.

    Returns:
        DiscoveryResult with candidate edges.
    """
    result = DiscoveryResult(method="ges", nodes_used=node_labels)

    n_vars, n_obs = matrix.shape
    if n_vars < 3:
        result.warnings.append("GES requires at least 3 variables")
        return result

    if n_obs < 5:
        result.warnings.append(f"GES requires at least 5 time points, got {n_obs}")
        return result

    try:
        from causal_learn.search.ScoreBased.GES import ges

        # Interpolate NaNs
        valid_mask = ~np.all(np.isnan(matrix), axis=1)
        valid_matrix = matrix[valid_mask]
        valid_labels = [node_labels[i] for i in range(len(node_labels)) if valid_mask[i]]

        if valid_matrix.shape[0] < 3:
            result.warnings.append("Too few complete variables after NaN removal")
            return result

        for i in range(valid_matrix.shape[0]):
            row = valid_matrix[i]
            nan_mask = np.isnan(row)
            if np.any(nan_mask):
                valid_idx = np.where(~nan_mask)[0]
                if len(valid_idx) >= 2:
                    from scipy.interpolate import interp1d

                    f = interp1d(valid_idx, row[valid_idx], kind="linear", fill_value="extrapolate")
                    row[nan_mask] = f(np.where(nan_mask)[0])
                elif len(valid_idx) == 1:
                    row[nan_mask] = row[valid_idx[0]]

        # Run GES
        record = ges(valid_matrix.T, maxP=max_parents)

        # Extract edges
        graph = record["G"]
        adj = np.array(graph.graph)
        n = adj.shape[0]
        edges = []

        for i in range(n):
            for j in range(i + 1, n):
                if adj[i, j] != 0 or adj[j, i] != 0:
                    from_idx = i
                    to_idx = j
                    edge_type = "SUGGESTED_CAUSES"

                    if adj[j, i] == -1 and adj[i, j] == -1:
                        edge_type = "SUGGESTED_CORRELATES_WITH"
                    elif adj[i, j] == -2:
                        from_idx = j
                        to_idx = i

                    corr = np.corrcoef(valid_matrix[from_idx], valid_matrix[to_idx])[0, 1]
                    confidence = min(abs(corr), 0.95) if not np.isnan(corr) else 0.5

                    candidate = DiscoveryCandidate(
                        from_node=valid_labels[from_idx],
                        to_node=valid_labels[to_idx],
                        method="ges",
                        confidence=round(confidence, 3),
                        edge_type=edge_type,
                        metadata={
                            "correlation": round(float(corr), 3) if not np.isnan(corr) else None,
                            "n_obs": n_obs,
                            "n_vars": len(valid_labels),
                        },
                    )
                    edges.append(candidate)

        result.candidates = edges
        result.observation_count = n_obs

    except ImportError:
        result.warnings.append("causal-learn not installed — falling back to correlation")
        return discover_correlation(matrix, node_labels)
    except Exception as e:
        logger.warning(f"GES failed: {e}")
        result.warnings.append(f"GES failed: {str(e)}")
        return discover_correlation(matrix, node_labels)

    return result


def discover_correlation(
    matrix: np.ndarray,
    node_labels: list[str],
    min_correlation: float = 0.5,
    min_obs: int = 3,
) -> DiscoveryResult:
    """Simple correlation-based discovery (fallback method).

    Discovers correlated pairs as candidate SUGGESTED_CORRELATES_WITH edges.

    Args:
        matrix: N×T observation matrix.
        node_labels: Node ID for each row.
        min_correlation: Minimum absolute correlation to consider.
        min_obs: Minimum number of overlapping observations.

    Returns:
        DiscoveryResult with candidate edges.
    """
    result = DiscoveryResult(method="correlation", nodes_used=node_labels)

    n_vars, n_obs = matrix.shape
    if n_vars < 2:
        result.warnings.append("Need at least 2 variables for correlation")
        return result

    # Interpolate NaNs for correlation computation
    valid_mask = ~np.all(np.isnan(matrix), axis=1)
    valid_matrix = matrix[valid_mask]
    valid_labels = [node_labels[i] for i in range(len(node_labels)) if valid_mask[i]]

    if valid_matrix.shape[0] < 2:
        result.warnings.append("Too few complete variables after NaN removal")
        return result

    # Fill remaining NaNs with column means
    for i in range(valid_matrix.shape[0]):
        col = valid_matrix[i]
        nan_mask = np.isnan(col)
        if np.any(nan_mask) and not np.all(nan_mask):
            col[nan_mask] = np.nanmean(col)

    # Compute correlation matrix
    n = len(valid_labels)
    edges = []

    for i in range(n):
        for j in range(i + 1, n):
            # Count overlapping non-NaN observations
            mask = ~np.isnan(valid_matrix[i]) & ~np.isnan(valid_matrix[j])
            overlap = mask.sum()

            if overlap < min_obs:
                continue

            x = valid_matrix[i][mask]
            y = valid_matrix[j][mask]

            if len(x) < min_obs:
                continue

            corr = np.corrcoef(x, y)[0, 1]

            if np.isnan(corr):
                continue

            if abs(corr) >= min_correlation:
                # Determine direction: node with higher variance → node with lower
                # (higher variance often drives the lower)
                var_i = np.var(x) if len(x) > 1 else 0
                var_j = np.var(y) if len(y) > 1 else 0

                if var_i >= var_j:
                    from_idx, to_idx = i, j
                else:
                    from_idx, to_idx = j, i

                edge_type = "SUGGESTED_CAUSES" if abs(corr) >= 0.7 else "SUGGESTED_CORRELATES_WITH"

                candidate = DiscoveryCandidate(
                    from_node=valid_labels[from_idx],
                    to_node=valid_labels[to_idx],
                    method="correlation",
                    confidence=round(min(abs(corr), 0.95), 3),
                    p_value=None,
                    edge_type=edge_type,
                    metadata={
                        "correlation": round(float(corr), 3),
                        "overlapping_obs": int(overlap),
                        "var_from": round(float(var_i if from_idx == i else var_j), 4),
                        "var_to": round(float(var_j if from_idx == i else var_i), 4),
                    },
                )
                edges.append(candidate)

    result.candidates = edges
    result.observation_count = n_obs

    return result


def run_discovery(
    observations: list[dict[str, Any]],
    node_ids: list[str],
    method: str = "auto",
    alpha: float = 0.05,
    min_observations: int = 3,
) -> DiscoveryResult:
    """Run causal discovery on observation data.

    Args:
        observations: List of observation dicts from OHM.
        node_ids: Node IDs to include in discovery.
        method: Discovery method ("pc", "ges", "correlation", "auto").
        alpha: Significance level for PC algorithm.
        min_observations: Minimum observations per node to include.

    Returns:
        DiscoveryResult with candidate edges.
    """
    # Filter to requested nodes
    filtered_obs = [o for o in observations if o.get("node_id") in node_ids]

    # Count observations per node
    from collections import Counter

    node_counts = Counter(o.get("node_id") for o in filtered_obs)
    eligible_nodes = [nid for nid in node_ids if node_counts.get(nid, 0) >= min_observations]

    if len(eligible_nodes) < 2:
        return DiscoveryResult(
            method=method,
            warnings=[
                f"Only {len(eligible_nodes)} nodes with >= {min_observations} observations (need >= 2)"
            ],
        )

    # Build observation matrix
    matrix, labels, time_labels = build_observation_matrix(filtered_obs, eligible_nodes)

    if matrix.size == 0 or matrix.shape[1] < 3:
        # Fall back to correlation even if requested method was PC/GES
        result = discover_correlation(matrix if matrix.size > 0 else np.zeros((2, 1)), labels)
        result.warnings.append(
            f"Insufficient time points ({matrix.shape[1] if matrix.size > 0 else 0}) for {method}, falling back to correlation"
        )
        return result

    # Auto-select method based on data characteristics
    if method == "auto":
        n_vars = matrix.shape[0]
        n_time = matrix.shape[1]

        if n_vars >= 3 and n_time >= 5:
            method = "pc"
        elif n_vars >= 3 and n_time >= 3:
            method = "ges"
        else:
            method = "correlation"

    if method == "pc":
        return discover_pc(matrix, labels, alpha=alpha)
    elif method == "ges":
        return discover_ges(matrix, labels)
    else:
        return discover_correlation(matrix, labels)