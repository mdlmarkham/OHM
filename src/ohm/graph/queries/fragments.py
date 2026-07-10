"""fragments queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from ohm.graph.queries import create_edge, create_node

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _existing_label


def scratch(
    conn: DuckDBPyConnection,
    *,
    content: str,
    created_by: str,
    tags: list[str] | None = None,
    connects_to: list[str] | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Write an L0 thinking fragment (OHM-a5rz.4).

    Minimal write: content + agent_name. Auto-generates id, label, type='fragment'.
    Extracts URLs from content. Fragments are exempt from cross-link requirements.
    """
    import re
    from ohm.schema import generate_node_id

    if not content or not content.strip():
        raise ValueError("content must be non-empty")

    label = content.strip()[:80]
    url = None
    url_match = re.search(r"https?://\S+", content)
    if url_match:
        url = url_match.group(0).rstrip(".,;:)")

    generate_node_id(label)

    # Merge caller-provided metadata with auto-detected metadata
    auto_metadata = {}
    is_question = "?" in content
    if tags:
        auto_metadata["tags"] = tags
    if is_question:
        auto_metadata["is_question"] = True
    # Caller metadata takes precedence for overlapping keys
    metadata = {**(metadata or {}), **auto_metadata} if (metadata or auto_metadata) else None

    node = create_node(
        conn,
        label=label,
        node_type="fragment",
        content=content,
        created_by=created_by,
        visibility="team",
        provenance="scratch",
        confidence=0.0,
        url=url,
        connects_to=connects_to,
    )
    if metadata:
        import json as _json

        conn.execute(
            "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
            [_json.dumps(metadata), node["id"]],
        )
        node["metadata"] = _json.dumps(metadata)
    node["scratch"] = True

    # OHM-a5rz.17: Create explicit L0 CONTEXT_OF edges for connects_to targets.
    # create_node() validates the targets exist but doesn't create edges.
    explicit_links = []
    if connects_to:
        for target_id in connects_to:
            edge = create_edge(
                conn,
                from_node=node["id"],
                to_node=target_id,
                layer="L0",
                edge_type="CONTEXT_OF",
                created_by=created_by,
                confidence=0.5,
                provenance="scratch_explicit",
            )
            explicit_links.append(
                {
                    "node_id": target_id,
                    "label": _existing_label(conn, target_id),
                    "edge_id": edge["id"],
                    "edge_type": "CONTEXT_OF",
                    "provenance": "scratch_explicit",
                }
            )
    if explicit_links:
        node["explicit_links"] = explicit_links

    auto_links = _auto_link_fragment(conn, node["id"], content, created_by)
    if auto_links:
        node["auto_links"] = auto_links

    # OHM-a5rz.25: Cross-agent fragment resonance
    resonance_edges = _create_resonance_edges(conn, node["id"], created_by, auto_links)
    if resonance_edges:
        node["resonance_links"] = resonance_edges

    return node


def _auto_link_fragment(
    conn: DuckDBPyConnection,
    fragment_id: str,
    content: str,
    created_by: str,
    max_links: int = 5,
) -> list[dict[str, Any]]:
    """Auto-link fragment to existing nodes (OHM-a5rz.8, OHM-a5rz.19).

    Uses semantic embedding similarity when available (OHM-a5rz.19):
    computes fragment embedding, finds top-K nearest nodes by cosine similarity
    above 0.7 threshold, creates L0 CONTEXT_OF edges with provenance
    'auto_link_semantic'.

    Falls back to label-substring matching (OHM-a5rz.8) when:
    - Ollama/embedding service unavailable (generate_embedding returns None)
    - VSS extension not loaded (array_cosine_distance unavailable)

    Skips fragment-type nodes and the fragment itself. Limits to max_links.
    Returns list of created edge records.
    """
    # OHM-a5rz.19: Try semantic auto-linking first
    import ohm.graph.queries as _gq

    embedding = _gq.generate_embedding(content)
    if embedding is not None:
        try:
            sem_links = _semantic_auto_link_fragment(
                conn,
                fragment_id,
                embedding,
                created_by,
                max_links=min(max_links, 3),  # top 3 per spec
            )
            if sem_links:
                return sem_links
        except Exception:
            pass  # Fall through to substring matching

    # OHM-a5rz.8: Fallback — label-substring matching
    return _substring_auto_link_fragment(conn, fragment_id, content, created_by, max_links)


