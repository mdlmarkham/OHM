"""ADR-021: Relational Tags — extract edge types as tags on endpoint nodes.

When an edge is created, the edge type is added as a tag on both endpoint nodes.
This creates cross-domain tag overlap even when agents use different vocabulary.

Example: Creating a CAUSES edge from concept-and-or-conversion to pattern-truce-treadmill
automatically adds "causes" as a tag on both nodes. Now /suggest?method=cross_domain
finds the bridge without embeddings, without shared vocabulary, instantly.

Relational tags are NEVER removed — only added. This preserves agent autonomy
while creating structural discoverability.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Mapping from edge types to relational tags
# Only well-known edge types get relational tags — not custom types
RELATIONAL_TAG_MAP = {
    # L3 Knowledge edges
    "CAUSES": "causes",
    "ENABLES": "enables",
    "CONSTRAINS": "constrains",
    "INVERTS": "inverts",
    "SUBVERTS": "subverts",
    "EMBODIES": "embodies",
    "EXEMPLIFIES": "exemplifies",
    "DEPENDS_ON": "depends_on",
    "CONTRADICTS": "contradicts",
    "REFERENCES": "references",
    "CHALLENGED_BY": "challenged_by",
    "APPLIES_TO": "applies_to",
    "SUPPORTS": "supports",
    "CORRELATES_WITH": "correlates_with",
    "INFLUENCES": "influences",
    "THREATENS": "threatens",
    "BLOCKS": "blocks",
    # L2 Citation edges
    "CITES": "cites",
    "SUPPORTED_BY": "supported_by",
    "REFUTED_BY": "refuted_by",
    # L1 Structure edges
    "VALUES": "values",
    "GOALS": "goals",
    "CAPABLE_OF": "capable_of",
    "LOCATED_IN": "located_in",
    "VERSION_OF": "version_of",
    "RUNS_ON": "runs_on",
    "HOSTS": "hosts",
    "UPSTREAM_OF": "upstream_of",
    # L0 Fragment edges
    "CONTEXT_OF": "context_of",
    "INSPIRED_BY": "inspired_by",
    "CONTRADICTS_FRAG": "contradicts_frag",
    "REFINES_FRAG": "refines_frag",
    "RESONANCE": "resonance",
}


def add_relational_tags(
    conn,
    from_node: str,
    to_node: str,
    edge_type: str,
) -> dict:
    """Add the edge type as a relational tag on both endpoint nodes.

    Args:
        conn: DuckDB connection
        from_node: Source node ID
        to_node: Target node ID
        edge_type: The edge type (e.g., "CAUSES")

    Returns:
        Dict with updated nodes and tags added.
    """
    tag = RELATIONAL_TAG_MAP.get(edge_type)
    if not tag:
        # Unknown edge type — don't auto-tag
        return {"tags_added": [], "nodes_updated": []}

    updated = []
    tags_added = []

    for node_id in [from_node, to_node]:
        try:
            row = conn.execute(
                "SELECT id, tags FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [node_id],
            ).fetchone()
            if not row:
                continue

            nid, tags_json = row
            try:
                existing_tags = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
            except (json.JSONDecodeError, TypeError):
                existing_tags = []

            if tag not in existing_tags:
                new_tags = existing_tags + [tag]
                conn.execute(
                    "UPDATE ohm_nodes SET tags = ? WHERE id = ?",
                    [json.dumps(new_tags), nid],
                )
                updated.append(nid)
                tags_added.append(tag)
                logger.info(f"Added relational tag '{tag}' to node '{nid}' from {edge_type} edge")

        except Exception as e:
            # Never fail the edge creation because of tag enrichment
            logger.warning(f"Failed to add relational tag to node '{node_id}': {e}")
            continue

    return {"tags_added": tags_added, "nodes_updated": updated}


def backfill_relational_tags(conn) -> dict:
    """Backfill relational tags for all existing edges.

    Scans all non-deleted edges and adds relational tags to both endpoints.
    Used as a one-time migration.

    Returns:
        Dict with counts of tags added and nodes updated.
    """
    edges = conn.execute(
        "SELECT from_node, to_node, edge_type FROM ohm_edges WHERE deleted_at IS NULL",
    ).fetchall()

    total_tags_added = 0
    total_nodes_updated = set()

    for from_node, to_node, edge_type in edges:
        result = add_relational_tags(conn, from_node, to_node, edge_type)
        total_tags_added += len(result["tags_added"])
        total_nodes_updated.update(result["nodes_updated"])

    return {
        "edges_scanned": len(edges),
        "tags_added": total_tags_added,
        "nodes_updated": len(total_nodes_updated),
    }
