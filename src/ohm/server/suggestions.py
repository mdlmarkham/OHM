"""Post-write suggestions — help agents discover related nodes immediately.

When an agent creates a node, the response includes suggestions for similar
nodes, shared tags, and orphan connections. This is the proactive discoverability
layer (OHM-tr71.1): the graph tells you what's relevant, rather than waiting
for you to search.

ADR-019 (L0) + ADR-021 (Proactive Discoverability): The graph should feel
like it's paying attention, not waiting for the right question.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Maximum suggestions per category
MAX_SIMILAR = 3
MAX_SHARED_TAGS = 3
MAX_ORPHAN_SUGGESTIONS = 3

# Performance budget: if suggestions take longer than this, skip them
SUGGESTION_TIMEOUT_S = 0.5


def generate_suggestions(
    store: Any,
    node_id: str,
    content: str | None = None,
    label: str | None = None,
    tags: list[str] | None = None,
    node_type: str | None = None,
    has_edges: bool = False,
) -> dict[str, Any]:
    """Generate post-write suggestions for a newly created node.

    Returns a dict with:
        similar_nodes: top 3 nodes by embedding similarity
        shared_tags: top 3 nodes sharing tags with this node
        orphan_warning: suggested connections if this node has no edges (or None)

    Suggestions never fail the write. If semantic search is unavailable,
    returns empty lists. This function is defensive — every path returns
    a valid suggestions dict.
    """
    suggestions: dict[str, Any] = {
        "similar_nodes": [],
        "shared_tags": [],
        "orphan_warning": None,
    }

    # ── 1. Similar nodes (semantic search) ────────────────────────────────
    query_text = content or label or ""
    if query_text.strip():
        try:
            from ohm.graph.queries import semantic_search

            similar = semantic_search(
                store.conn,
                query=query_text[:500],  # Limit query length
                limit=MAX_SIMILAR + 1,  # +1 to exclude self
                include_l0=False,
            )
            # Exclude the newly created node from its own suggestions
            similar = [s for s in similar if s.get("node_id") != node_id][:MAX_SIMILAR]
            suggestions["similar_nodes"] = [
                {
                    "id": s.get("node_id", ""),
                    "label": s.get("label", ""),
                    "type": s.get("type", ""),
                    "distance": round(s.get("distance", 1.0), 4),
                }
                for s in similar
            ]
        except (ValueError, ImportError, Exception) as e:
            # Ollama unavailable or search failed — suggestions are enhancement, not requirement
            logger.debug(f"Semantic search unavailable for suggestions: {e}")

    # ── 2. Shared tags ──────────────────────────────────────────────────
    if tags:
        try:
            suggestions["shared_tags"] = _find_shared_tags(store, node_id, tags)
        except Exception as e:
            logger.debug(f"Tag overlap query failed: {e}")

    # ── 3. Orphan warning ────────────────────────────────────────────────
    if not has_edges:
        try:
            suggestions["orphan_warning"] = _find_orphan_connections(store, node_id, query_text)
        except Exception as e:
            logger.debug(f"Orphan suggestion query failed: {e}")

    return suggestions


def _find_shared_tags(
    store: Any,
    node_id: str,
    tags: list[str],
) -> list[dict[str, Any]]:
    """Find nodes that share tags with the newly created node.

    Returns top 3 nodes by tag overlap count, excluding self.
    """
    if not tags:
        return []

    # Query nodes with non-empty tags
    rows = store.conn.execute(
        "SELECT id, label, type, tags FROM ohm_nodes "
        "WHERE tags IS NOT NULL AND tags != '[]' AND deleted_at IS NULL AND id != ?",
        [node_id],
    ).fetchall()

    # Calculate tag overlap for each node
    scored = []
    tag_set = set(tags)
    for row in rows:
        nid, nlabel, ntype, ntags_json = row
        try:
            ntags = set(json.loads(ntags_json) if isinstance(ntags_json, str) else (ntags_json or []))
        except (json.JSONDecodeError, TypeError):
            continue
        overlap = len(tag_set & ntags)
        if overlap > 0:
            scored.append({
                "id": nid,
                "label": nlabel or "",
                "type": ntype or "",
                "shared_tags": sorted(tag_set & ntags),
                "overlap_count": overlap,
            })

    # Sort by overlap count descending, return top 3
    scored.sort(key=lambda x: x["overlap_count"], reverse=True)
    return scored[:MAX_SHARED_TAGS]


def _find_orphan_connections(
    store: Any,
    node_id: str,
    query_text: str,
) -> dict[str, Any] | None:
    """Suggest connections for an orphan node (no edges).

    Uses semantic search to find similar orphans, then suggests connecting
    to both similar orphans and similar connected nodes.
    """
    # Find orphans (nodes with no edges)
    orphans = store.conn.execute(
        "SELECT n.id, n.label, n.type FROM ohm_nodes n "
        "WHERE n.deleted_at IS NULL AND n.id != ? "
        "AND n.id NOT IN (SELECT from_node FROM ohm_edges WHERE deleted_at IS NULL) "
        "AND n.id NOT IN (SELECT to_node FROM ohm_edges WHERE deleted_at IS NULL) "
        "AND n.type != 'fragment' "
        "ORDER BY n.created_at DESC LIMIT 50",
        [node_id],
    ).fetchall()

    if not orphans:
        return None

    # If we have semantic search available, find semantically similar orphans
    similar_orphans = []
    if query_text.strip():
        try:
            from ohm.graph.queries import semantic_search

            results = semantic_search(
                store.conn,
                query=query_text[:500],
                limit=10,
                include_l0=False,
            )
            orphan_ids = {o[0] for o in orphans}
            for r in results:
                rid = r.get("node_id", "")
                if rid in orphan_ids and rid != node_id:
                    similar_orphans.append({
                        "id": rid,
                        "label": r.get("label", ""),
                        "type": r.get("type", ""),
                        "distance": round(r.get("distance", 1.0), 4),
                    })
        except (ValueError, ImportError, Exception):
            pass

    # Also suggest some connected nodes (not orphans) that are semantically similar
    similar_connected = []
    if query_text.strip():
        try:
            from ohm.graph.queries import semantic_search

            results = semantic_search(
                store.conn,
                query=query_text[:500],
                limit=5,
                include_l0=False,
            )
            for r in results:
                rid = r.get("node_id", "")
                if rid != node_id and rid not in {o[0] for o in orphans}:
                    similar_connected.append({
                        "id": rid,
                        "label": r.get("label", ""),
                        "type": r.get("type", ""),
                    })
        except (ValueError, ImportError, Exception):
            pass

    warning = {
        "message": f"This node has no edges yet. Consider connecting it to existing nodes.",
        "similar_orphans": similar_orphans[:MAX_ORPHAN_SUGGESTIONS],
        "similar_connected": similar_connected[:2],
    }

    return warning if (similar_orphans or similar_connected) else None


def generate_edge_suggestions(
    store: Any,
    from_node: str,
    to_node: str,
    edge_type: str,
    layer: str = "L3",
) -> dict[str, Any]:
    """Generate post-write suggestions for a newly created edge.

    Returns a dict with:
        related_edges: other edges involving these nodes or similar patterns
        edge_patterns: common edge types from these nodes (suggest richer connections)
        orphan_resolved: True if this edge resolved an orphan warning

    Suggestions never fail the write. Every path returns a valid dict.
    """
    suggestions: dict[str, Any] = {
        "related_edges": [],
        "edge_patterns": [],
        "orphan_resolved": False,
    }

    # ── 1. Related edges — what else connects to/from these nodes? ─────────
    try:
        # Edges FROM this node
        from_edges = store.conn.execute(
            "SELECT to_node, edge_type, layer, confidence FROM ohm_edges "
            "WHERE from_node = ? AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 5",
            [from_node],
        ).fetchall()
        # Edges TO this node
        to_edges = store.conn.execute(
            "SELECT from_node, edge_type, layer, confidence FROM ohm_edges "
            "WHERE to_node = ? AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 5",
            [to_node],
        ).fetchall()

        related = []
        for row in from_edges:
            related.append({"node": row[0], "edge_type": row[1], "direction": "from"})
        for row in to_edges:
            related.append({"node": row[0], "edge_type": row[1], "direction": "to"})
        suggestions["related_edges"] = related[:5]
    except Exception as e:
        logger.debug(f"Related edges query failed: {e}")

    # ── 2. Edge patterns — common edge types from these node neighborhoods ──
    try:
        # What edge types does from_node typically use?
        from_types = store.conn.execute(
            "SELECT edge_type, COUNT(*) as cnt FROM ohm_edges "
            "WHERE from_node = ? AND deleted_at IS NULL "
            "GROUP BY edge_type ORDER BY cnt DESC LIMIT 3",
            [from_node],
        ).fetchall()
        # What edge types does to_node typically receive?
        to_types = store.conn.execute(
            "SELECT edge_type, COUNT(*) as cnt FROM ohm_edges "
            "WHERE to_node = ? AND deleted_at IS NULL "
            "GROUP BY edge_type ORDER BY cnt DESC LIMIT 3",
            [to_node],
        ).fetchall()

        patterns = []
        for row in from_types:
            if row[0] != edge_type:
                patterns.append({"from": from_node, "edge_type": row[0], "count": row[1]})
        for row in to_types:
            if row[0] != edge_type:
                patterns.append({"to": to_node, "edge_type": row[0], "count": row[1]})
        suggestions["edge_patterns"] = patterns[:5]
    except Exception as e:
        logger.debug(f"Edge patterns query failed: {e}")

    # ── 3. Orphan resolved check ────────────────────────────────────────
    try:
        # Check if from_node or to_node was previously an orphan (no edges)
        # After creating this edge, they're no longer orphans
        for nid in [from_node, to_node]:
            edge_count = store.conn.execute(
                "SELECT COUNT(*) FROM ohm_edges "
                "WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
                [nid, nid],
            ).fetchone()[0]
            # If this was the ONLY edge for this node, it just resolved an orphan state
            if edge_count == 1:
                suggestions["orphan_resolved"] = True
                break
    except Exception as e:
        logger.debug(f"Orphan resolved check failed: {e}")

    return suggestions

def generate_connectivity_nudge(
    store: Any,
    agent: str,
    threshold: float = 1.5,
) -> dict[str, Any] | None:
    """Generate a connectivity nudge for agents with low edges-per-node.

    OHM-tr71.6: When an agent's average connectivity (edges per node)
    falls below the threshold, include a connectivity_warning in their
    write response.

    Returns None if no nudge is needed (agent is well-connected or unknown).
    """
    if not agent:
        return None

    try:
        # Agent's node count (excluding fragments)
        agent_nodes = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes "
            "WHERE created_by = ? AND deleted_at IS NULL AND type != 'fragment'",
            [agent],
        ).fetchone()[0]

        if agent_nodes == 0:
            return None

        # Agent's edge count (edges they created)
        agent_edges = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE created_by = ? AND deleted_at IS NULL",
            [agent],
        ).fetchone()[0]

        # Graph average connectivity
        graph_nodes = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment'"
        ).fetchone()[0]
        graph_edges = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL"
        ).fetchone()[0]

        agent_connectivity = agent_edges / max(agent_nodes, 1)
        graph_connectivity = graph_edges / max(graph_nodes, 1)

        if agent_connectivity >= threshold:
            return None

        return {
            "connectivity_warning": {
                "agent_connectivity": round(agent_connectivity, 2),
                "graph_average": round(graph_connectivity, 2),
                "threshold": threshold,
                "message": (
                    f"Your average connectivity is {agent_connectivity:.1f} edges/node "
                    f"(graph average: {graph_connectivity:.1f}). "
                    f"Consider connecting more of your nodes to the graph via /edge "
                    f"or the 'connects_to' field when creating nodes."
                ),
                "suggestion": "Use /orphans?created_by=YOUR_AGENT to find your disconnected nodes.",
            }
        }
    except Exception as e:
        logger.debug(f"Connectivity nudge failed: {e}")
        return None
