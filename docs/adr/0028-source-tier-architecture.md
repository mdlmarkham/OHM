# ADR-028: Source Tier Architecture and Confidence Ceilings

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-wvz8.1 (this ADR + schema), OHM-wvz8 (parent epic), ADR-015 (source citation), ADR-018.3 (verification decay)

## Context

OHM's `confidence` field on nodes and edges has a range check [0, 1] but no quality dimension. A 0.9 confidence claim from a single Reddit post is currently indistinguishable from a 0.9 confidence claim from a peer-reviewed replication. Mathematically confident wrong answers propagate further than uncertain right ones — the Bayesian stack compounds them without any signal about source quality.

Three threads converge on this gap:

1. **Cornell UGC poisoning** (arxiv 2605.24245) — user-generated content can claim institutional consensus that does not actually exist. OHM must not treat "many agents said so" as evidence of independent verification.

2. **Hillman truth-vs-consensus** — institutional consensus is captured by an AND-gate on independent verifications. Raw agreement from many sources is not the same as evidence; it can be a coordinated capture signal. Confidence must reflect the AND-gate, not the OR-gate of popularity.

3. **tastebud-memory hyperdimensional composition** — similarity can only be assessed after explicit basis identification. A high-confidence claim with no identified source basis cannot be sensibly compared to a high-confidence claim with a peer-reviewed basis. The tier is the basis.

ADR-015 hinted at this distinction with `citation_status: "verified" | "unverified"`. ADR-018.3 implemented the structural half-life split: 30-day decay for unverified, 365-day for verified. OHM-wvz8.1 formalizes the underlying dimension as a write-time enum with confidence ceilings, so that source quality becomes an enforced property rather than a derived annotation.

## Decision

### 1. The five-tier enum

| Tier | Meaning | Ceiling |
|------|---------|---------|
| `raw` | Single unverified claim, no citation | 0.3 |
| `unverified` | Cited but no independent corroboration | 0.5 |
| `preliminary` | One independent verification recorded | 0.7 |
| `official` | Institutional / peer-reviewed source | 0.9 |
| `verified` | Multi-source confirmed AND outcome recorded | 1.0 |

The tiers form a strict monotonic ordering — promoting to a higher tier requires evidence the lower tier does not have (a citation, an independent corroboration, an institutional process, a recorded outcome). Demotion is not modeled; once verified, a claim does not regress to preliminary.

### 2. Storage

VARCHAR column `source_tier` on both `ohm_nodes` and `ohm_edges`. NULL means "tier not assessed" and bypasses ceiling enforcement — this preserves existing write paths for non-tier-aware callers and prevents breaking legacy code on migration.

```sql
ALTER TABLE ohm_nodes ADD COLUMN source_tier VARCHAR;
ALTER TABLE ohm_edges ADD COLUMN source_tier VARCHAR;
```

A CHECK constraint validates tier values when populated:

```sql
CHECK (source_tier IS NULL OR source_tier IN ('raw','unverified','preliminary','official','verified'))
```

### 3. Enforcement

At write time in `create_node` / `create_edge`. When `source_tier` is set and `confidence > ceiling`, raise `ValueError`:

```python
TIER_CEILINGS = {
    "raw": 0.3,
    "unverified": 0.5,
    "preliminary": 0.7,
    "official": 0.9,
    "verified": 1.0,
}

if source_tier is not None and confidence > TIER_CEILINGS[source_tier]:
    raise ValueError(
        f"confidence {confidence} exceeds {source_tier} ceiling {TIER_CEILINGS[source_tier]}"
    )
```

Enforcement is in `ohm.store` (OhmStore) and `ohm.queries` — both write paths must check. There is no global post-write validator; the ceiling is enforced at the point of confidence assignment, not at read time.

### 4. Default and backward compatibility

No default. Existing callers pass NULL → no behavior change. The protection is opt-in until callers begin passing `source_tier` explicitly. This is intentional: a hard default would either break callers (if set to a low tier) or fail to add value (if set to `verified`).

### 5. Migration

Schema version `0.30.0`. Migration script adds the two columns and the CHECK constraint. No backfill — existing rows default to NULL. Agents and callers that want enforcement migrate by adding `source_tier=` to their write calls.

