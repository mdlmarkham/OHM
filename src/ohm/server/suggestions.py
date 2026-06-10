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
import time
from typing import Any

logger = logging.getLogger(__name__)

# Per-agent island nudge cache: {agent_name: (timestamp, result_or_None)}
# Prevents recomputing island detection on every write (OHM-tr71.4).
_island_nudge_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_ISLAND_CACHE_TTL = 300  # 5 minutes

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

    # ── 2b. Cross-domain bridges ──────────────────────────────────────────
    # Find nodes from OTHER agents that share tags or semantic similarity.
    # This addresses Socrates's key gap: /suggest_connections only finds
    # intra-domain (same-agent) connections, not cross-domain bridges.
    created_by = None
    try:
        row = store.conn.execute(
            "SELECT created_by FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        if row:
            created_by = row[0]
    except Exception:
        pass

    if created_by and tags:
        try:
            suggestions["cross_domain"] = _find_cross_domain(store, node_id, created_by, tags)
        except Exception as e:
            logger.debug(f"Cross-domain suggestion failed: {e}")

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
        "SELECT id, label, type, tags FROM ohm_nodes WHERE tags IS NOT NULL AND tags != '[]' AND deleted_at IS NULL AND id != ?",
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
            scored.append(
                {
                    "id": nid,
                    "label": nlabel or "",
                    "type": ntype or "",
                    "shared_tags": sorted(tag_set & ntags),
                    "overlap_count": overlap,
                }
            )

    # Sort by overlap count descending, return top 3
    scored.sort(key=lambda x: x["overlap_count"], reverse=True)
    return scored[:MAX_SHARED_TAGS]


def _find_cross_domain(
    store: Any,
    node_id: str,
    created_by: str,
    tags: list[str],
) -> list[dict[str, Any]]:
    """Find nodes from OTHER agents that share tags with this node.

    This is the cross-domain bridge: Socrates's manipulation patterns
    should connect to Metis's AND-gate framework, not just other
    manipulation patterns. Cross-domain connections score HIGHER
    because they're more surprising and valuable.
    """
    if not tags or not created_by:
        return []

    # Find nodes NOT created by this agent that share tags
    rows = store.conn.execute(
        "SELECT id, label, type, created_by, tags FROM ohm_nodes WHERE tags IS NOT NULL AND tags != '[]' AND deleted_at IS NULL AND id != ? AND created_by != ?",
        [node_id, created_by],
    ).fetchall()

    tag_set = set(tags)
    scored = []
    for row in rows:
        nid, nlabel, ntype, ncreated_by, ntags_json = row
        try:
            ntags = set(json.loads(ntags_json) if isinstance(ntags_json, str) else (ntags_json or []))
        except (json.JSONDecodeError, TypeError):
            continue
        overlap = len(tag_set & ntags)
        if overlap >= 1:  # Lower threshold for cross-domain (1 shared tag is enough)
            # Cross-domain bonus: score = overlap * 1.5 to surface these above intra-domain
            scored.append(
                {
                    "id": nid,
                    "label": nlabel or "",
                    "type": ntype or "",
                    "created_by": ncreated_by or "",
                    "shared_tags": sorted(tag_set & ntags),
                    "overlap_count": overlap,
                    "cross_domain_score": round(overlap * 1.5, 1),  # Bonus for cross-domain
                }
            )

    # Sort by cross_domain_score descending
    scored.sort(key=lambda x: x["cross_domain_score"], reverse=True)
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
                    similar_orphans.append(
                        {
                            "id": rid,
                            "label": r.get("label", ""),
                            "type": r.get("type", ""),
                            "distance": round(r.get("distance", 1.0), 4),
                        }
                    )
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
                    similar_connected.append(
                        {
                            "id": rid,
                            "label": r.get("label", ""),
                            "type": r.get("type", ""),
                        }
                    )
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
            "SELECT to_node, edge_type, layer, confidence FROM ohm_edges WHERE from_node = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 5",
            [from_node],
        ).fetchall()
        # Edges TO this node
        to_edges = store.conn.execute(
            "SELECT from_node, edge_type, layer, confidence FROM ohm_edges WHERE to_node = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 5",
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
            "SELECT edge_type, COUNT(*) as cnt FROM ohm_edges WHERE from_node = ? AND deleted_at IS NULL GROUP BY edge_type ORDER BY cnt DESC LIMIT 3",
            [from_node],
        ).fetchall()
        # What edge types does to_node typically receive?
        to_types = store.conn.execute(
            "SELECT edge_type, COUNT(*) as cnt FROM ohm_edges WHERE to_node = ? AND deleted_at IS NULL GROUP BY edge_type ORDER BY cnt DESC LIMIT 3",
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
                "SELECT COUNT(*) FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
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
            "SELECT COUNT(*) FROM ohm_nodes WHERE created_by = ? AND deleted_at IS NULL AND type != 'fragment'",
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
        graph_nodes = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment'").fetchone()[0]
        graph_edges = store.conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL").fetchone()[0]

        agent_connectivity = agent_edges / max(agent_nodes, 1)
        graph_connectivity = graph_edges / max(graph_nodes, 1)

        if agent_connectivity >= threshold:
            return None

        return {
            "connectivity_warning": {
                "agent_connectivity": round(agent_connectivity, 2),
                "graph_average": round(graph_connectivity, 2),
                "threshold": threshold,
                "message": (f"Your average connectivity is {agent_connectivity:.1f} edges/node (graph average: {graph_connectivity:.1f}). Consider connecting more of your nodes to the graph via /edge or the 'connects_to' field when creating nodes."),
                "suggestion": "Use /orphans?created_by=YOUR_AGENT to find your disconnected nodes.",
            }
        }
    except Exception as e:
        logger.debug(f"Connectivity nudge failed: {e}")
        return None


def generate_island_nudge(
    store: Any,
    agent: str,
    min_island_size: int = 20,
    min_cross_domain_edges: int = 2,
) -> dict[str, Any] | None:
    """Generate an island isolation warning for agents with large disconnected domains.

    OHM-tr71.4: When an agent's island (disconnected component) has more than
    min_island_size nodes and fewer than min_cross_domain_edges connections to
    other islands, include an island_warning in their write/heartbeat response.

    Results are cached per-agent with a 5-minute TTL to avoid recomputing
    island detection (Union-Find over the full graph) on every write.

    Returns None if no nudge is needed.
    """
    if not agent:
        return None

    from ohm.methods import find_islands

    # Check cache
    now = time.time()
    cached = _island_nudge_cache.get(agent)
    if cached and (now - cached[0]) < _ISLAND_CACHE_TTL:
        return cached[1]

    try:
        island_data = find_islands(
            store.conn,
            exclude_fragments=True,
            min_size=2,
            max_islands=50,
        )
    except Exception as exc:
        logger.debug("Island nudge failed for %s: %s", agent, exc)
        _island_nudge_cache[agent] = (now, None)
        return None

    # Find which island (if any) this agent belongs to
    agent_node_ids = set()
    try:
        rows = store.conn.execute(
            "SELECT id FROM ohm_nodes WHERE created_by = ? AND deleted_at IS NULL AND type != 'fragment'",
            [agent],
        ).fetchall()
        agent_node_ids = {r[0] for r in rows}
    except Exception as exc:
        logger.debug("Island nudge: failed to get agent nodes for %s: %s", agent, exc)
        _island_nudge_cache[agent] = (now, None)
        return None

    if len(agent_node_ids) < min_island_size:
        _island_nudge_cache[agent] = (now, None)
        return None

    # Check if the agent's nodes form part of an isolated island
    agent_island = None
    for island in island_data.get("islands", []):
        island_node_ids = {n["id"] for n in island.get("nodes", [])}
        overlap = island_node_ids & agent_node_ids
        if len(overlap) >= min(min_island_size, len(agent_node_ids) // 2):
            agent_island = island
            break

    if not agent_island:
        _island_nudge_cache[agent] = (now, None)
        return None

    # Check cross-domain edges: how many edges connect this island to others?
    island_id_set = {n["id"] for n in agent_island.get("nodes", [])}
    try:
        cross_edges = store.conn.execute(
            """
            SELECT COUNT(*) FROM ohm_edges
            WHERE deleted_at IS NULL
            AND (
                (from_node IN (SELECT unnest(?::VARCHAR[])) AND to_node NOT IN (SELECT unnest(?::VARCHAR[])))
                OR
                (to_node IN (SELECT unnest(?::VARCHAR[])) AND from_node NOT IN (SELECT unnest(?::VARCHAR[])))
            )
            """,
            [list(island_id_set), list(island_id_set)],
        ).fetchone()[0]
    except Exception as exc:
        logger.debug("Island nudge: cross-edge count failed for %s: %s", agent, exc)
        _island_nudge_cache[agent] = (now, None)
        return None

    if cross_edges >= min_cross_domain_edges:
        _island_nudge_cache[agent] = (now, None)
        return None

    warning = {
        "island_warning": {
            "agent_domain_size": len(agent_node_ids),
            "island_size": agent_island["size"],
            "cross_domain_edges": cross_edges,
            "min_cross_domain_edges": min_cross_domain_edges,
            "message": (f"Your domain has {len(agent_node_ids)} nodes but only {cross_edges} connection(s) to other domains (minimum recommended: {min_cross_domain_edges}). Consider connecting your work to the broader graph."),
            "suggestion": "Use GET /islands to see disconnected components and GET /suggest?node_id=<id> for bridging candidates.",
        }
    }
    _island_nudge_cache[agent] = (now, warning)
    return warning


def clear_island_nudge_cache(agent: str | None = None) -> None:
    """Clear the island nudge cache for testing or config changes."""
    global _island_nudge_cache
    if agent:
        _island_nudge_cache.pop(agent, None)
    else:
        _island_nudge_cache = {}