def _semantic_auto_link_fragment(
    conn: DuckDBPyConnection,
    fragment_id: str,
    embedding: list[float],
    created_by: str,
    max_links: int = 3,
) -> list[dict[str, Any]]:
    """Auto-link fragment using semantic embedding similarity (OHM-a5rz.19).

    Finds non-fragment nodes with embeddings closest to the fragment
    embedding using array_cosine_distance. Creates L0 CONTEXT_OF edges
    for matches above the similarity threshold (> 0.7).
    """
    DISTANCE_THRESHOLD = 0.3  # cosine similarity > 0.7 → distance < 0.3

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT id, label, type,
                      array_cosine_distance(embedding, ?::FLOAT[768]) AS distance
               FROM ohm_nodes
               WHERE deleted_at IS NULL
                 AND type != 'fragment'
                 AND embedding IS NOT NULL
                 AND id != ?
                 AND array_cosine_distance(embedding, ?::FLOAT[768]) < ?
               ORDER BY distance ASC
               LIMIT ?""",
            [embedding, fragment_id, embedding, DISTANCE_THRESHOLD, max_links],
        )
    )

    matched = []
    for candidate in candidates:
        edge = create_edge(
            conn,
            from_node=fragment_id,
            to_node=candidate["id"],
            layer="L0",
            edge_type="CONTEXT_OF",
            created_by=created_by,
            confidence=0.3,
            provenance="auto_link_semantic",
        )
        matched.append(
            {
                "node_id": candidate["id"],
                "label": candidate["label"],
                "edge_id": edge["id"],
                "provenance": "auto_link_semantic",
                "similarity": round(1.0 - candidate["distance"], 4),
            }
        )

    return matched


def _substring_auto_link_fragment(
    conn: DuckDBPyConnection,
    fragment_id: str,
    content: str,
    created_by: str,
    max_links: int = 5,
) -> list[dict[str, Any]]:
    """Auto-link fragment to existing nodes whose labels appear in content (OHM-a5rz.8).

    Scans ohm_nodes for labels that are substrings of the fragment content
    (case-insensitive). Creates L0 CONTEXT_OF edges for matches.
    Skips fragment-type nodes and the fragment itself. Limits to max_links.
    """
    content_lower = content.lower()

    candidates = _rows_to_dicts(
        conn.execute(
            "SELECT id, label, type FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment' ORDER BY LENGTH(label) DESC",
        )
    )

    matched = []
    for candidate in candidates:
        if candidate["id"] == fragment_id:
            continue
        if len(matched) >= max_links:
            break
        label_lower = candidate["label"].lower()
        if len(label_lower) >= 4 and label_lower in content_lower:
            edge = create_edge(
                conn,
                from_node=fragment_id,
                to_node=candidate["id"],
                layer="L0",
                edge_type="CONTEXT_OF",
                created_by=created_by,
                confidence=0.3,
                provenance="auto_link_substring",
            )
            matched.append(
                {
                    "node_id": candidate["id"],
                    "label": candidate["label"],
                    "edge_id": edge["id"],
                    "provenance": "auto_link_substring",
                }
            )

    return matched


def _create_resonance_edges(
    conn: DuckDBPyConnection,
    fragment_id: str,
    created_by: str,
    auto_links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create RESONANCE edges when fragments from different agents share auto-link targets (OHM-a5rz.25).

    After a fragment auto-links to targets, checks if other fragments from
    different agents also link to the same targets. Creates L0 RESONANCE
    edges between the current fragment and matching fragments.

    Returns list of created resonance edge records.
    """
    if not auto_links:
        return []

    target_ids = [link["node_id"] for link in auto_links]
    placeholders = ",".join(["?"] * len(target_ids))

    rows = _rows_to_dicts(
        conn.execute(
            f"""SELECT e.from_node AS fragment_id, f.created_by AS agent,
                       e.to_node AS shared_target
                FROM ohm_edges e
                JOIN ohm_nodes f ON e.from_node = f.id
                WHERE e.edge_type = 'CONTEXT_OF' AND e.layer = 'L0' AND e.deleted_at IS NULL
                  AND f.type = 'fragment' AND f.deleted_at IS NULL
                  AND f.id != ?
                  AND f.created_by != ?
                  AND e.to_node IN ({placeholders})
            """,
            [fragment_id, created_by] + target_ids,
        )
    )

    if not rows:
        return []

    # Group by fragment, collecting shared targets
    fragment_targets: dict[str, dict[str, Any]] = {}
    for row in rows:
        fid = row["fragment_id"]
        if fid not in fragment_targets:
            fragment_targets[fid] = {
                "fragment_id": fid,
                "agent": row["agent"],
                "shared_targets": [],
            }
        fragment_targets[fid]["shared_targets"].append(row["shared_target"])

    resonance_edges = []
    for fid, info in fragment_targets.items():
        edge = create_edge(
            conn,
            from_node=fragment_id,
            to_node=fid,
            layer="L0",
            edge_type="RESONANCE",
            created_by=created_by,
            confidence=0.3,
            provenance="auto_resonance",
        )
        resonance_edges.append(
            {
                "node_id": fid,
                "edge_id": edge["id"],
                "edge_type": "RESONANCE",
                "shared_targets": info["shared_targets"],
                "shared_count": len(info["shared_targets"]),
            }
        )

    return resonance_edges


