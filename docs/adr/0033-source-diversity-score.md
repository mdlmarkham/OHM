# ADR-033: Source Diversity Score — Independence-Weighted Shannon Entropy

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-qi6r (this ADR), ADR-028 (source tier ceilings), ADR-029 (consensus-only detection), ADR-030 (oppositional review)

## Context

The Cornell UGC poisoning paper (arxiv 2605.24245) demonstrated that deep-research agents are poisoned when many user-generated sources agree — recursive agreement loops compound into false confidence. Hillman's truth-vs-consensus framing identifies this as peer-review capture: institutional consensus is an AND-gate on independent verifications, not an OR-gate on popularity. "Many agree" is not the same as "independently verified."

ADR-028 added `source_tier` with confidence ceilings to distinguish source quality. ADR-029 detects consensus-only support (SUPPORTS edges with no recorded outcomes). ADR-030 flags homogeneous CAUSES clusters by `source_tier` and `agent_authorship`. But none of these measure whether the *sources themselves* are independent. Ten SUPPORTS edges from the same institution are not ten independent verifications — they are one institutional position repeated ten times.

OHM needs an independence-weighted diversity metric that distinguishes "many agree" from "independently verified." The metric must annotate, not block — low diversity is a signal for oppositional review (ADR-030), not a write gate.

## Decision

### 1. Three new columns on `ohm_nodes`

| Column | Type | Nullable | Default | Purpose |
|--------|------|----------|---------|---------|
| `source_author` | VARCHAR | Yes | NULL | Person or agent who authored the source |
| `source_institution` | VARCHAR | Yes | NULL | Organization or institution behind the source |
| `data_origin` | VARCHAR | Yes | NULL | Provenance category of the data |

All three are nullable with NULL defaults — existing rows and callers are unaffected.

```sql
ALTER TABLE ohm_nodes ADD COLUMN source_author VARCHAR;
ALTER TABLE ohm_nodes ADD COLUMN source_institution VARCHAR;
ALTER TABLE ohm_nodes ADD COLUMN data_origin VARCHAR;
```

### 2. Valid data origins

```python
VALID_DATA_ORIGINS = frozenset({
    "ugc",              # User-generated content (Reddit, forums, social media)
    "peer_reviewed",    # Journal articles, conference papers
    "government",       # Official government data/reports
    "news_wire",        # Wire services, established news outlets
    "sensor",           # Instrument readings, IoT, SCADA
    "agent_synthesis",  # Agent-derived claim (no external source)
    "expert",           # Domain expert opinion (non-peer-reviewed)
    "unknown",          # Origin not yet classified
})
```

`validate_data_origin(value)` in `src/ohm/validation.py` raises `ValueError` for values not in `VALID_DATA_ORIGINS`. Follows the same pattern as `validate_edge_type` / `validate_node_type`.

### 3. Schema version

`SCHEMA_VERSION = "0.32.0"` — idempotent ALTER TABLE migration in `src/ohm/schema.py`. No backfill; existing rows default to NULL on all three columns.

### 4. Source diversity score (computed-on-read)

`source_diversity_score(conn, node_id, *, depth=3)` in `src/ohm/graph/queries/__init__.py` walks CAUSES / SUPPORTS / EXPECTS / PREDICTS edges up to `depth=3` from the target node, collects the `source_author`, `source_institution`, and `data_origin` of all evidence nodes, and computes a weighted normalized Shannon entropy:

```
score = 0.4 * H(author) + 0.4 * H(institution) + 0.2 * H(origin)
```

Where `H` is normalized Shannon entropy:

```
H(X) = -Σ pᵢ log₂(pᵢ) / log₂(n)    where n = number of distinct categories
```

- `H = 0.0` → all evidence from a single author/institution/origin (homogeneous)
- `H = 1.0` → maximum diversity (uniform distribution across categories)
- When `source_author` is NULL, falls back to `created_by` on the node
- When `source_institution` is NULL, that dimension contributes 0 (not penalized)
- When `data_origin` is NULL, falls back to `"unknown"`
- If no evidence nodes found (depth=3 walk returns empty), returns `None`

The score is **computed-on-read**, not cached as a column. Evidence changes as new SUPPORTS/CAUSES edges are added; a cached column would go stale.

### 5. Annotation, not enforcement

The score annotates synthesis responses. `POST /agent/synthesis` and `Graph.synthesize()` include `source_diversity_score` in the response payload. Low diversity does not block writes — it triggers oppositional review (ADR-030) and surfaces in heartbeat nudges (ADR-029).

