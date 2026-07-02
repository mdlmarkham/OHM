"""Cross-graph corroboration: flag edges supported by N independent sources (OHM-m32a).

When two independent sources (different ``created_by`` agents) make the
same L3 claim (same ``to_node`` and same ``edge_type``), the edge is
corroborated. The ``corroboration_count`` column on ``ohm_edges``
records how many independent agents have made the same claim, and the
effective confidence gets a small bump for each corroborating source.

This is the missing AND-gate for "is this claim well-supported?" — the
agent no longer has to scan the graph; the corroboration count is a
single integer on each edge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

# The confidence bump per corroborating source. Diminishing returns:
# the 5th source adds less than the 2nd. Capped at MAX_CORROBORATION.
CORROBORATION_BUMP = 0.1
MAX_CORROBORATION = 5


def compute_edge_corroboration(conn: "DuckDBPyConnection") -> dict[str, Any]:
    """Recompute ``corroboration_count`` for all active L3 edges.

    For each edge, counts how many OTHER active edges share the same
    ``(to_node, edge_type, layer)`` but have a different ``created_by``
    agent. This is the number of independent sources corroborating the
    claim.

    Updates the ``corroboration_count`` column in place. Returns a
    summary dict with total_edges, corroborated_edges, max_count.

    Should be called periodically (e.g. after batch writes) or on
    demand via ``POST /admin/compute-corroboration``.
    """
    # Count corroborating edges per (to_node, edge_type, layer, created_by)
    # group, then attribute the count to each edge in the group.
    conn.execute(
        """
        UPDATE ohm_edges AS target
        SET corroboration_count = (
            SELECT COUNT(DISTINCT other.created_by)
            FROM ohm_edges AS other
            WHERE other.to_node = target.to_node
              AND other.edge_type = target.edge_type
              AND other.layer = target.layer
              AND other.deleted_at IS NULL
              AND other.created_by != target.created_by
              AND other.created_by IS NOT NULL
        )
        WHERE target.deleted_at IS NULL
          AND target.layer = 'L3'
        """
    )

    # Also reset to 0 for non-L3 edges (corroboration is only meaningful
    # for L3 knowledge claims).
    conn.execute(
        "UPDATE ohm_edges SET corroboration_count = 0 WHERE layer != 'L3' AND deleted_at IS NULL"
    )

    summary = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN corroboration_count > 0 THEN 1 ELSE 0 END) AS corroborated,
            MAX(corroboration_count) AS max_count
        FROM ohm_edges
        WHERE deleted_at IS NULL AND layer = 'L3'
        """
    ).fetchone()

    return {
        "total_edges": summary[0] if summary else 0,
        "corroborated_edges": summary[1] if summary else 0,
        "max_count": summary[2] if summary else 0,
    }


def get_edge_corroboration(
    conn: "DuckDBPyConnection",
    edge_id: str,
) -> dict[str, Any]:
    """Return corroboration detail for a single edge.

    Includes the edge's corroboration_count, its effective confidence
    (with the corroboration bump applied), and a list of the
    corroborating edges (from other agents making the same claim).
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")

    row = conn.execute(
        """SELECT id, from_node, to_node, edge_type, layer, confidence,
                  created_by, corroboration_count
           FROM ohm_edges
           WHERE id = ? AND deleted_at IS NULL""",
        [edge_id],
    ).fetchone()

    if not row:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Edge not found: {edge_id}")

    eid, from_node, to_node, edge_type, layer, confidence, created_by, count = row
    count = count or 0

    # Compute effective confidence with corroboration bump
    effective = confidence
    if layer == "L3" and count > 0:
        bump = CORROBORATION_BUMP * min(count, MAX_CORROBORATION)
        effective = min(1.0, confidence * (1.0 + bump))

    # Fetch the corroborating edges
    corroborators = conn.execute(
        """SELECT id, from_node, created_by, confidence, provenance
           FROM ohm_edges
           WHERE to_node = ?
             AND edge_type = ?
             AND layer = ?
             AND deleted_at IS NULL
             AND created_by != ?
             AND created_by IS NOT NULL
           ORDER BY confidence DESC
           LIMIT 20""",
        [to_node, edge_type, layer, created_by],
    ).fetchall()

    return {
        "edge_id": eid,
        "from_node": from_node,
        "to_node": to_node,
        "edge_type": edge_type,
        "layer": layer,
        "confidence": confidence,
        "effective_confidence": round(effective, 4),
        "corroboration_count": count,
        "corroboration_bump": round(effective - confidence, 4),
        "corroborating_edges": [
            {
                "edge_id": r[0],
                "from_node": r[1],
                "created_by": r[2],
                "confidence": r[3],
                "provenance": r[4],
            }
            for r in corroborators
        ],
    }


def effective_confidence_with_corroboration(
    confidence: float,
    corroboration_count: int,
    layer: str = "L3",
) -> float:
    """Compute the effective confidence with a corroboration bump.

    The bump is ``CORROBORATION_BUMP * min(count, MAX_CORROBORATION)``
    applied multiplicatively, capped at 1.0. Only applies to L3 edges
    (knowledge claims); L1/L2/L4 edges are not corroborated.

    Pure function — no DB access. Useful for unit tests and for callers
    that already have the confidence and corroboration_count in memory.
    """
    if layer != "L3" or corroboration_count <= 0:
        return confidence
    bump = CORROBORATION_BUMP * min(corroboration_count, MAX_CORROBORATION)
    return min(1.0, confidence * (1.0 + bump))