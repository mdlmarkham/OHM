# ADR-036: Ripen-Then-Decide Triage for Suggestions

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-xtzk (this ADR), ADR-013 (VoI — suggestions feed VoI candidates), ADR-028 (source tier — confidence ceiling on promoted edges), ADR-029 (consensus-only — promoted edges subject to consensus nudge)

## Context

Agents and substrate methods (`suggest_causes`, `suggest_connections`, VoI) produce candidate edges that should not be written directly to `ohm_edges`. Writing immediately would pollute the canonical graph with unverified suggestions, inflate confidence through recursive agreement (ADR-029), and create dead-end edges that bypass the cross-link requirement (ADR-018). But discarding suggestions loses potentially valuable signal — a suggestion that three independent agents independently propose is stronger than one a single agent proposes once.

The gap: OHM has no staging area for candidate edges and no automated triage that accumulates evidence over time. Every suggestion is either accepted immediately (risky) or lost (wasteful). A ripening model — where suggestions grow in credibility with age and corroboration — bridges the gap: suggestions persist until they either accumulate enough evidence to promote or age out as stale.

## Decision

### 1. `ohm_suggestions` table — staging area for candidate edges

```sql
CREATE TABLE IF NOT EXISTS ohm_suggestions (
    id               VARCHAR PRIMARY KEY,
    suggestion_type  VARCHAR NOT NULL,        -- 'edge' | 'node_link'
    from_node        VARCHAR,
    to_node          VARCHAR,
    target_node      VARCHAR,
    suggested_edge_type VARCHAR,
    suggested_layer  VARCHAR,
    confidence       FLOAT DEFAULT 0.5,
    status           VARCHAR DEFAULT 'ripe',   -- ripe | promoted | expired | rejected
    suggested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    evidence_count   INTEGER DEFAULT 1,
    ripeness_score   FLOAT DEFAULT 0.0,
    last_ripened_at  TIMESTAMP,
    source_method    VARCHAR,
    source_agent     VARCHAR,
    metadata         JSON,
    reviewed_by      VARCHAR,
    reviewed_at      TIMESTAMP,
    review_notes     TEXT,
    created_by       VARCHAR NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at       TIMESTAMP
);
```

Validated enums in `src/ohm/graph/schema.py:333`:

```python
VALID_SUGGESTION_TYPES = frozenset({"edge", "node_link"})
VALID_SUGGESTION_STATUSES = frozenset({"ripe", "promoted", "expired", "rejected"})
```

### 2. Lifecycle: ripe → promoted | expired | rejected

| Status | Meaning | Transition trigger |
|--------|---------|--------------------|
| `ripe` | Active candidate, accumulating evidence | Initial state on `create_suggestion` |
| `promoted` | Accepted — real edge created in `ohm_edges` | `promote_suggestion()` or auto-promote when `ripeness_score ≥ threshold` |
| `expired` | Stale — older than `max_age_days` without reaching threshold | `ripen_then_decide()` auto-expiry |
| `rejected` | Manually dismissed | `reject_suggestion()` with optional `review_notes` |

Transitions are one-way: `ripe → {promoted, expired, rejected}`. No backward transitions. A promoted suggestion cannot return to ripe.

### 3. Ripeness formula — inverted decay (ripeness GROWS with age)

```python
def compute_ripeness(suggestion, *, now, ripen_half_life_days=7.0, evidence_threshold=2) -> float:
    age_days = (now - suggested_at).total_seconds() / 86400.0
    time_factor = 1.0 - 0.5 ** (age_days / ripen_half_life_days)
    evidence_factor = min(1.0, evidence_count / evidence_threshold)
    confidence_factor = confidence  # 0–1
    return time_factor * evidence_factor * confidence_factor
```

Three multiplicative factors, each in [0, 1]:

| Factor | Formula | Rationale |
|--------|---------|----------|
| `time_factor` | `1 − 0.5^(age / half_life)` | Inverted half-life: ripeness asymptotes to 1.0 as the suggestion ages. A 7-day-old suggestion at default half-life = 0.5; 14 days = 0.75; 21 days = 0.875. |
| `evidence_factor` | `min(1.0, evidence_count / threshold)` | Saturates at 1.0 when `evidence_count ≥ threshold` (default 2). Two independent suggestions = full evidence. |
| `confidence_factor` | `confidence` | The creating agent's confidence in the suggestion. High-confidence suggestions ripen faster. |

The product means all three dimensions must be non-trivial for promotion: a brand-new suggestion (time_factor ≈ 0) won't promote regardless of evidence; a low-confidence suggestion won't promote regardless of age; a single-source suggestion won't promote regardless of confidence.

### 4. `ripen_then_decide` — batch triage

`ripen_then_decide(conn, *, dry_run=False, max_age_days=30, ripeness_threshold=0.7, ripen_half_life_days=7.0, evidence_threshold=2)` in `src/ohm/graph/methods.py:4588`:

1. **Ripen all** — compute `ripeness_score` for every `status='ripe'` suggestion, persist to `ripeness_score` column and update `last_ripened_at`.
2. **Auto-promote** — suggestions with `ripeness_score ≥ ripeness_threshold` (default 0.7) are promoted via `promote_suggestion()`, which creates a real edge in `ohm_edges` and sets `status='promoted'`.
3. **Auto-expire** — suggestions older than `max_age_days` (default 30) that haven't reached threshold are set to `status='expired'`.

Returns `{"ripened": N, "promoted": M, "expired": K, "dry_run": bool}`.

### 5. Duplicate prevention — evidence accumulation

`create_suggestion()` in `src/ohm/graph/queries/__init__.py:5305` checks for an existing ripe suggestion with the same `(from_node, to_node, target_node)` triple using `IS NOT DISTINCT FROM` (handles NULLs). On duplicate:

