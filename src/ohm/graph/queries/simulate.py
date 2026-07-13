"""Monte Carlo prospect simulation (OHM-843).

Simulates prospect outcomes by sampling from Beta-PERT distributions
per expectation node, computing per-expectation sensitivity rankings,
and cross-validating against compute_voi's VoI ranking via Spearman
rank correlation.

This is distinct from:
  - ohm_pert (single-node three-point estimate)
  - ohm_monte_carlo / monte_carlo_cascade (graph cascade/failure propagation)
  - ohm_simulate (this module — multi-expectation prospect-outcome aggregation)
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _rows_to_dicts


def _beta_pert_sample(
    p10: float,
    p50: float,
    p90: float,
    rng: random.Random,
) -> float:
    """Sample from a modified Beta-PERT distribution.

    Uses the PERT mean formula from ``inference/pert.py``::

        μ = (p10 + 4·p50 + p90) / 6

    Then derives Beta shape parameters::

        α = 1 + 4·(μ − p10) / (p90 − p10)
        β = 1 + 4·(p90 − μ) / (p90 − p10)

    Samples from ``Beta(α, β)`` on ``[0, 1]`` and scales to ``[p10, p90]``.

    If p10 == p90 (degenerate), returns p50.
    """
    lo, hi = p10, p90
    if hi <= lo:
        return p50
    mean = (lo + 4.0 * p50 + hi) / 6.0
    lam = (mean - lo) / (hi - lo)
    lam = max(0.001, min(0.999, lam))
    alpha = 1.0 + 4.0 * lam
    beta = 1.0 + 4.0 * (1.0 - lam)
    u = rng.betavariate(alpha, beta)
    return lo + (hi - lo) * u


def _rank(values: list[float]) -> list[float]:
    """Return ranks (1-based, average for ties) of *values*."""
    n = len(values)
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman_rank_correlation(x: list[float], y: list[float]) -> float:
    """Compute Spearman rank correlation coefficient.

    Returns 0.0 when either list has zero variance or length < 2.
    """
    n = len(x)
    if n < 2 or n != len(y):
        return 0.0
    rx = _rank(x)
    ry = _rank(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den_x = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    den_y = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def simulate_prospect(
    conn: "DuckDBPyConnection",
    *,
    prospect_id: str,
    n_iterations: int = 5000,
    seed: int | None = None,
) -> dict[str, Any]:
    """Run Monte Carlo simulation over a prospect's expectations (OHM-843).

    For each ``expectation`` node linked to the prospect via a ``CONTAINS``
    edge, samples from a modified Beta-PERT distribution parameterised by the
    expectation's ``p10`` / ``p50`` / ``p90`` metadata fields.

    Computes per-expectation distribution statistics (mean, std, p5, p50, p95),
    a sensitivity ranking (Spearman correlation between each expectation's
    samples and the normalised aggregate), and cross-validates the sensitivity
    ranking against ``compute_voi``'s ranking via Spearman rank correlation.

    The result is persisted as an ``experiment_result`` observation on the
    prospect node.

    Time-stepped degradation is deferred to v2.

    Args:
        conn: Database connection.
        prospect_id: The prospect node to simulate.
        n_iterations: Number of Monte Carlo iterations (default 5000).
        seed: Random seed for reproducibility.

    Returns:
        Dict with per-expectation results, aggregate statistics,
        sensitivity rankings, and VoI cross-validation.

    Raises:
        ValueError: If prospect not found, is not a prospect, or has no
            expectations.
    """
    from ohm.validation import validate_identifier

    prospect_id = validate_identifier(prospect_id, name="prospect_id")

    rows = _rows_to_dicts(conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [prospect_id],
    ))
    if not rows:
        raise ValueError(f"Prospect {prospect_id!r} not found")
    prospect = rows[0]
    if prospect.get("type") != "prospect":
        raise ValueError(f"Node {prospect_id!r} is type {prospect.get('type')!r}, not 'prospect'")

    expectations = _rows_to_dicts(conn.execute("""
        SELECT child.*, e.edge_type AS link_type
        FROM ohm_edges e
        JOIN ohm_nodes child ON child.id = e.to_node
            AND child.type = 'expectation'
            AND child.deleted_at IS NULL
        WHERE e.from_node = ? AND e.edge_type = 'CONTAINS' AND e.deleted_at IS NULL
        ORDER BY child.created_at
    """, [prospect_id]))

    if not expectations:
        raise ValueError(f"Prospect {prospect_id!r} has no expectation children")

    rng = random.Random(seed)

    parsed: list[dict[str, Any]] = []
    for exp in expectations:
        meta = exp.get("metadata") or {}
        if isinstance(meta, str):
            import json
            meta = json.loads(meta)
        p10 = float(meta.get("p10", meta.get("p05", 0.0)))
        p50 = float(meta.get("p50", meta.get("expected_value", 0.0)))
        p90 = float(meta.get("p90", meta.get("p95", 0.0)))
        unit = meta.get("unit", "")
        expected_value = meta.get("expected_value")
        if expected_value is None:
            expected_value = p50

        samples = [_beta_pert_sample(p10, p50, p90, rng) for _ in range(n_iterations)]

        samples_sorted = sorted(samples)
        n = len(samples_sorted)

        def pct(p: float) -> float:
            idx = int(n * p)
            if idx >= n:
                idx = n - 1
            return samples_sorted[idx]

        mean = sum(samples) / n
        variance = sum((s - mean) ** 2 for s in samples) / n
        std = math.sqrt(variance)

        parsed.append({
            "node_id": exp["id"],
            "label": exp.get("label", ""),
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "unit": unit,
            "expected_value": expected_value,
            "samples": samples,
            "mean": round(mean, 6),
            "std": round(std, 6),
            "p5": round(pct(0.05), 6),
            "p50_sim": round(pct(0.50), 6),
            "p95": round(pct(0.95), 6),
        })

    if len(parsed) == 1:
        e = parsed[0]
        e["sensitivity_score"] = 1.0
    else:
        lo_vals = [e["p10"] for e in parsed]
        hi_vals = [e["p90"] for e in parsed]
        ranges = [(hi_vals[i] - lo_vals[i]) or 1.0 for i in range(len(parsed))]
        norm_samples: list[list[float]] = []
        for i, e in enumerate(parsed):
            norm = [(s - lo_vals[i]) / ranges[i] for s in e["samples"]]
            norm_samples.append(norm)

        aggregate = []
        for j in range(n_iterations):
            aggregate.append(sum(norm_samples[i][j] for i in range(len(parsed))) / len(parsed))

        for i, e in enumerate(parsed):
            corr = _spearman_rank_correlation(e["samples"], aggregate)
            e["sensitivity_score"] = round(corr, 6)

    sensitivity_ranking = sorted(parsed, key=lambda e: e["sensitivity_score"], reverse=True)

    voi_correlation: float | None = None
    try:
        voi_result = _compute_voi_for_prospect(conn, prospect_id, parsed)
        if voi_result is not None:
            voi_correlation = voi_result
    except Exception:
        pass

    all_means = [e["mean"] for e in parsed]
    all_stds = [e["std"] for e in parsed]
    aggregate_mean = sum(all_means) / len(all_means) if all_means else 0.0
    aggregate_std = math.sqrt(sum(s * s for s in all_stds) / len(all_stds)) if all_stds else 0.0

    for e in parsed:
        del e["samples"]

    result = {
        "prospect_id": prospect_id,
        "prospect_label": prospect.get("label", ""),
        "n_iterations": n_iterations,
        "seed": seed,
        "expectations": sensitivity_ranking,
        "aggregate": {
            "mean": round(aggregate_mean, 6),
            "std": round(aggregate_std, 6),
        },
        "voi_cross_validation": {
            "spearman_correlation": round(voi_correlation, 6) if voi_correlation is not None else None,
            "threshold": 0.5,
            "passed": voi_correlation is not None and voi_correlation > 0.5,
        },
        "note": "Time-stepped degradation deferred to v2.",
    }

    from ohm.graph.queries import create_observation

    create_observation(
        conn,
        node_id=prospect_id,
        obs_type="experiment_result",
        created_by="ohm_simulate",
        value=aggregate_mean,
        notes=f"Monte Carlo simulation: {n_iterations} iterations, {len(parsed)} expectations",
        metadata={
            "n_iterations": n_iterations,
            "seed": seed,
            "expectation_count": len(parsed),
            "aggregate_mean": round(aggregate_mean, 6),
            "aggregate_std": round(aggregate_std, 6),
            "voi_correlation": round(voi_correlation, 6) if voi_correlation is not None else None,
        },
    )

    return result


def _compute_voi_for_prospect(
    conn: "DuckDBPyConnection",
    prospect_id: str,
    parsed: list[dict[str, Any]],
) -> float | None:
    """Cross-validate sensitivity ranking against compute_voi.

    Tries to compute VoI using the expectation target nodes (linked via
    EXPECTS edges) as decision nodes. If that yields rankings, computes
    Spearman rank correlation between our sensitivity scores and VoI scores.

    Returns None if VoI can't be computed (e.g. no target nodes, no
    causal edges).
    """
    from ohm.inference.bayesian import compute_voi

    target_ids = []
    for e in parsed:
        targets = _rows_to_dicts(conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'EXPECTS' AND deleted_at IS NULL",
            [e["node_id"]],
        ))
        for t in targets:
            tid = t.get("to_node") or t.get("to")
            if tid:
                target_ids.append(tid)

    if not target_ids:
        return None

    try:
        voi = compute_voi(conn, decision_nodes=target_ids)
    except Exception:
        return None

    rankings = voi.get("rankings", [])
    if not rankings:
        return None

    voi_by_node: dict[str, float] = {}
    for r in rankings:
        nid = r.get("node_id")
        if nid:
            voi_by_node[nid] = r.get("voi_score", 0.0)

    target_to_expectation: dict[str, str] = {}
    for e in parsed:
        targets = _rows_to_dicts(conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'EXPECTS' AND deleted_at IS NULL",
            [e["node_id"]],
        ))
        for t in targets:
            tid = t.get("to_node") or t.get("to")
            if tid:
                target_to_expectation[tid] = e["node_id"]

    matched_voi: list[float] = []
    matched_sens: list[float] = []
    for e in parsed:
        exp_id = e["node_id"]
        for tid, eid in target_to_expectation.items():
            if eid == exp_id and tid in voi_by_node:
                matched_voi.append(voi_by_node[tid])
                matched_sens.append(e["sensitivity_score"])
                break

    if len(matched_voi) < 2:
        return None

    return _spearman_rank_correlation(matched_sens, matched_voi)