def resolve_question(
    conn: DuckDBPyConnection,
    *,
    fragment_id: str,
    resolved_by: str,
) -> dict[str, Any] | None:
    """Mark a question fragment as resolved (OHM-a5rz.12).

    Updates metadata: is_question → false, adds resolved_at timestamp.
    Only resolves fragments that currently have is_question=true in metadata.

    Returns updated node dict, or None if fragment is not a question.
    """
    import json

    node = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [fragment_id],
    ).fetchone()
    if not node:
        return None

    metadata_raw = node[1]
    metadata = json.loads(metadata_raw) if metadata_raw else {}
    if not metadata.get("is_question"):
        return None

    metadata["is_question"] = False
    now_result = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()
    metadata["resolved_at"] = str(now_result[0]) if now_result else ""

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [json.dumps(metadata), resolved_by, fragment_id],
    )
    _log_change(conn, "ohm_nodes", fragment_id, "UPDATE", agent_name=resolved_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [fragment_id]))[0]


def promote_fragment(
    conn: DuckDBPyConnection,
    *,
    fragment_id: str,
    promoted_by: str,
) -> dict[str, Any]:
    """Promote an L0 fragment to an L1 concept node (OHM-a5rz.26).

    Creates a new concept node with the fragment's label and content,
    sets metadata.promoted_from on the concept and metadata.promoted_to
    on the fragment, and creates a REFINES_FRAG edge from concept → fragment.

    Enforces ADR-022 L0→L1 promotion constraints (min_context_links ≥ 1).

    Args:
        conn: Database connection.
        fragment_id: ID of the fragment to promote.
        promoted_by: Agent performing the promotion.

    Returns:
        Dict with the new concept node and the created edge.

    Raises:
        NodeNotFoundError: If fragment doesn't exist.
        ValueError: If node is not a fragment or constraints not satisfied.
    """
    from ohm.exceptions import NodeNotFoundError, ConstraintViolationError

    frag = conn.execute(
        "SELECT id, label, content FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [fragment_id],
    ).fetchone()
    if not frag:
        raise NodeNotFoundError(f"Fragment not found: {fragment_id}")

    frag_type = conn.execute(
        "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [fragment_id],
    ).fetchone()
    if not frag_type or frag_type[0] != "fragment":
        raise ValueError(f"Node {fragment_id} is not a fragment (type={frag_type[0] if frag_type else 'N/A'})")

    # ADR-022: Validate L0→L1 promotion constraints (enforced for structural constraints)
    from ohm.graph.constraints import validate_layer_promotion

    valid, warnings, errors = validate_layer_promotion(
        fragment_id,
        "L0",
        "L1",
        conn,
        enforce=True,
    )
    if errors:
        raise ConstraintViolationError(f"Cannot promote fragment {fragment_id}: {'; '.join(errors)}")

    import json as _json

    label = frag[1]
    content = frag[2]

    concept = create_node(
        conn,
        label=label,
        node_type="concept",
        content=content,
        created_by=promoted_by,
        provenance="fragment_promotion",
        confidence=0.5,
    )

    concept_id = concept["id"]

    # Set metadata.promoted_from on the concept
    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps({"promoted_from": fragment_id}), concept_id],
    )

    edge = create_edge(
        conn,
        from_node=concept_id,
        to_node=fragment_id,
        layer="L0",
        edge_type="REFINES_FRAG",
        created_by=promoted_by,
        confidence=0.5,
        provenance="fragment_promotion",
    )

    # Update fragment metadata with promoted_to
    frag_meta_row = conn.execute(
        "SELECT metadata FROM ohm_nodes WHERE id = ?",
        [fragment_id],
    ).fetchone()
    frag_metadata = {}
    if frag_meta_row and frag_meta_row[0]:
        try:
            frag_metadata = _json.loads(frag_meta_row[0])
        except (ValueError, TypeError):
            frag_metadata = {}
    frag_metadata["promoted_to"] = concept_id
    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(frag_metadata), fragment_id],
    )

    return {
        "concept": concept,
        "edge": edge,
        "promoted_from": fragment_id,
    }


