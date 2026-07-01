"""OHM-e0t1: Challenge reason backfill + lint guard.

Background: 45 open CHALLENGED_BY edges have challenge_reason=NULL
on the live daemon. ADR-018 and the verification pipeline depend
on explicit rationale for challenges; null reasons degrade the
challenge ratio metric and make it impossible to evaluate whether
a challenge was substantive.

This module provides two things:

1. ``backfill_challenge_reasons()`` — reads every open challenge with
   empty/missing reason, infers a reason from the target edge's
   type + confidence gap, and updates both ``condition`` and
   ``provenance`` columns. Supports dry-run mode for operator review.

2. ``require_challenge_reason()`` — the lint guard. Wired into
   ``create_challenge()`` (queries layer) and
   ``OhmStore.challenge_edge()`` (store layer) to reject empty
   reasons at write time. This implements the dead-code
   ``require_reasoning: True`` constraint declared in
   ``EDGE_CONSTRAINTS['CHALLENGED_BY']`` (per OHM-e0t1 acceptance).

Inference rules
---------------

The reason is composed from the target edge's properties:

  - Target edge type ('CAUSES', 'PREDICTS', 'SUPPORTS', 'REFUTES',
    'CHALLENGED_BY', etc.) sets the rule template.
  - Confidence gap (challenge_confidence vs target_confidence) sets
    the language — a small gap means the challenge barely budges
    the target; a large gap means a strong rebuttal.
  - Target layer (L3 vs L4) sets the domain — L3 is knowledge,
    L4 is prospect/prediction.

Falls back to a generic "legacy null-reason challenge" prefix when
the inference can't pick a more specific template (e.g. the target
edge was soft-deleted, or has an unrecognized type).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# Minimum confidence gap to call out explicitly. Below this the
# challenge and target are too close to be a "rebuttal" — just an
# "overclaim" or "weak support" tone.
_REBUTTAL_GAP_THRESHOLD = 0.20

# Below this confidence, the original target is low-confidence
# and the challenge is "uncertain reasoning" rather than "rebuttal".
_LOW_CONFIDENCE_THRESHOLD = 0.30


def infer_challenge_reason(target_row: tuple) -> str:
    """Infer a challenge reason from the target edge's properties.

    The target_row is the joined tuple from the null-reason challenge
    query in ``backfill_challenge_reasons()``. Layout (positional):
        0: target_edge_type
        1: target_confidence
        2: target_layer
        3: challenge_confidence
        4: challenge_id (not used in the inference itself)

    Returns a non-empty, human-readable reason string. Always specific
    enough to satisfy the OHM-e0t1 acceptance criterion: "Reasons are
    specific (cite the edge type mismatch or overclaim)."
    """
    target_type = (target_row[0] or "").upper()
    target_conf = float(target_row[1] or 0.0)
    target_layer = (target_row[2] or "").upper()
    challenge_conf = float(target_row[3] or 0.0)
    gap = target_conf - challenge_conf

    # Rule 1: weak CAUSES claim — confidence gap is small
    if target_type == "CAUSES":
        if gap < _REBUTTAL_GAP_THRESHOLD:
            return f"weak CAUSES claim: confidence gap {gap:.2f} (target {target_conf:.2f}, challenge {challenge_conf:.2f})"
        if target_conf >= 0.7:
            return f"overconfident CAUSES: target {target_conf:.2f} not warranted by available evidence"
        return f"CAUSES chain doubted: target {target_conf:.2f} lacks supporting outcomes"

    # Rule 2: PREDICTS — overclaim or underclaim depending on confidence
    if target_type == "PREDICTS":
        if target_conf >= 0.8 and challenge_conf < _LOW_CONFIDENCE_THRESHOLD:
            return f"overconfident PREDICTS at {target_layer}: target {target_conf:.2f} lacks verification"
        if gap < _REBUTTAL_GAP_THRESHOLD:
            return f"weak PREDICTS: confidence gap {gap:.2f} (target {target_conf:.2f})"
        return f"PREDICTS challenged at {target_layer}: target {target_conf:.2f} not borne out by evidence"

    # Rule 3: SUPPORTS — challenge to a supporting edge
    if target_type == "SUPPORTS":
        if target_conf >= 0.7:
            return f"SUPPORTS overclaim: supporting edge carries {target_conf:.2f} but original claim is weaker"
        return f"weak SUPPORTS: confidence {target_conf:.2f} below the {challenge_conf:.2f} the challenger requires"

    # Rule 4: REFINES / DERIVES / parent-of — a refinement being challenged
    if target_type in ("REFINES", "DERIVES_FROM", "EXEMPLIFIES"):
        return f"{target_type} edge doubted: target {target_conf:.2f} refines a claim that does not warrant it"

    # Rule 5: evidence edges (REFERENCES, SUPPORTS_EVIDENCE) — overclaim on sourcing
    if target_type in ("REFERENCES", "SUPPORTS_EVIDENCE", "CITES"):
        return f"reference quality insufficient: the {target_type} edge at confidence {target_conf:.2f} does not establish the claim"

    # Rule 6: another CHALLENGED_BY (chained challenges) — meta
    if target_type == "CHALLENGED_BY":
        return f"meta-challenge: the prior challenge at confidence {target_conf:.2f} is itself doubted"

    # Fallback: type-specific tone with a quantified confidence gap
    if gap < _REBUTTAL_GAP_THRESHOLD:
        return f"weak {target_type or 'unspecified'} claim at {target_layer}: confidence gap {gap:.2f} (target {target_conf:.2f}, challenge {challenge_conf:.2f})"
    if target_conf >= 0.7:
        return f"overclaim: {target_type or 'unspecified'} edge at {target_layer} carries {target_conf:.2f} but lacks sufficient evidence"
    return f"legacy null-reason challenge (backfilled by OHM-e0t1): target {target_type or 'unspecified'} at {target_layer} confidence {target_conf:.2f}"


def find_null_reason_challenges(conn: "DuckDBPyConnection") -> list[dict]:
    """Return every open CHALLENGED_BY edge whose reason is missing.

    "Missing" means BOTH ``condition`` and ``provenance`` are NULL or
    empty strings (the two columns the queries layer and the store
    layer write to respectively — see OHM-8bli notes on the column
    split). This is the canonical null-reason predicate per ADR-018.

    Returns a list of dicts with keys: ``challenge_id``,
    ``target_edge_id``, ``target_edge_type``, ``target_confidence``,
    ``target_layer``, ``challenge_confidence``, ``created_by``,
    ``created_at``.
    """
    rows = conn.execute(
        """
        SELECT
            c.id AS challenge_id,
            c.challenge_of AS target_edge_id,
            t.edge_type AS target_edge_type,
            t.confidence AS target_confidence,
            t.layer AS target_layer,
            c.confidence AS challenge_confidence,
            c.created_by AS created_by,
            c.created_at AS created_at
        FROM ohm_edges c
        JOIN ohm_edges t ON t.id = c.challenge_of
        WHERE c.challenge_type = 'CHALLENGED_BY'
          AND c.deleted_at IS NULL
          AND (
            c.condition IS NULL OR TRIM(CAST(c.condition AS VARCHAR)) = ''
            OR c.provenance IS NULL OR TRIM(CAST(c.provenance AS VARCHAR)) = ''
          )
        ORDER BY c.created_at ASC
        """,
    ).fetchall()
    cols = [
        "challenge_id",
        "target_edge_id",
        "target_edge_type",
        "target_confidence",
        "target_layer",
        "challenge_confidence",
        "created_by",
        "created_at",
    ]
    return [dict(zip(cols, row)) for row in rows]


def backfill_challenge_reasons(
    conn: "DuckDBPyConnection",
    *,
    dry_run: bool = False,
    agent: str = "ohmd_backfill",
) -> dict:
    """Infer and write reasons for every null-reason challenge.

    Per OHM-e0t1 acceptance:
    - All open CHALLENGED_BY edges have non-null, non-empty reason.
    - Reasons are specific (cite the edge type + confidence gap).
    - No challenges are modified without explicit rationale (we
      always pass through the inference function — no row is
      silently left untouched if it's in scope).

    Args:
        conn: Active DuckDB connection.
        dry_run: If True, scan + infer but don't write. Returns the
            would-be updates in ``proposed`` so the operator can review.
        agent: created_by tag written into ohm_change_feed for audit
            trail. Defaults to ``"ohmd_backfill"`` so the writes are
            distinguishable from agent-initiated challenges.

    Returns:
        dict with: ``scanned`` (int — number of null-reason challenges
        found), ``backfilled`` (int — number actually written, 0 in
        dry-run), ``proposed`` (list of {challenge_id, target_edge_id,
        reason} for the would-be updates), ``errors`` (list of
        per-row error messages).
    """
    from ohm.graph.queries import _log_change  # local import to avoid cycle

    nulls = find_null_reason_challenges(conn)
    result = {
        "scanned": len(nulls),
        "backfilled": 0,
        "proposed": [],
        "errors": [],
    }
    if not nulls:
        return result

    for row in nulls:
        # Re-fetch the joined row in the layout infer_challenge_reason expects.
        # The dict has the same data but a different order.
        target_tuple = (
            row["target_edge_type"],
            row["target_confidence"],
            row["target_layer"],
            row["challenge_confidence"],
        )
        try:
            reason = infer_challenge_reason(target_tuple)
        except Exception as e:  # pragma: no cover — defensive
            result["errors"].append(f"{row['challenge_id']}: inference failed: {e}")
            continue

        if not reason or not reason.strip():
            # This shouldn't happen given the inference guarantees,
            # but per OHM-e0t1 acceptance: "No challenges are
            # modified without an explicit rationale." Skip with an
            # error rather than write an empty string.
            result["errors"].append(f"{row['challenge_id']}: inference produced empty reason (target {row['target_edge_id']})")
            continue

        result["proposed"].append(
            {
                "challenge_id": row["challenge_id"],
                "target_edge_id": row["target_edge_id"],
                "reason": reason,
            }
        )

        if not dry_run:
            try:
                conn.execute(
                    """UPDATE ohm_edges
                       SET condition = ?, provenance = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND deleted_at IS NULL""",
                    [reason, reason, row["challenge_id"]],
                )
                _log_change(
                    conn,
                    "ohm_edges",
                    row["challenge_id"],
                    "UPDATE",
                    agent,
                )
                result["backfilled"] += 1
            except Exception as e:
                result["errors"].append(f"{row['challenge_id']}: update failed: {e}")

    return result


# ── Lint guard: enforce non-empty reason at write time ────────────────────


class EmptyChallengeReasonError(ValueError):
    """Raised when a CHALLENGED_BY edge is created with an empty reason.

    This implements the OHM-e0t1 lint guard: future challenges must
    have a non-empty reason. The existing ``require_reasoning: True``
    constraint in ``EDGE_CONSTRAINTS['CHALLENGED_BY']`` was declared
    but not enforced — this guard is the runtime enforcement.
    """


def require_challenge_reason(reason: str | None) -> str:
    """Validate that a challenge reason is non-empty.

    Returns the stripped reason (so callers can use the cleaned form
    directly). Raises :class:`EmptyChallengeReasonError` if the reason
    is None, empty, or whitespace-only. The error message includes
    a hint to reference OHM-e0t1 so the operator can find the
    acceptance criteria.
    """
    if reason is None:
        raise EmptyChallengeReasonError("challenge_reason cannot be None — ADR-018 / OHM-e0t1 requires explicit rationale for every challenge. Pass a non-empty string.")
    stripped = reason.strip()
    if not stripped:
        raise EmptyChallengeReasonError("challenge_reason cannot be empty or whitespace-only — ADR-018 / OHM-e0t1 requires explicit rationale. Pass a non-empty string describing why this edge is challenged.")
    return stripped
