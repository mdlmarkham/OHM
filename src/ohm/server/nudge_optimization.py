"""Nudge-message optimization autoresearch loop (OHM-847).

A/B-tests nudge message wording, measures which variants cause the
desired agent behavior (via ``accept_nudge()`` self-report), and
promotes winners.

Uses the existing ``ohm_nudge_log`` table (extended with ``variant_id``)
and ``accept_nudge()``/``nudge_acceptance_stats()`` infrastructure.

v1 scope:
  - Conversion signal: ``accept_nudge()`` explicit self-report
  - Statistical test: Fisher's exact test at p < 0.05
  - Minimum 30 exposures per variant before evaluation
  - guard_action_count: dropped (no implementable signal exists)
  - Variant selection: best-effort, non-blocking (``try/except: pass``)
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _rows_to_dicts

MIN_EXPOSURES_PER_VARIANT = 30


def _fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Compute Fisher's exact test p-value (two-sided).

    Args:
        a: successes in variant A
        b: successes in variant B
        c: failures in variant A
        d: failures in variant B

    Returns:
        Two-sided p-value.
    """
    n = a + b + c + d
    if n == 0:
        return 1.0

    def log_factorial(k: int) -> float:
        return sum(math.log(i) for i in range(1, k + 1)) if k > 0 else 0.0

    def log_choose(n: int, k: int) -> float:
        return log_factorial(n) - log_factorial(k) - log_factorial(n - k)

    row1_total = a + c
    row2_total = b + d
    col1_total = a + b
    col2_total = c + d

    log_p_observed = (
        log_choose(row1_total, a)
        + log_choose(row2_total, b)
        - log_choose(n, col1_total)
    )

    min_a = max(0, col1_total - row2_total)
    max_a = min(row1_total, col1_total)

    p_two_sided = 0.0
    for i in range(min_a, max_a + 1):
        j = col1_total - i
        if j < 0:
            continue
        log_p_i = (
            log_choose(row1_total, i)
            + log_choose(row2_total, j)
            - log_choose(n, col1_total)
        )
        if log_p_i <= log_p_observed + 1e-12:
            p_two_sided += math.exp(log_p_i)

    return min(1.0, max(0.0, p_two_sided))


def select_variant(
    conn: "DuckDBPyConnection",
    *,
    nudge_type: str,
    variants: list[str],
) -> str:
    """Select a variant for a nudge type (OHM-847).

    Uses a simple traffic split: if there are fewer than MIN_EXPOSURES
    per variant, assigns the least-exposed variant. Otherwise assigns
    randomly (round-robin would require persistent state).

    Args:
        conn: Database connection.
        nudge_type: The nudge type to select a variant for.
        variants: List of variant ids (e.g. ["A", "B"]).

    Returns:
        The selected variant id.
    """
    import random

    if not variants:
        return "default"
    if len(variants) == 1:
        return variants[0]

    counts: dict[str, int] = {}
    for v in variants:
        row = conn.execute(
            "SELECT COUNT(*) FROM ohm_nudge_log WHERE nudge_type = ? AND variant_id = ?",
            [nudge_type, v],
        ).fetchone()
        counts[v] = row[0] if row else 0

    min_count = min(counts.values())
    if min_count < MIN_EXPOSURES_PER_VARIANT:
        least_exposed = [v for v, c in counts.items() if c == min_count]
        return random.choice(least_exposed)

    return random.choice(variants)


def record_exposure(
    conn: "DuckDBPyConnection",
    *,
    nudge_id: str,
    nudge_type: str,
    variant_id: str,
    agent: str,
    message: str,
    target_id: str | None = None,
    severity: str = "info",
) -> None:
    """Record a nudge exposure with variant_id (OHM-847).

    The nudge is already logged to ``ohm_nudge_log`` by ``enrich_response()``.
    This function updates the existing row with the variant_id, or can
    be called to insert a new exposure row.

    Args:
        conn: Database connection.
        nudge_id: The nudge log row id.
        nudge_type: The nudge type.
        variant_id: The A/B variant id.
        agent: Agent receiving the nudge.
        message: The nudge message text.
        target_id: Optional target node id.
        severity: Nudge severity.
    """
    try:
        conn.execute(
            """INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, target_id, message, variant_id)
               VALUES (?, ?, 'node', ?, ?, ?, ?, ?)""",
            [nudge_id, agent, nudge_type, severity, target_id, message, variant_id],
        )
    except Exception:
        try:
            conn.execute(
                "UPDATE ohm_nudge_log SET variant_id = ? WHERE id = ?",
                [variant_id, nudge_id],
            )
        except Exception:
            pass