def detect_fragment_resonance(
    conn: DuckDBPyConnection,
    *,
    min_shared: int = 2,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Detect cross-agent fragment resonance (OHM-a5rz.13).

    Finds pairs of fragments from different agents that share 2+ context
    nodes (via L0 CONTEXT_OF edges). Returns resonance pairs with Jaccard
    similarity on their context node sets.

    Args:
        min_shared: Minimum shared context nodes for a resonance pair.
        limit: Max resonance pairs to return.

    Returns:
        List of resonance dicts with fragment ids, agents, shared nodes, jaccard.
    """
    rows = _rows_to_dicts(
        conn.execute(
            """SELECT f1.id AS frag_a, f1.created_by AS agent_a,
                      f2.id AS frag_b, f2.created_by AS agent_b,
                      e1.to_node AS context_node
               FROM ohm_edges e1
               JOIN ohm_edges e2 ON e1.to_node = e2.to_node
               JOIN ohm_nodes f1 ON e1.from_node = f1.id AND f1.type = 'fragment' AND f1.deleted_at IS NULL
               JOIN ohm_nodes f2 ON e2.from_node = f2.id AND f2.type = 'fragment' AND f2.deleted_at IS NULL
               WHERE e1.edge_type = 'CONTEXT_OF' AND e1.layer = 'L0' AND e1.deleted_at IS NULL
                 AND e2.edge_type = 'CONTEXT_OF' AND e2.layer = 'L0' AND e2.deleted_at IS NULL
                 AND f1.created_by != f2.created_by
                 AND f1.id < f2.id
            """,
        )
    )

    from collections import defaultdict

    pair_contexts: dict[tuple[str, str], set[str]] = defaultdict(set)
    pair_agents: dict[tuple[str, str], tuple[str, str]] = {}

    for row in rows:
        key = (row["frag_a"], row["frag_b"])
        pair_contexts[key].add(row["context_node"])
        pair_agents[key] = (row["agent_a"], row["agent_b"])

    results = []
    for (frag_a, frag_b), shared in sorted(pair_contexts.items(), key=lambda x: -len(x[1])):
        if len(shared) < min_shared:
            continue
        if len(results) >= limit:
            break

        agent_a, agent_b = pair_agents[(frag_a, frag_b)]

        ctx_a_rows = conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'CONTEXT_OF' AND layer = 'L0' AND deleted_at IS NULL",
            [frag_a],
        ).fetchall()
        ctx_a = {r[0] for r in ctx_a_rows}

        ctx_b_rows = conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'CONTEXT_OF' AND layer = 'L0' AND deleted_at IS NULL",
            [frag_b],
        ).fetchall()
        ctx_b = {r[0] for r in ctx_b_rows}

        union = ctx_a | ctx_b
        jaccard = len(shared) / len(union) if union else 0.0

        results.append(
            {
                "fragment_a": frag_a,
                "fragment_b": frag_b,
                "agent_a": agent_a,
                "agent_b": agent_b,
                "shared_context_nodes": sorted(shared),
                "shared_count": len(shared),
                "jaccard": round(jaccard, 3),
            }
        )

    return results


def reflect_challenge_to_fragments(
    conn: DuckDBPyConnection,
    challenged_edge_id: str,
    challenge_edge_id: str,
    challenged_by: str,
) -> list[dict[str, Any]]:
    """Trace a challenge back to originating L0 fragments (OHM-a5rz.15).

    When an L3/L4 edge is challenged, follow ``DERIVES_FROM`` / ``REFERENCES``
    edges backward from the claim node to find L0 ``fragment`` nodes that may
    have originated the claim. Creates lightweight L0 annotation edges
    (``type='CHALLENGED_BY'``, ``layer='L0'``) from the challenge back to
    each originating fragment so the thinking layer is aware of the challenge.

    Returns a list of fragment IDs that were annotated.
    """
    target = conn.execute(
        "SELECT from_node, layer FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [challenged_edge_id],
    ).fetchone()
    if not target:
        return []

    claim_node, layer = target
    if not layer or layer == "L0":
        return []

    fragments = conn.execute(
        """SELECT DISTINCT n.id
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.type = 'fragment' AND n.deleted_at IS NULL
           WHERE e.to_node = ?
             AND e.edge_type IN ('DERIVES_FROM', 'REFERENCES')
             AND e.deleted_at IS NULL
           LIMIT 5""",
        [claim_node],
    ).fetchall()

    results = []
    for row in fragments:
        frag_id = row[0]
        ann_id = f"backflow_{challenge_edge_id[:36]}_{frag_id[:36]}"[:80]
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence, provenance)
               VALUES (?, ?, ?, 'L0', 'CHALLENGED_BY', ?, 0.5, ?)
               ON CONFLICT (id) DO NOTHING""",
            [ann_id, claim_node, frag_id, challenged_by, f"auto: challenge backflow from {challenge_edge_id}"],
        )
        results.append({"fragment_id": frag_id})
    return results


