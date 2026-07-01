"""Write a parsed document tree into the OHM graph.

Creates source/section/paragraph/table/list nodes and CONTAINS/PART_OF
edges. Optionally links leaf text to existing concept nodes via lightweight
semantic matching.
"""

from __future__ import annotations

from typing import Any

from ohm.graph.queries import create_edge, create_node
from ohm.ingestion.document_tree import DocumentTree, keyword_overlap


def ingest_document_tree(
    conn,
    tree: DocumentTree,
    created_by: str,
    *,
    link_concepts: bool = True,
    concept_labels: list[str] | None = None,
    provenance: str = "ingestion",
    source_url: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Ingest a DocumentTree into the OHM graph.

    Args:
        conn: Active DuckDB connection.
        tree: Parsed DocumentTree (from parse_document()).
        created_by: Agent/agent name creating nodes.
        link_concepts: If True, leaf nodes are matched against concept_labels
            and SUPPORTS edges are created for strong overlaps.
        concept_labels: Candidate labels to match against. If None, existing
            concept nodes are queried from the graph.
        provenance: Provenance tag for created nodes/edges.
        source_url: URL for the source node.
        tags: Optional tags for the source node.

    Returns:
        Dict with created node ids, edge ids, and matched concepts.
    """
    if concept_labels is None and link_concepts:
        rows = conn.execute("SELECT label FROM ohm_nodes WHERE type = 'concept' AND deleted_at IS NULL").fetchall()
        concept_labels = [r[0] for r in rows]

    node_id_map: dict[str, str] = {}
    created_node_ids: list[str] = []
    created_edge_ids: list[str] = []
    matched_concepts: list[dict[str, Any]] = []

    # Create or update source node (root)
    source_record = create_node(
        conn,
        label=tree.title or tree.source_id,
        node_type="source",
        content=tree.root.text,
        created_by=created_by,
        provenance=provenance,
        url=source_url,
        tags=tags,
    )
    source_id = source_record["id"]
    node_id_map[tree.root.id] = source_id
    created_node_ids.append(source_id)

    # Map document node types to OHM node types
    type_map = {
        "section": "concept",  # sections are structural concepts
        "paragraph": "fragment",
        "list": "fragment",
        "table": "fragment",
        "block": "fragment",
    }

    # Create all non-root nodes
    for dnode in tree.flat:
        if dnode.id == tree.root.id:
            continue
        ohm_type = type_map.get(dnode.node_type, "fragment")
        metadata = {
            **dnode.metadata,
            "doc_level": dnode.level,
            "doc_position": dnode.position,
            "doc_node_type": dnode.node_type,
        }
        if dnode.title:
            metadata["heading"] = dnode.title
        label_text = dnode.title or dnode.text or dnode.node_type
        if len(label_text) > 80:
            label_text = label_text[:80] + "..."
        record = create_node(
            conn,
            label=label_text,
            node_type=ohm_type,
            content=dnode.text,
            created_by=created_by,
            provenance=provenance,
            metadata=metadata,
        )
        node_id_map[dnode.id] = record["id"]
        created_node_ids.append(record["id"])

    # Create CONTAINS / PART_OF edges
    for dnode in tree.flat:
        if dnode.id == tree.root.id:
            continue
        from_id = node_id_map.get(dnode.parent_id or tree.root.id)
        to_id = node_id_map.get(dnode.id)
        if from_id is None or to_id is None:
            continue
        edge = create_edge(
            conn,
            from_node=from_id,
            to_node=to_id,
            layer="L1",
            edge_type="CONTAINS",
            created_by=created_by,
            confidence=0.9,
            provenance=provenance,
            metadata={"doc_node_type": dnode.node_type},
        )
        created_edge_ids.append(edge["id"])

        # Reverse edge (PART_OF) is optional; many schemas use only CONTAINS.
        reverse = create_edge(
            conn,
            from_node=to_id,
            to_node=from_id,
            layer="L1",
            edge_type="PART_OF",
            created_by=created_by,
            confidence=0.9,
            provenance=provenance,
            metadata={"doc_node_type": dnode.node_type},
        )
        created_edge_ids.append(reverse["id"])

    # Link leaf nodes to concepts
    if link_concepts and concept_labels:
        for dnode in tree.flat:
            if dnode.node_type not in {"paragraph", "list", "table", "block"}:
                continue
            matches = keyword_overlap(dnode.text, concept_labels)
            for label, score in matches[:3]:
                if score < 0.15:
                    continue
                # Find concept node id for this label
                row = conn.execute(
                    "SELECT id FROM ohm_nodes WHERE type = 'concept' AND LOWER(label) = LOWER(?) AND deleted_at IS NULL LIMIT 1",
                    [label],
                ).fetchone()
                if row is None:
                    continue
                concept_id = row[0]
                leaf_id = node_id_map.get(dnode.id)
                if leaf_id is None:
                    continue
                edge = create_edge(
                    conn,
                    from_node=leaf_id,
                    to_node=concept_id,
                    layer="L2",
                    edge_type="REFERENCES",
                    created_by=created_by,
                    confidence=min(0.95, max(0.5, score)),
                    provenance=provenance,
                    metadata={"match_score": score, "match_method": "keyword_overlap"},
                )
                created_edge_ids.append(edge["id"])
                matched_concepts.append(
                    {
                        "leaf_id": leaf_id,
                        "concept_id": concept_id,
                        "concept_label": label,
                        "score": score,
                    }
                )

    return {
        "source_id": source_id,
        "created_nodes": created_node_ids,
        "created_edges": created_edge_ids,
        "matched_concepts": matched_concepts,
    }