def evaluate_nudge_variants(
    conn: "DuckDBPyConnection",
    *,
    nudge_type: str,
    significance_threshold: float = 0.05,
    min_exposures: int = MIN_EXPOSURES_PER_VARIANT,
) -> dict[str, Any]:
    """Evaluate A/B test results for a nudge type (OHM-847).

    Uses Fisher's exact test at the given significance threshold.
    Refuses to declare a winner below ``min_exposures`` per variant.

    Args:
        conn: Database connection.
        nudge_type: The nudge type to evaluate.
        significance_threshold: p-value threshold for significance.
        min_exposures: Minimum exposures per variant (default 30).

    Returns:
        Dict with variant stats, winner (or None), p_value, and
        insufficient_data flag.
    """
    rows = _rows_to_dicts(conn.execute(
        """SELECT variant_id,
                  COUNT(*) AS total,
                  SUM(CASE WHEN accepted = true THEN 1 ELSE 0 END) AS accepted_count,
                  SUM(CASE WHEN accepted = false THEN 1 ELSE 0 END) AS rejected_count
           FROM ohm_nudge_log
           WHERE nudge_type = ? AND variant_id IS NOT NULL
           GROUP BY variant_id
           ORDER BY variant_id""",
        [nudge_type],
    ))

    if not rows:
        return {
            "nudge_type": nudge_type,
            "variants": [],
            "winner": None,
            "p_value": None,
            "insufficient_data": True,
            "reason": "No variant data found",
        }

    variants = []
    for row in rows:
        total = row.get("total", 0) or 0
        accepted = row.get("accepted_count", 0) or 0
        rejected = row.get("rejected_count", 0) or 0
        responded = accepted + rejected
        rate = round(accepted / responded, 4) if responded > 0 else None
        variants.append({
            "variant_id": row.get("variant_id"),
            "total": total,
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": rate,
            "has_enough_data": total >= min_exposures,
        })

    if len(variants) < 2:
        return {
            "nudge_type": nudge_type,
            "variants": variants,
            "winner": None,
            "p_value": None,
            "insufficient_data": True,
            "reason": f"Only {len(variants)} variant(s), need at least 2",
        }

    all_have_enough = all(v["has_enough_data"] for v in variants)
    if not all_have_enough:
        return {
            "nudge_type": nudge_type,
            "variants": variants,
            "winner": None,
            "p_value": None,
            "insufficient_data": True,
            "reason": f"Some variants have fewer than {min_exposures} exposures",
        }

    v_a = variants[0]
    v_b = variants[1]
    a_success = v_a["accepted"]
    b_success = v_b["accepted"]
    a_fail = v_a["rejected"]
    b_fail = v_b["rejected"]

    if a_success + a_fail == 0 or b_success + b_fail == 0:
        return {
            "nudge_type": nudge_type,
            "variants": variants,
            "winner": None,
            "p_value": None,
            "insufficient_data": True,
            "reason": "No responses (accepted/rejected) for one or both variants",
        }

    p_value = _fisher_exact(a_success, b_success, a_fail, b_fail)

    winner = None
    if p_value < significance_threshold:
        rate_a = v_a["acceptance_rate"]
        rate_b = v_b["acceptance_rate"]
        if rate_a is not None and rate_b is not None:
            winner = v_a["variant_id"] if rate_a > rate_b else v_b["variant_id"]

    return {
        "nudge_type": nudge_type,
        "variants": variants,
        "winner": winner,
        "p_value": round(p_value, 6),
        "insufficient_data": False,
        "significance_threshold": significance_threshold,
        "min_exposures": min_exposures,
    }


def promote_nudge_variant(
    conn: "DuckDBPyConnection",
    *,
    nudge_type: str,
    variant_id: str,
) -> dict[str, Any]:
    """Promote a winning variant as the default for a nudge type (OHM-847).

    Records the promotion in ``ohm_meta`` so the server can look up the
    default variant on next nudge fire.

    Args:
        conn: Database connection.
        nudge_type: The nudge type to promote for.
        variant_id: The winning variant id.

    Returns:
        Dict with the promotion result.
    """
    import json

    meta_key = f"nudge_variant:{nudge_type}"
    conn.execute(
        "INSERT OR REPLACE INTO ohm_meta (key, value) VALUES (?, ?)",
        [meta_key, json.dumps({"default_variant": variant_id, "promoted_at": str(__import__("datetime").datetime.now())})],
    )

    return {
        "nudge_type": nudge_type,
        "variant_id": variant_id,
        "status": "promoted",
        "meta_key": meta_key,
    }


def demote_nudge_variant(
    conn: "DuckDBPyConnection",
    *,
    nudge_type: str,
) -> dict[str, Any]:
    """Demote/reset the default variant for a nudge type (OHM-847).

    Removes the promoted variant from ohm_meta, reverting to the
    default (non-variant) nudge message.

    Args:
        conn: Database connection.
        nudge_type: The nudge type to reset.

    Returns:
        Dict with the demotion result.
    """
    meta_key = f"nudge_variant:{nudge_type}"
    conn.execute(
        "DELETE FROM ohm_meta WHERE key = ?",
        [meta_key],
    )

    return {
        "nudge_type": nudge_type,
        "status": "demoted",
        "meta_key": meta_key,
    }


def get_default_variant(
    conn: "DuckDBPyConnection",
    *,
    nudge_type: str,
) -> str | None:
    """Get the current default variant for a nudge type (OHM-847).

    Args:
        conn: Database connection.
        nudge_type: The nudge type to look up.

    Returns:
        The default variant id, or None if no promotion has occurred.
    """
    import json

    row = conn.execute(
        "SELECT value FROM ohm_meta WHERE key = ?",
        [f"nudge_variant:{nudge_type}"],
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
        return data.get("default_variant")
    except (json.JSONDecodeError, TypeError):
        return None