## Tier mapping rationale

The enum subsumes and formalizes existing signals:

| Existing concept | Maps to |
|------------------|---------|
| `citation_status: "verified"` (ADR-015) | `source_tier: "official"` (or `"verified"` if outcome recorded) |
| `citation_status: "unverified"` (ADR-015) | `source_tier: "unverified"` |
| ADR-018.3 unverified edge (30-day half-life) | `source_tier: "unverified"` |
| ADR-018.3 verified edge (365-day half-life) | `source_tier: "verified"` |
| No source, raw agent observation | `source_tier: "raw"` |
| Single external corroboration recorded | `source_tier: "preliminary"` |

The 30-day / 365-day split (ADR-018.3) maps cleanly: `unverified` edges decay with the 30-day half-life, `verified` edges with the 365-day half-life. This is not coincidental — the half-life split was already implicitly a tier distinction. OHM-wvz8.1 makes it explicit and write-time enforced.

## Consequences

**Positive:**
- OHM can no longer confuse high-confidence claims with high-quality sources. A 0.9 confidence `raw` claim is now a `ValueError`, not a sacred reference.
- Source diversity scoring (OHM-qi6r) becomes feasible — tiers give a quality basis for diversity, not just author diversity.
- Verification loops (OHM-2yq2) can respect tier — promoting `unverified` → `preliminary` requires a specific evidence shape.
- Oppositional review (OHM-jbsr) has a concrete basis to challenge against: "your tier is X but your confidence implies Y."
- The Bayesian stack's compound confidence now compounds within tier-constrained bounds, not over an open [0, 1] range.

**Negative:**
- Legacy write paths without tier pass NULL and skip ceiling — protection is opt-in until callers migrate. Until then, the schema-level constraint is weaker than intended.
- Tier assignment is a judgment call. Agents may disagree on whether a source is `preliminary` or `official`. Challenge semantics (ADR-003) apply — this is acceptable disagreement, not consensus averaging.
- The 5-tier scale is coarse. A claim with four independent verifications sits at `preliminary` until an institutional source confirms; this is intentional (tiers are about source class, not count).

## Alternatives considered

- **Single boolean `is_verified`** — rejected. Too coarse: cannot distinguish `preliminary` (one verification) from `official` (peer review). Loses the basis identification that tastebud-memory composition requires.
- **Numeric tier 1–5** — rejected. Less readable in logs and audit trails; harder for agents to communicate in natural language ("this is `official`, not `preliminary`" is clearer than "this is tier 4").
- **Tier-keyed probability rather than confidence** — rejected. `probability` already has distinct semantics per ADR-008 (likelihood of described relationship occurring in the world). Conflating tier with probability would break the agent's ability to record "70% chance this fails" with `"official"` provenance.
- **Tier on observations only, not on edges/nodes** — rejected. The verification decay (ADR-018.3) operates on edges; observations are already tiered by their own `source_url` presence. Putting tier on edges is where the ceiling enforcement pays off.

## Implementation

| Issue | Description | Status |
|-------|-------------|--------|
| OHM-wvz8.1 | Schema: add `source_tier` to nodes and edges with CHECK | Open |
| OHM-wvz8.2 | `create_node` / `create_edge` ceiling enforcement | Open |
| OHM-wvz8.3 | SDK passthrough in `ohm.sdk.connect` | Open |
| OHM-wvz8.4 | Tests for ceiling enforcement + NULL bypass | Open |
| OHM-wvz8.5 | Migration `0.30.0` | Open |

## References

- ADR-015 — Source Citation Architecture (introduced `citation_status`)
- ADR-018 / ADR-018.3 — Verification Loops, automated confidence decay (30d/365d split)
- ADR-008 — Probability and Confidence model (kept distinct from tier)
- ADR-003 — Agent-owned edges (challenge semantics apply to tier assignments)
- Cornell UGC poisoning paper (arxiv 2605.24245)
- Hillman truth-vs-consensus: institutional consensus is an AND-gate on independent verifications
- tastebud-memory hyperdimensional composition: basis identification before similarity
