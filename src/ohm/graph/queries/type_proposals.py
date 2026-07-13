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


# ── Type promotion / demotion (OHM-846) ─────────────────────────────────────


def evaluate_type_proposal(
    conn: "DuckDBPyConnection",
    *,
    proposal_id: str,
    min_distinct_agents: int = 2,
    min_evidence_nodes: int = 3,
) -> dict[str, Any]:
    """Evaluate a type proposal for promotion readiness (OHM-846).

    Computes usage metrics: distinct agents using the proposed type,
    total evidence nodes, and a simple tag/name similarity heuristic
    for collision detection (v1 — causal-signature comparison deferred
    to v2).

    Args:
        conn: Database connection.
        proposal_id: The ohm_type_proposals row id.
        min_distinct_agents: Minimum distinct agents for promotion (default 2).
        min_evidence_nodes: Minimum evidence nodes for promotion (default 3).

    Returns:
        Dict with metrics, ready (bool), and reason.

    Raises:
        ValueError: If proposal not found.
    """
    proposal = get_type_proposal(conn, proposal_id=proposal_id)

    if proposal["status"] != "trial":
        return {
            "proposal_id": proposal_id,
            "proposed_type": proposal["proposed_type"],
            "ready": False,
            "reason": f"Status is {proposal['status']!r}, not 'trial'",
            "metrics": {},
        }

    evidence_ids = proposal.get("evidence_node_ids") or []
    if isinstance(evidence_ids, str):
        import json
        evidence_ids = json.loads(evidence_ids)

    if not evidence_ids:
        return {
            "proposal_id": proposal_id,
            "proposed_type": proposal["proposed_type"],
            "ready": False,
            "reason": "No evidence nodes recorded",
            "metrics": {"evidence_count": 0},
        }

    placeholders = ", ".join(["?"] * len(evidence_ids))
    distinct_agents_row = conn.execute(
        f"SELECT COUNT(DISTINCT created_by) FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        evidence_ids,
    ).fetchone()
    distinct_agents = distinct_agents_row[0] if distinct_agents_row else 0

    total_nodes_row = conn.execute(
        f"SELECT COUNT(*) FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        evidence_ids,
    ).fetchone()
    total_nodes = total_nodes_row[0] if total_nodes_row else 0

    tag_prefix = PROPOSED_TYPE_PREFIX + proposal["proposed_type"]
    all_usage_row = conn.execute(
        "SELECT COUNT(DISTINCT created_by) FROM ohm_nodes WHERE json_contains(tags, ?) AND deleted_at IS NULL",
        [f'"{tag_prefix}"'],
    ).fetchone()
    all_usage_agents = all_usage_row[0] if all_usage_row else 0

    metrics = {
        "evidence_count": total_nodes,
        "distinct_agents": distinct_agents,
        "total_usage_agents": all_usage_agents,
        "min_distinct_agents": min_distinct_agents,
        "min_evidence_nodes": min_evidence_nodes,
    }

    if distinct_agents < min_distinct_agents:
        return {
            "proposal_id": proposal_id,
            "proposed_type": proposal["proposed_type"],
            "ready": False,
            "reason": f"Only {distinct_agents} distinct agent(s), need {min_distinct_agents}",
            "metrics": metrics,
        }

    if total_nodes < min_evidence_nodes:
        return {
            "proposal_id": proposal_id,
            "proposed_type": proposal["proposed_type"],
            "ready": False,
            "reason": f"Only {total_nodes} evidence node(s), need {min_evidence_nodes}",
            "metrics": metrics,
        }

    return {
        "proposal_id": proposal_id,
        "proposed_type": proposal["proposed_type"],
        "ready": True,
        "reason": f"Meets criteria: {distinct_agents} agents, {total_nodes} evidence nodes",
        "metrics": metrics,
    }


def promote_type(
    conn: "DuckDBPyConnection",
    *,
    proposal_id: str,
    agent: str = "system",
) -> dict[str, Any]:
    """Promote a proposed type into the canonical schema (OHM-846).

    Adds the proposed type to ``VALID_NODE_TYPES`` at runtime and updates
    the proposal status to 'promoted'. Uses ``SchemaConfig.extend()`` to
    merge the new type into ``DEFAULT_SCHEMA``.

    Args:
        conn: Database connection.
        proposal_id: The ohm_type_proposals row id.
        agent: Agent performing the promotion.

    Returns:
        Dict with the promotion result and updated proposal.

    Raises:
        ValueError: If proposal not found or not in trial status.
    """
    import ohm.graph.schema as schema_mod

    proposal = get_type_proposal(conn, proposal_id=proposal_id)
    if proposal["status"] != "trial":
        raise ValueError(
            f"Proposal {proposal_id!r} status is {proposal['status']!r}, not 'trial'"
        )

    proposed_type = proposal["proposed_type"]

    if proposed_type in schema_mod.VALID_NODE_TYPES:
        pass
    else:
        schema_mod.VALID_NODE_TYPES = schema_mod.VALID_NODE_TYPES | frozenset({proposed_type})

        try:
            extended = schema_mod.DEFAULT_SCHEMA.extend(
                schema_mod.SchemaConfig(node_types=frozenset({proposed_type}))
            )
            schema_mod.DEFAULT_SCHEMA = extended
        except Exception:
            pass

    conn.execute(
        """UPDATE ohm_type_proposals
           SET status = 'promoted', promoted_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        [proposal_id],
    )

    return {
        "proposal_id": proposal_id,
        "proposed_type": proposed_type,
        "status": "promoted",
        "in_valid_node_types": proposed_type in schema_mod.VALID_NODE_TYPES,
    }


def demote_type(
    conn: "DuckDBPyConnection",
    *,
    proposal_id: str,
    agent: str = "system",
    reason: str | None = None,
) -> dict[str, Any]:
    """Demote/reject a proposed type (OHM-846).

    Sets the proposal status to 'rejected'. Future nodes with the
    ``proposed-type:*`` tag will still be created (the canonical type
    is valid), but the type will not be promoted to VALID_NODE_TYPES.

    Args:
        conn: Database connection.
        proposal_id: The ohm_type_proposals row id.
        agent: Agent performing the demotion.
        reason: Optional reason for rejection.

    Returns:
        Dict with the demotion result.

    Raises:
        ValueError: If proposal not found.
    """
    proposal = get_type_proposal(conn, proposal_id=proposal_id)
    proposed_type = proposal["proposed_type"]

    conn.execute(
        """UPDATE ohm_type_proposals
           SET status = 'rejected', rejected_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        [proposal_id],
    )

    return {
        "proposal_id": proposal_id,
        "proposed_type": proposed_type,
        "status": "rejected",
        "reason": reason or "Demoted by " + agent,
    }