def detect_fragment_clusters(
    conn: DuckDBPyConnection,
    *,
    min_cluster_size: int = 5,
    window_days: int = 7,
) -> list[dict[str, Any]]:
    """Detect clusters of L0 fragments sharing context nodes (OHM-a5rz.14).

    When an agent accumulates ``min_cluster_size`` or more fragments that
    share context nodes (via ``CONTEXT_OF`` edges) within ``window_days``,
    returns the cluster with a theme summary to nudge the agent toward
    synthesis.

    Returns a list of cluster dicts, each with:
    - ``agent``: the agent who owns the fragments
    - ``fragment_count``: number of fragments in the cluster
    - ``fragment_ids``: list of fragment IDs
    - ``fragment_labels``: list of fragment labels
    - ``shared_context_nodes``: context nodes shared across fragments
    - ``theme``: auto-generated theme from shared context labels
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")

    clusters: dict[str, dict] = {}

    # Find agents with fragments in the window
    fragment_counts = conn.execute(
        """SELECT created_by, COUNT(*) AS cnt
           FROM ohm_nodes
           WHERE type = 'fragment' AND deleted_at IS NULL
             AND created_at >= ?::TIMESTAMP
           GROUP BY created_by
           HAVING COUNT(*) >= ?""",
        [cutoff, min_cluster_size],
    ).fetchall()

    for row in fragment_counts:
        agent = row[0]
        # Get this agent's fragments with their context nodes
        ctx_rows = conn.execute(
            """SELECT n.id, n.label, c.to_node AS ctx_node
               FROM ohm_nodes n
               LEFT JOIN ohm_edges e ON e.from_node = n.id
                 AND e.edge_type = 'CONTEXT_OF'
                 AND e.deleted_at IS NULL
               LEFT JOIN ohm_nodes c ON c.id = e.to_node
               WHERE n.type = 'fragment'
                 AND n.deleted_at IS NULL
                 AND n.created_by = ?
                 AND n.created_at >= ?::TIMESTAMP
               ORDER BY n.created_at DESC
               LIMIT 200""",
            [agent, cutoff],
        ).fetchall()

        # Group by fragment
        frag_map: dict[str, dict] = {}
        for r in ctx_rows:
            fid, flabel, ctx_id = r[0], r[1], r[2]
            if fid not in frag_map:
                frag_map[fid] = {"label": flabel, "context": set()}
            if ctx_id:
                frag_map[fid]["context"].add(ctx_id)

        fragments = list(frag_map.items())

        # Check if enough fragments share at least one context node
        shared_ctx: set[str] = set()
        for fid, info in fragments:
            if not shared_ctx:
                shared_ctx = info["context"]
            else:
                shared_ctx &= info["context"]

        if len(fragments) >= min_cluster_size and len(shared_ctx) >= 1:
            cluster_key = agent
            clusters[cluster_key] = {
                "agent": agent,
                "fragment_count": len(fragments),
                "fragment_ids": [f[0] for f in fragments],
                "fragment_labels": [f[1]["label"] for f in fragments],
                "shared_context_nodes": sorted(shared_ctx),
                "theme": f"{len(fragments)} fragments sharing {len(shared_ctx)} context nodes",
            }

            # Compute theme from shared context node labels
            if shared_ctx:
                placeholders = ",".join(["?"] * len(shared_ctx))
                ctx_labels = conn.execute(
                    f"SELECT id, label FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                    list(shared_ctx),
                ).fetchall()
                if ctx_labels:
                    labels = [r[1] for r in ctx_labels if r[1]]
                    if labels:
                        clusters[cluster_key]["theme"] = f"You've been thinking about: {', '.join(labels[:5])}"

    return list(clusters.values())


def query_fragment_clusters(
    conn: DuckDBPyConnection,
    *,
    min_fragments: int = 3,
    min_shared_targets: int = 2,
) -> list[dict[str, Any]]:
    """Find clusters of fragments sharing CONTEXT_OF targets (OHM-a5rz.28).

    Identifies groups of 3+ fragments that share 2+ CONTEXT_OF target
    nodes. These clusters are promotion candidates — the fragments may
    be worth combining into an L1 concept.

    Uses a graph-based approach: finds fragment pairs sharing >= 2 targets,
    then groups connected components into clusters.

    Args:
        min_fragments: Minimum fragments per cluster (default 3).
        min_shared_targets: Minimum shared targets per pair (default 2).

    Returns:
        List of cluster dicts, sorted by cluster size descending.
    """
    # Step 1: Find all fragment→target pairs (CONTEXT_OF edges from fragments)
    fragment_targets = _rows_to_dicts(
        conn.execute(
            """SELECT e.from_node AS fragment_id, e.to_node AS target_id
               FROM ohm_edges e
               JOIN ohm_nodes n ON e.from_node = n.id
               WHERE e.edge_type = 'CONTEXT_OF' AND e.layer = 'L0' AND e.deleted_at IS NULL
                 AND n.type = 'fragment' AND n.deleted_at IS NULL
            """,
        )
    )

    if len(fragment_targets) < min_fragments:
        return []

    # Group targets by fragment
    from collections import defaultdict

    frag_to_targets: dict[str, set[str]] = defaultdict(set)
    for row in fragment_targets:
        frag_to_targets[row["fragment_id"]].add(row["target_id"])

    fragment_ids = list(frag_to_targets.keys())

    # Step 2: Build adjacency — edge between fragments sharing >= min_shared_targets
    adj: dict[str, set[str]] = defaultdict(set)
    for i in range(len(fragment_ids)):
        fi = fragment_ids[i]
        ti = frag_to_targets[fi]
        for j in range(i + 1, len(fragment_ids)):
            fj = fragment_ids[j]
            tj = frag_to_targets[fj]
            shared = ti & tj
            if len(shared) >= min_shared_targets:
                adj[fi].add(fj)
                adj[fj].add(fi)

    # Step 3: BFS to find connected components (clusters)
    visited: set[str] = set()
    clusters: list[list[str]] = []

    for fid in adj:
        if fid in visited:
            continue
        component: list[str] = []
        queue = [fid]
        visited.add(fid)
        while queue:
            node = queue.pop(0)
            component.append(node)
            for neighbor in adj[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(component) >= min_fragments:
            clusters.append(component)

    # Sort by cluster size descending
    clusters.sort(key=lambda c: -len(c))

    # Step 4: Build response with shared target info
    result = []
    for cluster in clusters:
        # Union of all shared targets across the cluster's internal edges
        cluster_targets: set[str] = set()
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                shared = frag_to_targets[cluster[i]] & frag_to_targets[cluster[j]]
                if len(shared) >= min_shared_targets:
                    cluster_targets |= shared

        result.append(
            {
                "cluster_size": len(cluster),
                "fragment_ids": sorted(cluster),
                "shared_target_count": len(cluster_targets),
                "shared_target_ids": sorted(cluster_targets),
            }
        )

    return result


def evict_expired_fragments(
    conn: DuckDBPyConnection,
    *,
    ttl_days: int = 30,
) -> dict[str, Any]:
    """Soft-delete expired L0 fragments (OHM-a5rz.27).

    Runs the fragment TTL eviction policy:
    - Fragments older than ``ttl_days`` (based on ``updated_at``) are candidates.
    - If the fragment was promoted (has ``metadata.promoted_to``), it is **never** evicted.
    - If the fragment has any outgoing L0 edges, its TTL is **extended** (``updated_at``
      set to ``now()``) — connected fragments are worth keeping.
    - Otherwise, the fragment is **soft-deleted** (``deleted_at`` set to ``now()``).

    This is designed to run as an hourly background job in ohmd, but can also be
    called on-demand via ``POST /admin/evict-fragments``.

    Args:
        conn: Database connection.
        ttl_days: Number of days after which an unconnected fragment expires.

    Returns:
        Dict with ``evicted`` (list of fragment ids soft-deleted),
        ``extended`` (list of fragment ids whose TTL was extended),
        ``skipped_promoted`` (list of promoted fragment ids preserved),
        and ``candidate_count`` (total candidates evaluated).
    """
    import json as _json
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT id, metadata
               FROM ohm_nodes
               WHERE type = 'fragment'
                 AND deleted_at IS NULL
                 AND updated_at < ?
               ORDER BY updated_at ASC
            """,
            [cutoff],
        )
    )

    result: dict[str, Any] = {
        "evicted": [],
        "extended": [],
        "skipped_promoted": [],
        "candidate_count": len(candidates),
    }

    for candidate in candidates:
        fid = candidate["id"]
        meta_raw = candidate["metadata"]
        meta: dict[str, Any] = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
            except (ValueError, TypeError):
                meta = {}

        # Never evict promoted fragments (OHM-a5rz.26)
        if "promoted_to" in meta:
            result["skipped_promoted"].append(fid)
            continue

        # Check for outgoing L0 edges — extends TTL if any exist
        edge_row = conn.execute(
            """SELECT COUNT(*) FROM ohm_edges
               WHERE from_node = ? AND layer = 'L0' AND deleted_at IS NULL""",
            [fid],
        ).fetchone()
        has_edges = edge_row and edge_row[0] > 0

        if has_edges:
            # Extend TTL by bumping updated_at
            conn.execute(
                "UPDATE ohm_nodes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [fid],
            )
            result["extended"].append(fid)
        else:
            # Soft-delete: no edges, not promoted, expired
            conn.execute(
                "UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                [fid],
            )
            result["evicted"].append(fid)

    return result


