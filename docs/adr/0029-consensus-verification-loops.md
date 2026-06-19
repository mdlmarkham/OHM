# ADR-029: Consensus-Only Confidence Ceilings and Auto-Challenge Nudges

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-2yq2 (this ADR), OHM-wvz8 (parent epic), ADR-018 (verification loops), ADR-028 (source tier ceilings)

## Context

Hillman's truth-vs-consensus framing distinguishes evidence from agreement: claims defended only by consensus ("no scholar agrees with him") are consensus-only support, not evidence. The Cornell UGC poisoning paper (arxiv 2605.24245) demonstrates that recursive agreement loops compound into false confidence — user-generated content can claim institutional consensus that does not actually exist.

ADR-028 added per-source confidence ceilings (`source_tier` → `TIER_CEILINGS`), but a CAUSES edge can still reach high confidence when many SUPPORTS edges agree even if none of those supporters' from_nodes have a recorded outcome in `ohm_outcomes`. The AND-gate of independent verification is missing for the support structure: multi-agent agreement without outcomes is still consensus, not evidence.

ADR-018's verification loops address the behavioral half (nudge agents to record outcomes) and the structural half (decay unverified edges). ADR-029 closes the remaining gap: detecting when an edge's support is consensus-only, recommending a confidence ceiling, and auto-nudging for verification.

## Decision

### 1. Consensus-only detection (computed, no DDL)

A L3 CAUSES edge is **consensus-only** when it has ≥1 SUPPORTS edge (linked via the `challenge_of` column) AND none of those supporters' `from_nodes` have a recorded outcome in `ohm_outcomes`. Multi-agent agreement without outcomes still counts as consensus (the Hillman point).

Implemented in `detect_consensus_only_support(conn, *, edge_id)` in `src/ohm/graph/queries/__init__.py:1297`. Returns:

```python
{
    "edge_id": str,
    "is_consensus_only": bool,       # True when no supporter has an outcome
    "supporting_edges": list[dict],  # SUPPORTS edges with tier/confidence/agent
    "strongest_tier": str | None,    # highest-ceiling tier among supporters
    "strongest_ceiling": float | None,
    "has_verified_outcome": bool,
    "recommended_ceiling": float | None,  # strongest_ceiling if consensus-only, else None
}
```

### 2. Recommended ceiling

`recommended_ceiling = SOURCE_TIER_CEILINGS[strongest_tier]` among the SUPPORTS edges (strongest = highest ceiling tier). When the support is NOT consensus-only (an outcome exists), `recommended_ceiling` is `None` — no consensus ceiling applies.

This is **additive** to the ADR-028 source_tier ceiling: the lower of the two wins. The ceiling is "recommended" and applied via the nudge/review path, not enforced at write time in `create_edge` — because support edges may not exist when a CAUSES edge is first created.

### 3. Auto-challenge nudge

`fire_verification_nudge(conn, *, edge_id, reason, created_by='system', confidence=0.3)` in `src/ohm/graph/queries/__init__.py:1368` creates a `CHALLENGED_BY` edge with `challenge_type='CONSENSUS_FLAG'` (distinct from agent-initiated `'CHALLENGED_BY'` type). Idempotent — skips if a `CONSENSUS_FLAG` nudge already exists for the edge. Does NOT call `enforce_challenge_boundary` (lightweight system signal, not a full challenge).

### 4. Heartbeat surfacing

`agent_heartbeat()` in `src/ohm/graph/methods.py:557` returns `consensus_nudge` (up to 3 of the agent's own consensus-only CAUSES edges, ordered by confidence desc) + `consensus_nudge_count`. Mirrors the existing `verification_overdue` / `sacred_references` / `challenge_nudge` inline-query pattern.

### 5. SDK

- `Graph.detect_consensus_only(edge_id)` — `src/ohm/framework/sdk.py:643`
- `Graph.fire_verification_nudge(edge_id, *, reason)` — `src/ohm/framework/sdk.py:653`

### 6. Scope

CAUSES edges only (Phase 1). Backward compatible — no schema change, no enforcement gate on existing rows.

## Consequences

**Positive:**
- Consensus-only edges are surfaced for verification without forcibly ceiling-clamping at write time (the ceiling is recommended and applied via the nudge/review path).
- Auto-nudges are capped at 3/heartbeat and idempotent, avoiding spam.
- The `CONSENSUS_FLAG` challenge_type is a metadata convention in the existing `challenge_type` VARCHAR column (no new frozenset, no DDL).
- Bridges ADR-028 (source tier) and ADR-018 (verification loops) — the consensus ceiling uses the tier ceiling as its basis, and the nudge feeds into the verification-overdue cycle.

**Negative:**
- Write-time ceiling enforcement in `create_edge` is deferred because support edges may not exist when a CAUSES edge is first created. A consensus-only edge can temporarily exceed its recommended ceiling until the nudge/review path catches it.
- The `CONSENSUS_FLAG` challenge_type is not in a validated frozenset — it relies on convention. A typo would create a different challenge_type that the idempotency check would not detect.

## Alternatives considered

- **Store a `consensus_only` boolean column** — rejected. Derived property that changes as outcomes are recorded; would create a stale-cache problem requiring re-evaluation on every outcome write.
- **Block synthesis on consensus-only edges** — rejected. Too aggressive; annotate and nudge instead of blocking. The oppositional review pipeline (ADR-030) handles the flag-only path.
- **Apply ceiling to all edge types** — rejected. Start with CAUSES per the issue scope (OHM-2yq2). PREDICTS/EXPECTS edges may be added in a future phase.

## Remaining plumbing (not in this phase)

- Write-time ceiling enforcement in `create_edge` when support already exists
- OhmStore wrappers for `detect_consensus_only_support` / `fire_verification_nudge`
- `GET /admin/consensus-scan` endpoint
- CLI commands: `ohm graph consensus-scan` / `ohm graph fire-nudge`

## References

- ADR-028 — Source Tier Architecture and Confidence Ceilings (tier ceilings used as consensus ceiling basis)
- ADR-018 — Verification Loops (behavioral nudge + structural decay; this ADR extends the nudge layer)
- Cornell UGC poisoning paper (arxiv 2605.24245) — recursive agreement loops compound into false confidence
- Hillman truth-vs-consensus — institutional consensus is an AND-gate on independent verifications, not an OR-gate on popularity
