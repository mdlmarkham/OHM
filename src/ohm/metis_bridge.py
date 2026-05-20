"""Metis zettelkasten → OHM bridge (OHM-8c6).

Projects synthesized insights, patterns, and connections from Metis's
private zettelkasten into the OHM shared graph for cross-agent reasoning.

What gets projected:
    - Pattern notes (confidence >= 0.7) → OHM concept/pattern nodes (L3)
    - Wikilinks between notes → OHM edges (REFINES, SUPPORTS, etc.)
    - Tags → node labels or ohm_nodes.tags field
    - Source references → provenance field

What STAYS in Metis private zettelkasten:
    - Raw voice memo transcriptions
    - Half-formed thoughts
    - Notes with confidence < 0.7
    - Personal observations not yet synthesized

The projection is idempotent — re-running doesn't duplicate nodes.
Uses find_or_create_node() and checks for existing edges before creating.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

# Minimum confidence threshold for projection
DEFAULT_CONFIDENCE_THRESHOLD = 0.7

# Edge types derived from wikilink context
# Maps context keyword → (edge_type, layer)
WIKILINK_EDGE_MAP: dict[str, tuple[str, str]] = {
    "refines": ("REFINES", "L3"),
    "supports": ("SUPPORTS", "L3"),
    "derives": ("DERIVES_FROM", "L2"),
    "references": ("REFERENCES", "L2"),
    "contradicts": ("CONTRADICTS", "L3"),
}

# Default edge type for wikilinks without context
DEFAULT_EDGE_TYPE = ("REFERENCES", "L2")

# Cooldown between projections (seconds)
DEFAULT_PROJECTION_INTERVAL = 24 * 60 * 60  # 24 hours


def _extract_wikilinks(text: str) -> list[tuple[str, str | None]]:
    """Extract [[wikilinks]] and optional context from text.

    Supports:
        [[target]]              → (target, None)
        [[target|context]]      → (target, context)

    Returns:
        List of (target, context) tuples.
    """
    if not text:
        return []
    pattern = r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]"
    matches = re.findall(pattern, text)
    return [(target.strip(), context.strip() if context else None) for target, context in matches]


def _derive_edge_type(context: str | None) -> tuple[str, str]:
    """Derive OHM edge type and layer from wikilink context.

    Returns:
        Tuple of (edge_type, layer).
    """
    if not context:
        return DEFAULT_EDGE_TYPE
    context_lower = context.lower()
    for keyword, (edge_type, layer) in WIKILINK_EDGE_MAP.items():
        if keyword in context_lower:
            return (edge_type, layer)
    return DEFAULT_EDGE_TYPE


def _get_zettelkasten_notes(
    metis_conn: "DuckDBPyConnection",
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Query Metis zettelkasten for notes eligible for projection.

    Args:
        metis_conn: Connection to Metis DuckDB.
        min_confidence: Minimum confidence threshold.

    Returns:
        List of note dicts with id, title, content, confidence, tags, type.
    """
    # Try common zettelkasten table names
    table_candidates = ["notes", "zettelkasten", "metis_notes"]
    table_name = None
    for candidate in table_candidates:
        try:
            result = metis_conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name = ?",
                [candidate],
            ).fetchone()
            if result and result[0] > 0:
                table_name = candidate
                break
        except Exception:
            continue

    if table_name is None:
        return []

    # Try to get columns that exist
    try:
        columns_result = metis_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?",
            [table_name],
        ).fetchall()
        available_cols = {col[0] for col in columns_result}
    except Exception:
        return []

    # Build query based on available columns
    select_parts = []
    if "id" in available_cols:
        select_parts.append("id")
    elif "note_id" in available_cols:
        select_parts.append("note_id AS id")

    if "title" in available_cols:
        select_parts.append("title")
    elif "label" in available_cols:
        select_parts.append("label AS title")

    if "content" in available_cols:
        select_parts.append("content")
    elif "body" in available_cols:
        select_parts.append("body AS content")

    if "confidence" in available_cols:
        select_parts.append("confidence")

    if "tags" in available_cols:
        select_parts.append("tags")

    if "type" in available_cols:
        select_parts.append("type")
    elif "note_type" in available_cols:
        select_parts.append("note_type AS type")

    if not select_parts:
        return []

    select_sql = ", ".join(select_parts)
    where_parts = []
    params: list[Any] = []

    if "confidence" in available_cols:
        where_parts.append("confidence >= ?")
        params.append(min_confidence)

    if "type" in available_cols or "note_type" in available_cols:
        type_col = "type" if "type" in available_cols else "note_type"
        where_parts.append(f"({type_col} = 'pattern' OR {type_col} = 'concept')")

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    try:
        cursor = metis_conn.execute(
            f"SELECT {select_sql} FROM {table_name} {where_sql}",
            params,
        )
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except Exception:
        return []