### 6. Four-layer plumbing

| Layer | File | Change |
|-------|------|--------|
| queries | `src/ohm/graph/queries/__init__.py` | `source_diversity_score(conn, node_id, depth=3)` + `create_node` accepts `source_author`, `source_institution`, `data_origin` |
| store | `src/ohm/store.py` | `OhmStore.create_node` accepts the 3 new fields |
| SDK | `src/ohm/sdk.py` | `Graph.create_node` passes through; `Graph.source_diversity(node_id)` wrapper |
| server | `src/ohm/server.py` | `POST /nodes` and `POST /agent/synthesis` accept + return the fields |

All three new fields default to `None` in every layer — backward compatible.

## Mapping to existing concepts

| Existing concept | Relationship |
|------------------|-------------|
| `source_tier` (ADR-028) | Orthogonal — tier is quality ceiling; diversity is independence. A `verified` tier claim from a single institution has high tier but low diversity. |
| `created_by` (ADR-003) | Fallback — `source_author` is preferred; `created_by` used when NULL. `created_by` is the agent who wrote the edge; `source_author` is who authored the underlying source. |
| Consensus-only detection (ADR-029) | Input — `source_diversity_score` gives ADR-029 a quantitative basis for "how homogeneous is the support?" beyond the binary consensus-only flag. |
| Oppositional review (ADR-030) | Trigger — ADR-030 Phase 2 replaces `source_tier` + `agent_authorship` homogeneity with `source_diversity_score` as the primary detection signal. |
| `VALID_DATA_ORIGINS` | Extends the graduated enforcement model (ADR-006/007) — `data_origin` follows the same advisory → lenient → strict lifecycle. |

## Consequences

**Positive:**
- Detects homogeneous UGC citation rings — all same author/institution/origin → score near 0.0
- Distinguishes "many agree" from "independently verified" — the AND-gate from Hillman
- Quantitative input to oppositional review (ADR-030 Phase 2) and consensus detection (ADR-029)
- Shannon entropy normalized by max possible gives 0–1 range regardless of evidence count
- Rare sources weighted more than common ones (Shannon property) — better for detecting capture than counting

**Negative:**
- Requires agents to populate `source_author` and `source_institution` for full utility; falls back to `created_by` when NULL, which conflates writing agent with source author
- Computed-on-read cost: O(evidence_nodes × depth) per query. Acceptable at OHM scale (hundreds of nodes, depth ≤ 3) but not free
- `data_origin` classification is a judgment call — agents may disagree on `ugc` vs `expert`. Challenge semantics (ADR-003) apply
- The 0.4/0.4/0.2 weighting is a heuristic, not derived from theory. May need tuning per domain

## Alternatives considered

- **Simpson's diversity index instead of Shannon** — rejected. Simpson weights dominant categories more; Shannon weights rare categories more. For detecting capture (a single institution dominating), Shannon's sensitivity to rare sources is the desired property.
- **Cached `diversity_score` column on `ohm_nodes`** — rejected. Score changes as evidence is added; a cached column goes stale and requires re-evaluation triggers. Compute-on-read is correct; cache later if profiling shows a bottleneck.
- **Block low-diversity writes** — rejected. Too aggressive for Phase 2. Annotate first (this ADR), let oppositional review (ADR-030) and consensus nudges (ADR-029) handle the behavioral path. Blocking is a Phase 3+ decision.
- **Gini-Simpson or Berger-Parker dominance** — rejected. Gini-Simpson is a probability-of-difference measure (equivalent to 1 − Simpson); Berger-Parker measures single-dominance only. Shannon captures both dominance and evenness in one metric.

## References

- ADR-028 — Source Tier Architecture and Confidence Ceilings (orthogonal quality dimension)
- ADR-029 — Consensus-Only Confidence Ceilings (binary consensus detection; this ADR adds quantitative diversity)
- ADR-030 — Oppositional Review Pipeline (Phase 2 consumes `source_diversity_score`)
- ADR-003 — Agent-Owned Edges (challenge semantics apply to `data_origin` disagreements)
- ADR-006 — Advisory Schema with Graduated Enforcement (`VALID_DATA_ORIGINS` follows this model)
- Cornell UGC poisoning paper (arxiv 2605.24245) — recursive agreement loops compound into false confidence
- Hillman truth-vs-consensus — institutional consensus is an AND-gate on independent verifications
