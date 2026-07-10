"""Thread-aware belief context using L0 fragments (OHM-767).

Thread beliefs are stored as L0 fragments (via the existing scratch()
system) with posterior parameters in metadata, tagged 'thread_belief',
and connected to the target node. This reuses the mature fragment
lifecycle (TTL eviction, promotion to persistent nodes) rather than
building a parallel system.

Per the #767 comment's recommendation: "represent a thread belief as
an L0 fragment (scratch() with the posterior params in metadata,
tagged/connected to the target node), reuse evict_expired_fragments()
for TTL cleanup, and reuse promote_fragment() for the confirm-to-persist
step."
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = logging.getLogger(__name__)


def store_thread_belief(
    conn: "DuckDBPyConnection",
    *,
    thread_id: str,
    target_node: str,
    posterior: dict[str, float],
    evidence_summary: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Store a thread belief as an L0 fragment (OHM-767).

    Creates a fragment tagged 'thread_belief' with the posterior
    parameters in metadata, connected to the target node. The fragment
    is ephemeral (L0) and will be evicted by the existing TTL mechanism
    unless promoted to a persistent node.

    Returns the created fragment node dict.
    """
    from ohm.graph.queries import scratch

    content = f"Thread {thread_id} belief for {target_node}: P(bad)={posterior.get('P(bad)', 0):.2f}"
    if evidence_summary:
        content += f". Evidence: {evidence_summary}"

    metadata = {
        "thread_id": thread_id,
        "target_node": target_node,
        "posterior": posterior,
        "evidence_summary": evidence_summary,
    }

    return scratch(
        conn,
        content=content,
        created_by=created_by,
        tags=["thread_belief"],
        connects_to=[target_node],
        metadata=metadata,
    )


def get_thread_beliefs(
    conn: "DuckDBPyConnection",
    thread_id: str,
    target_node: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve thread beliefs for a given thread (OHM-767).

    Returns L0 fragments tagged 'thread_belief' with matching thread_id
    in metadata. If target_node is specified, only returns beliefs for
    that target.
    """
    try:
        result = conn.execute(
            """
            SELECT id, label, content, metadata, created_at, created_by
            FROM ohm_nodes
            WHERE type = 'fragment'
              AND deleted_at IS NULL
              AND metadata IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 200
            """,
        )
    except Exception:
        return []

    rows = result.fetchall()
    beliefs = []
    for row in rows:
        try:
            metadata = json.loads(row[3]) if isinstance(row[3], str) else (row[3] if isinstance(row[3], dict) else {})
        except (json.JSONDecodeError, TypeError):
            metadata = {}

        # Filter by thread_id and thread_belief tag in Python (DuckDB JSON
        # filtering is unreliable across versions)
        if metadata.get("thread_id") != thread_id:
            continue
        tags = metadata.get("tags", [])
        if isinstance(tags, str):
            tags = json.loads(tags) if tags else []
        if "thread_belief" not in tags:
            continue
        if target_node and metadata.get("target_node") != target_node:
            continue

        beliefs.append(
            {
                "id": row[0],
                "label": row[1],
                "content": row[2],
                "posterior": metadata.get("posterior", {}),
                "target_node": metadata.get("target_node"),
                "thread_id": metadata.get("thread_id"),
                "created_at": str(row[4]),
                "created_by": row[5],
            }
        )
    return beliefs[:20]


def promote_thread_belief(
    conn: "DuckDBPyConnection",
    fragment_id: str,
    *,
    created_by: str,
) -> dict[str, Any]:
    """Promote a thread belief fragment to a persistent L1 concept (OHM-767).

    Wraps the existing promote_fragment() function — promotes the L0
    thread belief to a durable L1 concept node so the belief persists
    beyond the thread's TTL.
    """
    from ohm.graph.queries import promote_fragment

    return promote_fragment(conn, fragment_id=fragment_id, promoted_by=created_by)
