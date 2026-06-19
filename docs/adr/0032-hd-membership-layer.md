# ADR-032: HD Membership Layer — Persistent Fingerprints in DuckDB

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-wvz8.2 (this ADR), OHM-yk7z (prototype), ADR-031 (HD fingerprint prototype), ADR-028 (source tier — provenance_vec), ADR-0007 (schema evolution governance)

## Context

ADR-031 validated hyperdimensional fingerprinting as a complementary similarity signal to 768-dim float embeddings. The prototype computes 10,000-bit hypervectors on demand — every `hd_membership_search` call recomputes all node fingerprints from scratch, then runs all-pairs Hamming distance in Python. This is O(n) per query with recomputation overhead, and fingerprints are lost on DB restart.

For OHM's growing graph (675+ nodes, projected 5K+), recomputing fingerprints per query is wasteful and slow. The fingerprints are deterministic (same input + seed = same vector per ADR-031), so storing them eliminates redundant work. DuckDB lacks native XOR/popcount on BLOB, so Hamming distance must remain Python-side, but fetching pre-computed BLOBs is far cheaper than recomputing them.

This ADR adds persistent HD fingerprint storage to `ohm_nodes` and the query plumbing to use stored fingerprints for membership search.

## Decision

### 1. Schema: `hd_fingerprint BLOB` column on `ohm_nodes`

Nullable, default NULL. Stores the 1,250-byte (10,000-bit) binary hypervector per ADR-031.

```sql
ALTER TABLE ohm_nodes ADD COLUMN hd_fingerprint BLOB DEFAULT NULL;
```

### 2. Schema version bump: `0.31.0`

Idempotent `ALTER TABLE IF NOT EXISTS` migration in `src/ohm/schema.py:initialize_schema`. Follows ADR-0007 schema evolution governance — version recorded in `ohm_meta`.

### 3. Partial index for efficient filtering

```sql
CREATE INDEX IF NOT EXISTS idx_nodes_hd_fingerprint
    ON ohm_nodes(node_id) WHERE hd_fingerprint IS NOT NULL;
```

Only rows with stored fingerprints are indexed. Nodes without fingerprints (the default) are excluded, keeping the index small and focused.

### 4. Validation: `VALID_HD_DIMENSIONS` and `validate_hd_fingerprint`

```python
# src/ohm/schema.py
VALID_HD_DIMENSIONS = frozenset({10_000})

# src/ohm/validation.py
def validate_hd_fingerprint(fp: bytes, dim: int = 10_000) -> None:
    expected_len = (dim + 7) // 8  # 10,000 bits → 1,250 bytes
    if len(fp) != expected_len:
        raise ValueError(
            f"hd_fingerprint length {len(fp)} != expected {expected_len} "
            f"bytes for dim={dim}"
        )
```

Byte-length check: `(dim + 7) // 8` accounts for partial bytes. Currently only `dim=10_000` is valid; the frozenset allows future dimensions (e.g., 4,096 or 20,000) without schema changes.

### 5. Opt-in storage — no auto-populate on `create_node`

Fingerprints are NOT computed or stored during `create_node`. They are set explicitly via:

```python
# src/ohm/queries/__init__.py
def update_node_hd_fingerprint(
    conn: DuckDBPyConnection,
    node_id: str,
    hd_fingerprint: bytes,
) -> dict:
```

Rationale: computing a fingerprint requires assembling node attributes (type, label, tags, provenance) and running the HD primitives. Auto-populating would slow every `create_node` call for a feature most callers don't use yet. Opt-in keeps the hot path clean.

### 6. `hd_membership_search` — fetch stored BLOBs, compute Hamming in Python

```python
def hd_membership_search(
    conn: DuckDBPyConnection,
    query_fingerprint: bytes,
    dim: int = 10_000,
    top_k: int = 10,
    min_similarity: float = 0.0,
) -> list[dict]:
    """Fetch all stored hd_fingerprint BLOBs, compute Hamming similarity
    in Python, return top_k results above min_similarity."""
```