def update_node_hd_fingerprint(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    dim: int = 10000,
    seed: int = 42,
) -> dict[str, Any]:
    from ohm.exceptions import NodeNotFoundError
    from ohm.inference.hd import fingerprint_node
    from ohm.validation import validate_identifier, validate_hd_fingerprint

    node_id = validate_identifier(node_id, name="node_id")

    row = conn.execute(
        """SELECT id, label, type, content, tags, provenance
           FROM ohm_nodes
           WHERE id = ? AND deleted_at IS NULL""",
        [node_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Node {node_id} not found")

    nid, label, ntype, content, tags_json, provenance = row
    tags = None
    if tags_json:
        import json

        try:
            tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
        except (json.JSONDecodeError, TypeError):
            tags = None

    fp = fingerprint_node(
        label=label,
        node_type=ntype,
        content=content,
        tags=tags,
        provenance=provenance,
        dim=dim,
        seed=seed,
    )
    fp_bytes = bytes.fromhex(fp["fingerprint_hex"])
    validate_hd_fingerprint(fp_bytes, dimensions=dim)

    conn.execute(
        "UPDATE ohm_nodes SET hd_fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        [fp_bytes, node_id],
    )
    return {
        "node_id": nid,
        "label": label,
        "type": ntype,
        "fingerprint_hex": fp["fingerprint_hex"],
        "dimension": fp["dimension"],
        "seed": fp["seed"],
        "method": fp["method"],
        "stored": True,
    }


def hd_membership_search(
    conn: DuckDBPyConnection,
    query_fingerprint_hex: str,
    *,
    threshold: float = 0.65,
    limit: int = 20,
    node_type: str | None = None,
    dim: int = 10000,
) -> list[dict[str, Any]]:
    from ohm.inference.hd import hamming_similarity
    from ohm.validation import validate_hd_fingerprint

    if not query_fingerprint_hex:
        raise ValueError("query_fingerprint_hex must be non-empty")

    query_bytes = bytearray.fromhex(query_fingerprint_hex)
    validate_hd_fingerprint(bytes(query_bytes), dimensions=dim)

    conditions = ["hd_fingerprint IS NOT NULL", "deleted_at IS NULL"]
    params: list[Any] = []
    if node_type is not None:
        conditions.append("type = ?")
        params.append(node_type)
    where_sql = " AND ".join(conditions)

    rows = conn.execute(
        f"""SELECT id, label, type, confidence, hd_fingerprint
            FROM ohm_nodes
            WHERE {where_sql}""",
        params,
    ).fetchall()

    results = []
    for r in rows:
        rid, rlabel, rtype, rconf, rfp_blob = r
        if rfp_blob is None:
            continue
        candidate_bytes = bytearray(rfp_blob) if isinstance(rfp_blob, bytes) else bytearray(rfp_blob)
        if len(candidate_bytes) != len(query_bytes):
            continue
        sim = hamming_similarity(query_bytes, candidate_bytes)
        if sim >= threshold:
            results.append(
                {
                    "node_id": rid,
                    "label": rlabel,
                    "type": rtype,
                    "confidence": rconf,
                    "hd_similarity": round(sim, 4),
                }
            )
    results.sort(key=lambda x: x["hd_similarity"], reverse=True)
    return results[:limit]


def batch_update_hd_fingerprints(
    conn: DuckDBPyConnection,
    *,
    dim: int = 10000,
    seed: int = 42,
    limit: int = 1000,
) -> dict[str, Any]:
    from ohm.inference.hd import fingerprint_node
    from ohm.validation import validate_hd_fingerprint

    rows = conn.execute(
        """SELECT id, label, type, content, tags, provenance
           FROM ohm_nodes
           WHERE hd_fingerprint IS NULL AND deleted_at IS NULL
           ORDER BY confidence DESC
           LIMIT ?""",
        [limit],
    ).fetchall()

    updated = 0
    skipped = 0
    for r in rows:
        nid, label, ntype, content, tags_json, provenance = r
        tags = None
        if tags_json:
            import json

            try:
                tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
            except (json.JSONDecodeError, TypeError):
                tags = None
        try:
            fp = fingerprint_node(
                label=label,
                node_type=ntype,
                content=content,
                tags=tags,
                provenance=provenance,
                dim=dim,
                seed=seed,
            )
            fp_bytes = bytes.fromhex(fp["fingerprint_hex"])
            validate_hd_fingerprint(fp_bytes, dimensions=dim)
            conn.execute(
                "UPDATE ohm_nodes SET hd_fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [fp_bytes, nid],
            )
            updated += 1
        except Exception:
            skipped += 1

    return {
        "updated": updated,
        "skipped": skipped,
        "dimension": dim,
        "seed": seed,
        "method": "tastebud_hd_v1",
    }
