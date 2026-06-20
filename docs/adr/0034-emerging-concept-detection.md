# ADR-034: Emerging Concept Detection via HD Fingerprint Residual Mass

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-tlqz (this ADR), ADR-031 (HD fingerprint prototype), ADR-032 (HD membership layer / persistent BLOBs), ADR-033 (source diversity score)

## Context

OHM agents continuously create nodes — concepts, patterns, ideas, fragments — that may or may not correspond to known concepts in the graph. When a node is structurally novel (its HD fingerprint is distant from all existing concept fingerprints), it may represent an emerging concept: something real that the graph has not yet named or connected. Today, such nodes sit inert until a human or agent happens to notice them. There is no automated signal that says "this node is unlike anything we already know."

ADR-031 introduced 10,000-bit hyperdimensional fingerprints with Hamming similarity. ADR-032 persisted them as `hd_fingerprint BLOB` on `ohm_nodes`. The residual mass — the portion of a node's fingerprint that cannot be explained by any existing concept — is a natural novelty detector. A node with high residual mass is structurally dissimilar from all known concepts; if it also has enough evidence (observations, supporting edges), it is a candidate for naming and promotion.

The key question: what threshold distinguishes "genuinely novel" from "randomly dissimilar"? For 10,000-bit hypervectors, the expected Hamming similarity between two random vectors is ~0.5 (binomial distribution). A residual mass above 0.5 means the node is less similar to its closest concept than a random vector would be — strong evidence of structural novelty.

## Decision

### 1. Residual mass formula

```
residual_mass = 1 - max_similarity_to_concepts
```

Where `max_similarity_to_concepts` is the highest Hamming similarity between the node's HD fingerprint and all other stored fingerprints in `ohm_nodes`. Computed by `compute_residual_mass()` in `src/ohm/graph/method.py:4243`.

- `residual_mass = 1.0` → no other nodes have stored fingerprints (orphan)
- `residual_mass ≈ 0.5` → as similar to closest concept as a random vector
- `residual_mass > 0.5` → less similar than random → structurally novel

### 2. Stability = residual mass when evidence is sufficient

```
stability = residual_mass   if total_observations >= 3
stability = 0.0             if total_observations < 3
```

`total_observations = evidence_count + observation_count`, where evidence counts CAUSES/SUPPORTS/EXPECTS/PREDICTS edges targeting the node, and observation counts rows in `ohm_observations`. A node with high residual mass but zero evidence is not an emerging concept — it is noise. The ≥3 observation gate ensures only nodes with accumulated support are considered stable.

Computed by `compute_emerging_concept_stability()` in `src/ohm/graph/methods.py:4320`.

### 3. Stability threshold: 0.45

`promote_emerging_concept()` gates on `stability >= 0.45`. This is below the random-similarity floor of ~0.5 for 10,000-bit vectors, providing a conservative boundary: a node must be *at least* as dissimilar as a random vector to qualify for promotion, with a small margin for fingerprint noise.

The `detect_unknown_ingredients()` scan uses `stability_threshold=0.7` and `residual_mass_threshold=0.5` as defaults — stricter than the promotion gate, filtering for high-confidence emerging concepts.

### 4. `emerging_concept_score` JSON column on `ohm_nodes`

```sql
ALTER TABLE ohm_nodes ADD COLUMN IF NOT EXISTS emerging_concept_score JSON;
```

Schema version `0.33.0`. Nullable, default NULL. Stored by `update_emerging_concept_score()` in `src/ohm/graph/methods.py:4456`.

JSON structure:

```json
{
  "residual_mass": 0.72,
  "stability": 0.72,
  "observations_count": 5,
  "max_similarity": 0.28,
  "closest_concept": "concept_abc123",
  "status": "naming_candidate",
  "last_updated": "2026-06-19T14:30:00"
}
```

Promoted nodes additionally include `promoted_by` and `promoted_at`.

### 5. Status lifecycle

```python
VALID_EMERGING_CONCEPT_STATUSES = frozenset({"unnamed", "naming_candidate", "named", "rejected"})
```

Defined in `src/ohm/graph/schema.py:322`. Validated by `validate_emerging_concept_status()` in `src/ohm/framework/validation.py:119`.

| Status | Condition | Meaning |
|--------|-----------|---------|
| `unnamed` | stability < 0.7 or residual_mass < 0.5 | Novel but insufficient evidence or borderline similarity |
| `naming_candidate` | stability ≥ 0.7 AND residual_mass ≥ 0.5 | Strongly novel with sufficient evidence — ready for naming |
| `named` | After `promote_emerging_concept()` succeeds | Concept has been given a meaningful label |
| `rejected` | (manual) | Agent or human determined this is not a real concept |

Status transitions: `unnamed → naming_candidate → named` or `unnamed/naming_candidate → rejected`.