- **Increment `evidence_count`** instead of inserting a new row.
- Update `last_ripened_at` to current timestamp.
- Return the existing suggestion (not a new one).

This means three agents independently suggesting the same edge produce one suggestion with `evidence_count=3`, not three separate suggestions. The evidence_factor saturates at `evidence_threshold` (default 2), so the third agent adds no ripeness but does add audit trail.

### 6. Promoted suggestions create real edges

`promote_suggestion()` in `src/ohm/graph/queries/__init__.py:5382`:

- Validates suggestion exists and `status='ripe'`.
- For `suggestion_type='edge'`: creates a real edge in `ohm_edges` with `from_node`, `to_node`, `suggested_edge_type`, `layer`, `confidence` from the suggestion. The `created_by` on the new edge is set to `promoted_by` (the agent or `system` that triggered promotion).
- Sets `status='promoted'`, `reviewed_by`, `reviewed_at` on the suggestion.
- The suggestion row is retained (not deleted) for audit trail.

### 7. SDK surface

| Method | Location | Description |
|--------|----------|-------------|
| `Graph.create_suggestion(**kwargs)` | `src/ohm/framework/sdk.py:862` | Create or increment evidence on a suggestion |
| `Graph.query_suggestions(**kwargs)` | `src/ohm/framework/sdk.py:869` | Query by status, method, target, min_ripeness |
| `Graph.promote_suggestion(id, *, edge_layer)` | `src/ohm/framework/sdk.py:874` | Manually promote a ripe suggestion |
| `Graph.reject_suggestion(id, *, notes)` | `src/ohm/framework/sdk.py:879` | Manually reject a suggestion |
| `Graph.ripen_suggestions(*, dry_run, max_age_days, ripeness_threshold)` | `src/ohm/framework/sdk.py:884` | Batch ripen + auto-promote + auto-expire |

## Mapping to existing concepts

| Existing concept | Relationship |
|------------------|-------------|
| `suggest_causes()` (ADR-013) | Output channel → writes to `ohm_suggestions` instead of directly to `ohm_edges` |
| `suggest_connections()` | Output channel → same pattern |
| VoI `compute_voi()` (ADR-013) | High-VoI candidates become suggestions; promoted suggestions feed VoI back |
| Source tier (ADR-028) | Promoted edges inherit `source_tier` from the promoting agent; ceiling enforcement applies |
| Consensus-only (ADR-029) | Promoted edges are subject to consensus-only detection like any other edge |
| Cross-link requirement (ADR-018) | Promoted edges reference existing nodes (suggestions require `from_node`/`to_node` to exist) |
| Verification decay (ADR-018.3) | Promoted edges decay per 30d/365d half-life split based on outcomes |

## Consequences

**Positive:**
- The canonical graph (`ohm_edges`) stays clean — only promoted, reviewed edges enter it.
- Evidence accumulates: three independent agents suggesting the same edge produces one strong suggestion, not three weak ones.
- Ripening model respects time: a suggestion that has survived scrutiny for 14 days is more credible than one created 5 minutes ago.
- Auto-promote and auto-expire reduce manual triage burden — operators only review edge cases near the threshold.
- Full audit trail: suggestion rows are never deleted, only status-transitioned.
- Duplicate prevention prevents suggestion spam from inflating the table.

**Negative:**
- Suggestions table grows unbounded — expired/rejected rows accumulate. Mitigation: `deleted_at` soft-delete + periodic vacuum.
- The ripeness formula is multiplicative — if any factor is near 0, ripeness is near 0. A high-confidence suggestion from a single source won't promote until a second source corroborates (evidence_factor < 1). This is intentional but may surprise operators expecting fast promotion.
- Auto-promotion creates edges with `created_by='system'`, not the original suggesting agent. Attribution is preserved in the suggestion row but not on the edge itself.
- The `(from_node, to_node, target_node)` duplicate key is coarse — two suggestions with different `suggested_edge_type` but the same nodes/target would be deduplicated. This is acceptable because the most common case is suggesting the same edge type.

## Alternatives considered

- **Write suggestions directly to `ohm_edges` with a `suggested` flag** — rejected. Pollutes the canonical graph; every query must filter out suggestions; Bayesian inference would operate on unverified edges; breaks the clean separation between staging and production.
- **Time-only ripening (no evidence factor)** — rejected. A single agent's suggestion would auto-promote after enough time regardless of corroboration. This recreates the consensus-only problem (ADR-029) in suggestion form.
- **Weighted sum instead of product** — rejected. A weighted sum (e.g., `0.4*time + 0.4*evidence + 0.2*confidence`) allows a high-confidence single-source suggestion to promote quickly. The product enforces the AND-gate: all dimensions must be non-trivial. This is consistent with Hillman's truth-vs-consensus framing (ADR-028/029).

## References

- ADR-013 — Value of Information (suggestions feed VoI candidates)
- ADR-018 — Verification Loops and cross-link requirement
- ADR-028 — Source Tier Architecture (confidence ceilings on promoted edges)
- ADR-029 — Consensus-Only Confidence Ceilings (promoted edges subject to consensus nudge)
- ADR-003 — Agent-Owned Edges (promoted edges are owned by the promoting agent)
- `src/ohm/graph/schema.py:333` — `VALID_SUGGESTION_TYPES`, `VALID_SUGGESTION_STATUSES`
- `src/ohm/graph/schema.py:1034` — `ohm_suggestions` DDL
- `src/ohm/graph/queries/__init__.py:5305` — `create_suggestion` with duplicate prevention
- `src/ohm/graph/queries/__init__.py:5382` — `promote_suggestion`
- `src/ohm/graph/methods.py:4557` — `compute_ripeness`
- `src/ohm/graph/methods.py:4588` — `ripen_then_decide`