def project_zettelkasten(
    ohm_conn: "DuckDBPyConnection",
    metis_conn: "DuckDBPyConnection" | None = None,
    min_confidence: float = DEFAULT_CONFIDENCE_THRESHOLD,
    provenance: str = "metis_zettelkasten",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Project Metis zettelkasten patterns into the OHM shared graph.

    Reads pattern notes from Metis's zettelkasten (confidence >= threshold)
    and creates corresponding OHM nodes and edges. Idempotent — re-running
    doesn't duplicate nodes.

    Args:
        ohm_conn: Connection to OHM DuckDB.
        metis_conn: Connection to Metis DuckDB. If None, tries to connect
            to the default Metis database path.
        min_confidence: Minimum confidence threshold for projection.
        provenance: Provenance label for projected nodes.
        dry_run: If True, report what would be projected without making changes.

    Returns:
        Dict with nodes_created, edges_created, patterns_skipped, errors.
    """
    from ohm.queries import (
        create_observation,
        create_edge,
        find_or_create_node,
    )

    if metis_conn is None:
        # Try default Metis database path
        metis_db_path = os.environ.get(
            "METIS_DB",
            os.path.expanduser("~/.metis/metis.duckdb"),
        )
        try:
            import duckdb
            metis_conn = duckdb.connect(metis_db_path, read_only=True)
        except Exception:
            return {
                "nodes_created": 0,
                "edges_created": 0,
                "patterns_skipped": 0,
                "errors": [f"Cannot connect to Metis database at {metis_db_path}"],
            }

    # Get eligible notes
    notes = _get_zettelkasten_notes(metis_conn, min_confidence=min_confidence)

    if not notes:
        return {
            "nodes_created": 0,
            "edges_created": 0,
            "patterns_skipped": 0,
            "errors": [],
        }

    nodes_created = 0
    edges_created = 0
    patterns_skipped = 0
    errors: list[str] = []
    node_id_map: dict[str, str] = {}  # title or note_id → ohm_node_id

    # Phase 1: Create OHM nodes for each note
    for note in notes:
        note_id = note.get("id", "")
        title = note.get("title", "")
        content = note.get("content", "")
        confidence = note.get("confidence", 1.0)
        tags = note.get("tags", "")
        note_type = note.get("type", "pattern")

        if not title:
            patterns_skipped += 1
            continue

        # Map note type to OHM node type
        ohm_type = "pattern" if note_type in ("pattern",) else "concept"

        try:
            if dry_run:
                # Just record what would happen
                node_id = f"metis_{note_id}" if note_id else f"metis_{title.lower().replace(' ', '_')[:50]}"
                if note_id:
                    node_id_map[note_id] = node_id
                node_id_map[title] = node_id
                nodes_created += 1
            else:
                # Use find_or_create_node for idempotency
                result = find_or_create_node(
                    ohm_conn,
                    label=title,
                    node_type=ohm_type,
                    created_by="metis",
                    provenance=provenance,
                    confidence=confidence,
                    content=content,
                )
                node_id = result["id"]
                if note_id:
                    node_id_map[note_id] = node_id
                node_id_map[title] = node_id
                nodes_created += 1

                # Update tags if provided (tags not supported by find_or_create_node)
                if tags:
                    try:
                        import json
                        tags_json = json.dumps(tags.split(",") if isinstance(tags, str) else [tags])
                        ohm_conn.execute(
                            "UPDATE ohm_nodes SET tags = ? WHERE id = ? AND tags IS NULL",
                            [tags_json, node_id],
                        )
                    except Exception:
                        pass  # Tags update is non-critical
        except Exception as e:
            errors.append(f"Failed to create node for '{title}': {e}")
            patterns_skipped += 1

    # Phase 2: Create OHM edges for wikilinks
    for note in notes:
        note_id = note.get("id", "")
        title = note.get("title", "")
        content = note.get("content", "")

        source_key = note_id or title
        source_node_id = node_id_map.get(source_key)
        if not source_node_id:
            continue

        wikilinks = _extract_wikilinks(content or "")
        for target_title, context in wikilinks:
            target_key = target_title  # wikilink target is the note title
            target_node_id = node_id_map.get(target_key)

            if not target_node_id:
                # Target note wasn't projected (below threshold or missing)
                continue

            edge_type, layer = _derive_edge_type(context)

            try:
                if dry_run:
                    edges_created += 1
                else:
                    # Check if edge already exists (idempotency)
                    existing = ohm_conn.execute(
                        "SELECT id FROM ohm_edges "
                        "WHERE from_node = ? AND to_node = ? AND edge_type = ?",
                        [source_node_id, target_node_id, edge_type],
                    ).fetchone()
                    if existing is None:
                        create_edge(
                            ohm_conn,
                            from_node=source_node_id,
                            to_node=target_node_id,
                            edge_type=edge_type,
                            layer=layer,
                            created_by="metis",
                            confidence=note.get("confidence", 0.8),
                        )
                        edges_created += 1
            except Exception as e:
                errors.append(f"Failed to create edge {source_node_id}→{target_node_id}: {e}")

    # Phase 3: Record projection in observations
    if not dry_run and (nodes_created > 0 or edges_created > 0):
        try:
            create_observation(
                ohm_conn,
                node_id="metis",  # Metis agent node
                obs_type="projection",
                value=float(nodes_created),
                notes=f"Projected {nodes_created} patterns, {edges_created} edges from zettelkasten",
                created_by="metis",
            )
        except Exception:
            pass  # Non-critical — projection succeeded even if observation fails

    return {
        "nodes_created": nodes_created,
        "edges_created": edges_created,
        "patterns_skipped": patterns_skipped,
        "errors": errors,
    }
