"""Soft type-validation mode for schema-extension trials (OHM-848).

Detects ``proposed-type:*`` tags on nodes and auto-creates/updates
``ohm_type_proposals`` rows. Provides collision detection via
causal-signature comparison (L3 CAUSES edge patterns).

No validation bypass is needed — agents create nodes with valid
canonical types (e.g. ``concept``) and add ``proposed-type:<name>``
tags. The tag carries the semantic intent; the canonical type carries
the schema validity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _rows_to_dicts

PROPOSED_TYPE_PREFIX = "proposed-type:"


def detect_proposed_types(tags: list[str] | None) -> list[str]:
    """Extract proposed-type names from a list of tags.

    Args:
        tags: List of tag strings (e.g. ``["proposed-type:signal", "scope:q3"]``).

    Returns:
        List of proposed type names without the prefix (e.g. ``["signal"]``).
    """
    if not tags:
        return []
    return [
        t[len(PROPOSED_TYPE_PREFIX):]
        for t in tags
        if t.startswith(PROPOSED_TYPE_PREFIX)
    ]


def register_type_proposal(
    conn: "DuckDBPyConnection",
    *,
    proposed_type: str,
    proposed_by: str | None = None,
    domain: str | None = None,
    evidence_node_id: str | None = None,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Create or update a type proposal row (OHM-848).

    If a proposal for this type already exists in ``trial`` status,
    appends the evidence node id and updates ``updated_at``.

    Args:
        conn: Database connection.
        proposed_type: The proposed type name (without prefix).
        proposed_by: Agent who first proposed this type.
        domain: Optional domain label.
        evidence_node_id: Node id that triggered this proposal.
        tenant_id: Tenant identifier (default empty).

    Returns:
        The proposal row as a dict.
    """
    import json

    existing = _rows_to_dicts(conn.execute(
        "SELECT * FROM ohm_type_proposals WHERE proposed_type = ? AND status = 'trial' AND tenant_id = ?",
        [proposed_type, tenant_id],
    ))

    if existing:
        row = existing[0]
        evidence_ids = row.get("evidence_node_ids") or []
        if isinstance(evidence_ids, str):
            evidence_ids = json.loads(evidence_ids)
        if evidence_node_id and evidence_node_id not in evidence_ids:
            evidence_ids.append(evidence_node_id)
        conn.execute(
            "UPDATE ohm_type_proposals SET evidence_node_ids = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [json.dumps(evidence_ids), row["id"]],
        )
        return _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_type_proposals WHERE id = ?", [row["id"]]
        ))[0]

    conn.execute(
        """INSERT INTO ohm_type_proposals
           (proposed_type, proposed_by, domain, tenant_id, evidence_node_ids)
           VALUES (?, ?, ?, ?, ?)""",
        [
            proposed_type,
            proposed_by,
            domain,
            tenant_id,
            json.dumps([evidence_node_id] if evidence_node_id else []),
        ],
    )

    return _rows_to_dicts(conn.execute(
        "SELECT * FROM ohm_type_proposals WHERE proposed_type = ? AND status = 'trial' AND tenant_id = ? ORDER BY created_at DESC LIMIT 1",
        [proposed_type, tenant_id],
    ))[0]


def list_type_proposals(
    conn: "DuckDBPyConnection",
    *,
    status: str | None = None,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """List type proposals, optionally filtered by status (OHM-848).

    Args:
        conn: Database connection.
        status: Filter by status (trial, promoted, rejected, deprecated).
        tenant_id: Tenant filter.

    Returns:
        List of proposal rows.
    """
    conditions = ["tenant_id = ?"]
    params: list[Any] = [tenant_id]
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = " AND ".join(conditions)
    return _rows_to_dicts(conn.execute(
        f"SELECT * FROM ohm_type_proposals WHERE {where} ORDER BY created_at DESC",
        params,
    ))


def get_type_proposal(
    conn: "DuckDBPyConnection",
    *,
    proposal_id: str,
) -> dict[str, Any]:
    """Get a single type proposal by id (OHM-848).

    Raises:
        ValueError: If proposal not found.
    """
    rows = _rows_to_dicts(conn.execute(
        "SELECT * FROM ohm_type_proposals WHERE id = ?", [proposal_id]
    ))
    if not rows:
        raise ValueError(f"Type proposal {proposal_id!r} not found")
    return rows[0]


def detect_collisions(
    conn: "DuckDBPyConnection",
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Detect proposed types with structurally equivalent causal signatures.

    Compares L3 CAUSES edge patterns of nodes tagged with different
    ``proposed-type:*`` values. Two proposed types collide when their
    nodes have CAUSES edges to the same set of target node types.

    Args:
        conn: Database connection.
        tenant_id: Tenant filter.

    Returns:
        List of collision pairs: ``{type_a, type_b, shared_targets}``.
    """
    import json

    rows = _rows_to_dicts(conn.execute("""
        SELECT n.id, n.tags
        FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND n.tags IS NOT NULL
    """))

    type_to_targets: dict[str, set[str]] = {}

    for row in rows:
        tags = row.get("tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                continue
        if not tags:
            continue

        proposed = detect_proposed_types(tags)
        if not proposed:
            continue

        edge_rows = _rows_to_dicts(conn.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'CAUSES' AND deleted_at IS NULL""",
            [row["id"]],
        ))
        targets = {e["to_node"] for e in edge_rows}
        if not targets:
            continue

        for pt in proposed:
            if pt not in type_to_targets:
                type_to_targets[pt] = set()
            type_to_targets[pt].update(targets)

    proposed_types = sorted(type_to_targets.keys())
    collisions = []
    for i, a in enumerate(proposed_types):
        for b in proposed_types[i + 1:]:
            shared = type_to_targets[a] & type_to_targets[b]
            if shared:
                collisions.append({
                    "type_a": a,
                    "type_b": b,
                    "shared_targets": sorted(shared),
                })

    return collisions


def process_node_tags(
    conn: "DuckDBPyConnection",
    *,
    node_id: str,
    tags: list[str] | None,
    created_by: str | None = None,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Detect proposed-type tags on a node and register proposals (OHM-848).

    Called after node creation/update. For each ``proposed-type:*`` tag
    found, creates or updates a type proposal row.

    Args:
        conn: Database connection.
        node_id: The node that was just created/updated.
        tags: The node's tags.
        created_by: Agent who created the node.
        tenant_id: Tenant identifier.

    Returns:
        List of proposal rows created/updated.
    """
    proposed = detect_proposed_types(tags)
    if not proposed:
        return []

    proposals = []
    for pt in proposed:
        proposal = register_type_proposal(
            conn,
            proposed_type=pt,
            proposed_by=created_by,
            evidence_node_id=node_id,
            tenant_id=tenant_id,
        )
        proposals.append(proposal)

    return proposals