### 6. `promote_emerging_concept()` — gated naming

`promote_emerging_concept(conn, node_id, new_label, promoted_by)` in `src/ohm/graph/methods.py:4494`:

1. Validates `new_label` is non-empty and ≤ 500 chars
2. Computes stability; raises `ValueError` if `stability < 0.45`
3. Updates `ohm_nodes.label` to `new_label`
4. Sets `emerging_concept_score.status = "named"` with `promoted_by` and `promoted_at`

The 0.45 gate is lower than the `naming_candidate` threshold (0.7) to allow promotion of borderline concepts that an agent has judged worth naming — the agent's judgment overrides the statistical threshold, but not below the random-similarity floor.

### 7. `detect_unknown_ingredients()` — graph-wide scan

`detect_unknown_ingredients(conn, residual_mass_threshold=0.5, stability_threshold=0.7, min_observations=3, limit=20)` in `src/ohm/graph/methods.py:4365`:

Scans nodes of type `concept`, `pattern`, `idea`, `fragment`. For each, computes residual mass against all stored fingerprints, checks evidence count, and returns nodes exceeding both thresholds, sorted by residual mass descending.

### 8. SDK surface

| SDK method | File | Wraps |
|------------|------|-------|
| `Graph.detect_emerging_concepts()` | `src/ohm/framework/sdk.py:778` | `detect_unknown_ingredients()` |
| `Graph.compute_residual_mass(node_id)` | `src/ohm/framework/sdk.py:802` | `compute_residual_mass()` |
| `Graph.update_emerging_concept_score(node_id)` | `src/ohm/framework/sdk.py:808` | `update_emerging_concept_score()` |
| `Graph.name_emerging_concept(node_id, new_label)` | `src/ohm/framework/sdk.py:814` | `promote_emerging_concept()` |

## Mapping to existing concepts

| Existing concept | Emerging concept mapping |
|------------------|--------------------------|
| `hd_fingerprint BLOB` (ADR-032) | Source of similarity — residual mass is computed against stored BLOBs |
| `tastebud_hd_v1` version tag (ADR-031) | Fingerprint version coupling — tag change invalidates all `emerging_concept_score` values |
| `source_tier` (ADR-028) | Orthogonal — tier is source quality; residual mass is structural novelty |
| `source_diversity_score` (ADR-033) | Complementary — diversity measures independence of support; residual mass measures structural uniqueness |
| `VALID_EMERGING_CONCEPT_STATUSES` | Follows graduated enforcement model (ADR-006/007) — frozenset validation |
| `ohm_observations` | Evidence gate — `total_observations >= 3` required for stability > 0 |

## Consequences

**Positive:**
- Automated detection of structurally novel nodes — no manual scanning required
- Residual mass is grounded in HD fingerprint theory (random similarity floor ~0.5 for 10K-bit vectors)
- Evidence gate (≥3 observations) prevents noise from being promoted
- Status lifecycle gives agents a clear workflow: detect → score → name → verify
- JSON column avoids DDL churn for score schema evolution
- Promotion gate (0.45) is below random floor, allowing agent judgment to override statistics for borderline cases

**Negative:**
- O(n²) fingerprint comparison in `detect_unknown_ingredients()` — scans all nodes against all stored BLOBs. Acceptable at current scale (~700 nodes); needs indexing for 5K+ (deferred to HNSW-for-Hamming work)
- `emerging_concept_score` is a JSON column — not queryable by DuckDB SQL without json extension functions. Score fields must be read in Python
- Residual mass depends on fingerprint quality — primitive tokenization (whitespace split per ADR-031) may produce noisy fingerprints, inflating residual mass for poorly tokenized labels
- Status transitions are not enforced as state machine — `update_emerging_concept_score` sets `unnamed` or `naming_candidate` based on current thresholds, but `rejected` must be set manually
- Seed/version coupling: if `tastebud_hd_v1` changes, all `emerging_concept_score` values become stale and must be recomputed

## Alternatives considered

- **Clustering-based novelty (k-means on embeddings)** — rejected. Requires choosing k, sensitive to initialization, and cosine similarity on 768-dim float embeddings captures semantic similarity but not structural/type membership. HD residual mass is a single scalar with a clear statistical interpretation.
- **Fixed residual mass threshold with no evidence gate** — rejected. Without the ≥3 observation gate, any orphan node (no stored fingerprints in graph) would score residual_mass = 1.0 and be flagged as emerging. The evidence gate ensures only nodes with accumulated support are considered.
- **Cached `residual_mass` FLOAT column instead of JSON** — rejected. The score carries multiple fields (residual_mass, stability, status, closest_concept, timestamps) that evolve together. A single FLOAT column would lose the status lifecycle and metadata. JSON is more flexible and avoids repeated DDL for score schema changes.