DuckDB does not support XOR or bit_count on BLOB type. All Hamming distance computation is Python-side. The query fetches `(node_id, hd_fingerprint)` for rows where `hd_fingerprint IS NOT NULL`, then computes similarity in a loop. This is O(n) for nodes with stored fingerprints — no recomputation overhead.

### 7. `batch_update_hd_fingerprints` — bulk migration

```python
def batch_update_hd_fingerprints(
    conn: DuckDBPyConnection,
    fingerprints: dict[str, bytes],
    dim: int = 10_000,
) -> int:
    """Bulk-set hd_fingerprint for multiple nodes.
    Returns count of updated rows."""
```

For migrating existing nodes that lack fingerprints. Accepts a `{node_id: bytes}` mapping, validates each fingerprint, and writes in a single transaction.

### 8. Complementary to `semantic_search`, not replacing it

| Search method | Distance metric | Vector type | Captures |
|---------------|----------------|-------------|----------|
| `semantic_search` | Cosine | FLOAT[768] embedding | Meaning, semantic similarity |
| `hd_membership_search` | Hamming | BLOB (10,000-bit) | Structural/type membership |

Both return results independently. Future: `hybrid_search` combining cosine + Hamming with configurable weights (deferred).

## Mapping to existing concepts

| Existing concept | HD membership layer mapping |
|------------------|---------------------------|
| `node_type` (schema.py) | Encoded in fingerprint via `bind(type_vec, label_vec)` per ADR-031 |
| `tags` JSON array | Encoded via `majority_rule([item_memory(t) for t in tags])` |
| `source_tier` (ADR-028) | Encoded as `provenance_vec = item_memory(source_tier)` |
| `embedding` FLOAT[768] | Coexists — `hd_fingerprint BLOB` is a separate column, not a replacement |
| `tastebud_hd_v1` version tag (ADR-031) | Stored fingerprints are tied to this version; tag bump invalidates all stored BLOBs |

## Consequences

**Positive:**
- Fingerprints survive DB restarts — no recomputation on every query (unlike ADR-031 prototype)
- Search is O(n) fetch + compare for nodes with stored fingerprints, vs. O(n) with recomputation overhead in the prototype
- Partial index keeps filtering efficient — only nodes with fingerprints are scanned
- Opt-in storage keeps `create_node` fast for callers that don't use HD
- Batch migration enables one-time backfill of existing nodes
- Deterministic fingerprints (ADR-031) mean stored values are stable — no drift

**Negative:**
- Still brute-force Python-side search — no HNSW-like index for Hamming distance (DuckDB VSS extension only supports cosine/euclidean on float vectors)
- 1,250 bytes per node storage overhead (~1.25 KB/node) — acceptable at current scale, grows linearly
- Seed/version coupling: if `tastebud_hd_v1` tag changes or seed changes, all stored fingerprints must be recomputed via `batch_update_hd_fingerprints`
- No SQL-level XOR/popcount on BLOB — all Hamming distance is Python-side, limiting throughput for very large graphs

## Alternatives considered

- **UTINYINT[1250] array for SQL-level XOR** — rejected: DuckDB doesn't support element-wise XOR on arrays. Would require UDF or extension, adding complexity for marginal benefit at current scale.
- **BIT type** — rejected: DuckDB's BIT type has no XOR operator and no variable-length support. BLOB is the only viable binary container.
- **Auto-populate on `create_node`** — rejected: would slow every node creation for a feature most callers don't use. Opt-in via `update_node_hd_fingerprint` is cleaner and follows the pattern of other nullable columns (e.g., `source_tier` in ADR-028).
- **Store as hex string (VARCHAR)** — rejected: hex encoding doubles storage (2,500 chars vs. 1,250 bytes BLOB) and requires encode/decode on every read. BLOB is the natural representation for binary data.

## References

- ADR-031 — Hyperdimensional Fingerprinting Prototype (compute-on-demand, no persistence)
- ADR-028 — Source Tier Architecture (provenance_vec maps to source_tier)
- ADR-0007 — Schema Evolution and Type Governance (version bump + idempotent migration)
- Kanerva, P. — *Hyperdimensional Computing* (2009)
