"""embeddings queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile

def generate_embedding(
    text: str,
    model: str = "nomic-embed-text",
    ollama_url: str = "http://localhost:11434",
    timeout: float | None = None,
) -> list[float] | None:
    """Generate an embedding vector using Ollama.

    Calls the Ollama API to generate an embedding for the given text.
    Returns None if Ollama is unavailable or the request fails.

    Note: For pluggable embedding backends (OHM-9zk7), use ohm.graph.embeddings directly.
    This function is kept for backward compatibility.

    Args:
        text: Text to embed.
        model: Ollama model name (default: nomic-embed-text, 768 dimensions).
        ollama_url: Ollama API base URL.
        timeout: Optional request timeout in seconds. Uses the backend default
            when None.

    Returns:
        List of floats (embedding vector) or None on failure.
    """
    if not text or not text.strip():
        return None

    # Test/CI guard: skip slow Ollama network attempts when embeddings are not needed.
    import os

    if os.environ.get("OHM_DISABLE_EMBEDDINGS") == "1":
        return None

    from ohm.graph.embeddings import OllamaBackend

    backend = OllamaBackend(model=model, ollama_url=ollama_url)
    embeddings = backend.embed([text], timeout=timeout)
    if embeddings and any(e != 0.0 for e in embeddings[0]):
        return embeddings[0]
    return None


def semantic_search(
    conn: "DuckDBPyConnection",
    query: str,
    limit: int = 10,
    node_type: str | None = None,
    min_confidence: float | None = None,
    include_l0: bool = False,
    membership_weight: float | None = None,
    hd_dim: int = 10000,
    hd_seed: int = 42,
    embedding_timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Search nodes by semantic similarity using embedding vectors.

    Generates an embedding for the query text, then finds the most
    similar nodes using cosine distance on the embedding column.

    Requires:
    - Ollama running locally with an embedding model loaded
    - VSS extension loaded for HNSW index acceleration
    - embedding column on ohm_nodes (migration 0.11.0)

    Args:
        conn: Database connection.
        query: Natural language search query.
        limit: Maximum number of results (default 10).
        node_type: Optional filter by node type.
        min_confidence: Optional minimum confidence threshold.
        include_l0: Include fragment-type nodes (default False, OHM-a5rz.20).
        membership_weight: Optional blend weight in [0, 1] for HD Hamming
            similarity alongside cosine similarity (OHM-xuf4). When None
            (default), pure cosine ranking is returned unchanged. When
            provided, each result also carries ``hd_similarity`` and a
            ``blended_score`` = (1 - w) * cosine_sim + w * hd_sim, and
            results are re-ranked by blended_score descending.
        hd_dim: HD fingerprint dimension (default 10000).
        hd_seed: HD fingerprint seed (default 42).
        embedding_timeout: Optional timeout for the Ollama embedding call.
            When None, uses the backend default. Useful for time-budgeted
            callers such as post-write suggestions.

    Returns:
        List of dicts with node_id, label, type, distance, and confidence.
        When ``membership_weight`` is set, each dict also carries
        ``cosine_similarity``, ``hd_similarity`` (None if node has no
        stored fingerprint), and ``blended_score``.
    """
    if not query or not query.strip():
        return []

    embedding = generate_embedding(query, timeout=embedding_timeout)
    if embedding is None:
        raise ValueError("Ollama is not available. Start Ollama with an embedding model (e.g., 'ollama pull nomic-embed-text') to use semantic search.")

    # Build query with optional filters
    where_clauses = ["embedding IS NOT NULL"]
    params: list[Any] = []

    if node_type is not None:
        where_clauses.append("type = ?")
        params.append(node_type)
    elif not include_l0:
        # OHM-a5rz.20: exclude L0 fragments from default semantic search
        where_clauses.append("type != 'fragment'")

    if min_confidence is not None:
        where_clauses.append("confidence >= ?")
        params.append(min_confidence)

    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT
            id AS node_id,
            label,
            type,
            confidence,
            array_cosine_distance(embedding, ?::FLOAT[768]) AS distance
        FROM ohm_nodes
        WHERE {where_sql}
        ORDER BY distance ASC
        LIMIT ?
    """
    params.append(embedding)
    params.append(limit)

    result = conn.execute(sql, params)
    rows = _rows_to_dicts(result)

    if not rows:
        return rows

    if membership_weight is not None and not 0.0 <= membership_weight <= 1.0:
        raise ValueError(f"membership_weight must be in [0, 1], got {membership_weight}")

    # OHM-nnrw: Compute manifold_density_score for each result.
    # k-NN density = 1 - (mean cosine distance to k nearest neighbors).
    # Computed on-read, no cached column.
    k_density = 5
    node_ids = [r["node_id"] for r in rows]
    placeholders = ",".join(["?"] * len(node_ids))
    embed_rows = conn.execute(
        f"""SELECT id, embedding FROM ohm_nodes
            WHERE id IN ({placeholders}) AND embedding IS NOT NULL""",
        node_ids,
    ).fetchall()
    embed_map: dict[str, list] = {}
    for nid, emb in embed_rows:
        if emb is not None:
            embed_map[nid] = list(emb) if not isinstance(emb, list) else emb

    for r in rows:
        r["geodesic_distance"] = r.get("distance")
        nid = r["node_id"]
        if nid in embed_map:
            emb = embed_map[nid]
            try:
                mean_dist_row = conn.execute(
                    """SELECT AVG(d) FROM (
                        SELECT array_cosine_distance(embedding, ?::FLOAT[768]) AS d
                        FROM ohm_nodes
                        WHERE embedding IS NOT NULL AND id != ?
                        ORDER BY d ASC LIMIT ?
                    )""",
                    [emb, nid, k_density],
                ).fetchone()
                mean_dist = mean_dist_row[0] if mean_dist_row and mean_dist_row[0] is not None else 1.0
                r["manifold_density_score"] = round(max(0.0, 1.0 - float(mean_dist)), 6)
            except Exception:
                r["manifold_density_score"] = None
        else:
            r["manifold_density_score"] = None

    if membership_weight is None:
        return rows

    from ohm.inference.hd import fingerprint_text, hamming_similarity

    query_fp = fingerprint_text(query, dim=hd_dim, seed=hd_seed)
    query_bytes = bytes(query_fp)

    node_ids = [r["node_id"] for r in rows]
    if not node_ids:
        return rows

    placeholders = ",".join(["?"] * len(node_ids))
    fp_rows = conn.execute(
        f"""SELECT id, hd_fingerprint
            FROM ohm_nodes
            WHERE id IN ({placeholders}) AND hd_fingerprint IS NOT NULL""",
        node_ids,
    ).fetchall()

    fp_map: dict[str, bytes] = {}
    expected_len = (hd_dim + 7) // 8
    for nid, fp_blob in fp_rows:
        if fp_blob is None:
            continue
        candidate = bytes(fp_blob) if isinstance(fp_blob, (bytes, bytearray)) else bytes(fp_blob)
        if len(candidate) != expected_len:
            continue
        fp_map[nid] = candidate

    for r in rows:
        distance = r.get("distance")
        cosine_sim = 1.0 - float(distance) if distance is not None else 0.0
        r["cosine_similarity"] = round(cosine_sim, 6)
        nid = r["node_id"]
        if nid in fp_map:
            hd_sim = hamming_similarity(bytearray(query_bytes), bytearray(fp_map[nid]))
            r["hd_similarity"] = round(hd_sim, 6)
            blended = (1.0 - membership_weight) * cosine_sim + membership_weight * hd_sim
        else:
            r["hd_similarity"] = None
            blended = (1.0 - membership_weight) * cosine_sim
        r["blended_score"] = round(blended, 6)

    rows.sort(key=lambda x: x["blended_score"], reverse=True)
    return rows


def search(
    conn: "DuckDBPyConnection",
    query: str,
    limit: int = 20,
    node_type: str | None = None,
    created_by: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_l0: bool = False,
) -> list[dict[str, Any]]:
    """Text search over nodes using ILIKE matching (OHM-a5rz.18).

    Performs case-insensitive ILIKE search on both label and content.
    L0 fragments are excluded by default (matching stats/neighborhood
    behavior per ADR-019). Pass include_l0=True to include them.

    Args:
        conn: Database connection.
        query: Text to search for in labels and content.
        limit: Maximum results (default 20).
        node_type: Optional filter by node type (overrides include_l0).
        created_by: Optional filter by creator.
        since: Optional ISO 8601 lower bound on created_at.
        until: Optional ISO 8601 upper bound on created_at.
        include_l0: Include fragment-type nodes (default False).

    Returns:
        List of matching node records.
    """
    if not query or not query.strip():
        return []

    conditions: list[str] = ["deleted_at IS NULL", "(label ILIKE ? OR content ILIKE ?)"]
    params: list[Any] = [f"%{query}%", f"%{query}%"]

    if node_type:
        conditions.append("type = ?")
        params.append(node_type)
    elif not include_l0:
        conditions.append("type != 'fragment'")

    if created_by:
        conditions.append("created_by = ?")
        params.append(created_by)

    if since:
        conditions.append("created_at >= ?::TIMESTAMP")
        params.append(since)

    if until:
        conditions.append("created_at <= ?::TIMESTAMP")
        params.append(until)

    params.append(limit)
    sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
    result = conn.execute(sql, params)
    return _rows_to_dicts(result)


def fuzzy_search(
    conn: "DuckDBPyConnection",
    query: str,
    limit: int = 20,
    threshold: float = 0.6,
    include_l0: bool = False,
) -> list[dict[str, Any]]:
    """Fuzzy text search using DuckDB's jaro_winkler_similarity (OHM-tr71.9).

    Fallback when ILIKE + semantic search both return 0 results.
    Uses Jaro-Winkler similarity on labels against the query.
    Returns matches with similarity score and match_type='fuzzy'.

    Args:
        conn: Database connection.
        query: Query text to fuzzy-match against labels.
        limit: Maximum results (default 20).
        threshold: Minimum similarity (0-1) to consider a match (default 0.6).
        include_l0: Include fragment-type nodes (default False).

    Returns:
        List of dicts with node fields plus distance and match_type.
    """
    if not query or not query.strip():
        return []

    type_filter = ""
    if not include_l0:
        type_filter = "AND type != 'fragment'"

    params: list[Any] = [query, query, threshold, limit]
    sql = f"""
        SELECT *, jaro_winkler_similarity(LOWER(label), LOWER(?)) AS distance
        FROM ohm_nodes
        WHERE deleted_at IS NULL
          AND jaro_winkler_similarity(LOWER(label), LOWER(?)) >= ?
          {type_filter}
        ORDER BY distance DESC
        LIMIT ?
    """
    try:
        result = conn.execute(sql, params)
        rows = _rows_to_dicts(result)
        for r in rows:
            r["match_type"] = "fuzzy"
            r["distance"] = round(float(r.get("distance", 0)), 4)
        return rows
    except Exception:
        # DuckDB may not have this function on older versions — degrade gracefully
        import logging

        logging.getLogger(__name__).debug("fuzzy_search: jaro_winkler_similarity unavailable, returning empty")
        return []


def update_node_embedding(
    conn: "DuckDBPyConnection",
    node_id: str,
    text: str | None = None,
    ollama_url: str | None = None,
) -> bool:
    """Generate and store an embedding for a node.

    Generates an embedding from the node's label (or custom text)
    and updates the embedding column. Returns False if Ollama is
    unavailable or the node doesn't exist.

    Args:
        conn: Database connection.
        node_id: ID of the node to update.
        text: Optional custom text to embed. Defaults to node label.
        ollama_url: Optional Ollama URL for parallel embedding workers.
            Defaults to localhost. Use for distributed embedding generation
            across multiple GPU nodes.

    Returns:
        True if embedding was updated, False otherwise.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    # Enrich embedding text: label + content + tags
    # Short labels like "Artificial Scarcity" produce shallow embeddings.
    # Concatenating label, content, and tags gives nomic-embed-text richer
    # semantic material to work with. (ADR-021, Socrates Round 4 feedback)
    if text is None:
        result = conn.execute(
            "SELECT label, content, tags FROM ohm_nodes WHERE id = ?",
            [node_id],
        ).fetchone()
        if result is None:
            return False
        label, content, tags_json = result
        parts = []
        if label:
            parts.append(label)
        if content:
            parts.append(content)
        if tags_json:
            import json as _json

            try:
                tags = _json.loads(tags_json) if isinstance(tags_json, str) else tags_json
                if isinstance(tags, list) and tags:
                    parts.append(" ".join(str(t) for t in tags))
            except (ValueError, TypeError):
                pass
        text = "\n".join(parts) if parts else label

    if not text:
        return False

    embedding = generate_embedding(text, ollama_url=ollama_url or "http://localhost:11434")
    if embedding is None:
        return False

    conn.execute(
        "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
        [embedding, node_id],
    )
    return True


