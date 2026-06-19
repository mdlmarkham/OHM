# ADR-031: Hyperdimensional Fingerprinting Prototype

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-yk7z (this ADR), OHM-wvz8.2 (persistent storage + indexing follow-up), ADR-028 (source tier — HD tier vectors), ADR-013 (VoI — HD similarity for candidate ranking)

## Context

OHM's existing semantic search uses 768-dimensional float embeddings with cosine distance. This captures meaning but not structural or type similarity — two nodes with the same `node_type` and similar labels but different semantic content are distant in embedding space, while two semantically similar but structurally unrelated nodes are close. Hyperdimensional (HD) computing offers a complementary similarity signal: 10,000-bit binary hypervectors with XOR binding and Hamming distance capture structural membership (same type, same provenance, same tag set) cheaply and approximately.

This is a compute-on-demand prototype. No DDL changes, no persistent storage, no indexing. The goal is to validate the HD model against real OHM graphs before committing to schema changes and storage.

## Decision

### 1. Pure-Python HD computing module

`src/ohm/inference/hd.py` — no numpy dependency for core operations. Numpy is only in `markov` optional deps; adding it to core for a prototype is premature. Batch optimization deferred to OHM-wvz8.2.

### 2. Hypervector parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Dimension | 10,000 bits (1,250 bytes) | Kanerva standard; better statistical properties than 4,096 |
| Representation | `bytes` (1,250 bytes) | Compact, hashable, serializable |
| Default seed | 42 | Deterministic; same input + seed = same vector |
| Version tag | `tastebud_hd_v1` | Bump invalidates all cached fingerprints |

### 3. Deterministic primitives via seeded SHA-256 counter-mode

```python
def item_memory(label: str, seed: int = 42, dim: int = 10_000) -> bytes:
    """Deterministic hypervector for a label.
    SHA-256(label || seed || counter) for each 256-bit chunk,
    concatenated to fill dim bits."""
```

Same input + seed always produces the same vector. This is critical for cache coherence across agents and sessions.

### 4. Binding and unbinding

| Operation | Function | Property |
|-----------|----------|----------|
| bind | XOR | Self-inverse, commutative, preserves distance |
| unbind | XOR (same as bind) | Self-inverse means disbind = bind |
| bundle | majority_rule | Component-wise majority vote across vectors |

### 5. Node fingerprint composition

```
node_fp = majority_rule([
    bind(type_vec, label_vec),   # type-label pair
    content_vec,                  # node content/summary
    tags_bundle,                  # majority_rule of tag vectors
    provenance_vec,               # source_tier or agent identity
])
```

Binding `type_vec ⊕ label_vec` creates a role-filler pair. Bundling with content, tags, and provenance produces a single hypervector that is similar to nodes sharing type, labels, tags, or provenance.

### 6. Text fingerprint

```python
def text_fingerprint(text: str, seed: int = 42, dim: int = 10_000) -> bytes:
    """Majority-rule bundle of token-level vectors (whitespace split)."""
    tokens = text.split()
    token_vecs = [item_memory(tok, seed, dim) for tok in tokens]
    return majority_rule(token_vecs)
```

Primitive tokenization (whitespace split, no TF-IDF weighting). Adequate for prototype; refined tokenization deferred.

### 7. Similarity search

Naive all-pairs Hamming similarity, Python-side, O(n²). Adequate for graphs under ~1,000 nodes. Indexing and persistent storage deferred to OHM-wvz8.2.

```python
def hamming_similarity(a: bytes, b: bytes) -> float:
    """1 - (hamming_distance / dim). Range [0, 1]."""
```

### 8. No DDL / no persistent BLOB column

Fingerprints are computed on demand and returned in API responses. No `hd_fingerprint BLOB` column in `ohm_nodes` — that requires a migration and DuckDB bitwise operators, deferred to OHM-wvz8.2.

## Mapping to existing concepts

| Existing concept | HD mapping |
|------------------|-----------|
| `node_type` (schema.py) | `type_vec = item_memory(node_type)` |
| `label` on nodes | `label_vec = item_memory(label)` |
| `tags` JSON array | `tags_bundle = majority_rule([item_memory(t) for t in tags])` |
| `source_tier` (ADR-028) | `provenance_vec = item_memory(source_tier)` |
| `created_by` agent | `provenance_vec = item_memory(created_by)` (alternative to tier) |
| Semantic embedding (768-dim float) | Complementary, not replaced — HD captures structural similarity, semantic captures meaning |

## Consequences

**Positive:**
- Fast approximate similarity: Hamming distance on bitvectors is cheaper than cosine on float vectors (bitwise XOR + popcount vs. dot product)
- Complementary to semantic search: HD captures structural/type similarity that embeddings miss
- Deterministic and reproducible: same input + seed = same fingerprint, enabling cross-agent cache coherence
- No schema commitment: prototype validates the model before DDL changes
- Self-inverse binding (XOR) simplifies unbinding — no separate unbind operation

**Negative:**
- All-pairs search won't scale past ~1,000 nodes without indexing; OHM-wvz8.2 will add `hd_fingerprint BLOB` column + DuckDB bitwise ops
- Primitive text tokenization (whitespace split, no TF-IDF) — crude but adequate for prototype; refined later
- Seed must be consistent across the graph; changing seed invalidates all cached fingerprints
- 10,000-bit vectors are larger than 4,096-bit alternatives but necessary for Kanerva statistical properties
- No persistence — fingerprints recomputed on each call until wvz8.2 lands

## Alternatives considered

- **Store fingerprints in DuckDB BLOB column from the start** — rejected: premature commitment; prototype validates the HD model before schema changes. OHM-wvz8.2 adds persistence after validation.
- **Use numpy for batch operations** — rejected for prototype: numpy only in `markov` optional deps, not core. Added later for wvz8.2 scale-up.
- **Use 4,096-dim ±1 hypervectors (tastebud reference)** — rejected: 10,000 bits is Kanerva standard with better statistical properties (lower false-positive rate on similarity at the cost of 2.4× storage per vector).

## References

- Kanerva, P. — *Hyperdimensional Computing: An Introduction to Computing in Distributed Representation with High-Dimensional Random Vectors* (2009)
- ADR-028 — Source Tier Architecture (provenance_vec maps to source_tier)
- ADR-013 — Value of Information (HD similarity for VoI candidate ranking, future)
- OHM-wvz8.2 — Persistent HD storage + DuckDB bitwise indexing (follow-up)
