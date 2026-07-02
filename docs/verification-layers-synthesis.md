# Verification Layers — Synthesis Note (OHM-l1nd)

**Author:** Metis (via Metis test-synthesis task)
**Date:** 2026-07-02
**Scope:** Survey of the current verification stack in OHM and identification
of gaps where a new verification layer would add value.

## Current Layers

The OHM verification stack is composed of several independent layers, each
addressing a different failure mode. Most layers are independently toggleable
via ADR and source code, so operators can compose them per domain.

### 1. Source tiers (ADR-028)

Quality ceiling for confidence based on the source. Implemented in
`src/ohm/validation.py:enforce_confidence_ceiling` and consumed by
`create_node`, `create_edge`, `batch_create_nodes`, `batch_create_edges`,
and the OhmStore write paths. The `source_tier` column on `ohm_edges` and
the inferred tier on `ohm_nodes` (based on `provenance`/`source_author`)
is the input. Output: an error or a `value_error` if confidence exceeds
the tier's ceiling.

**Failure mode addressed:** prevents premature high-confidence claims
from unverified sources.

### 2. Confidence decay (ADR-018)

Time-based erosion of confidence on unverified L3/L4 edges.
`/admin/verification-decay` applies the decay; the heartbeat returns
`verification_overdue` for any CAUSES/PREDICTS/EXPECTS edge older than 14
days with no recorded outcome. Verified edges (via `/record-outcome` or
`ohm_outcomes`) get a 365-day half-life; unverified edges get a 30-day
half-life.

**Failure mode addressed:** prevents evaluation trap (uncorroborated
claims persisting forever).

### 3. Challenge pipeline (ADR-009, OHM-e0t1)

`CHALLENGED_BY` edges attach to a target edge with a required
`challenge_reason` (lint guard enforced at write time per OHM-e0t1).
A high challenge ratio (>5% of L3 edges) is the operational signal in
the heartbeat `challenge_nudge`.

**Failure mode addressed:** counter-evidence propagation; explicit
disagreement tracking.

### 4. Outcomes tracking

`/record-outcome` records a TRUE/FALSE/AMBIGUOUS/DEFERRED outcome on a
claim node, which (a) stops confidence decay for the originating edge
and (b) feeds the empirical half-life calibration (layer 6).

**Failure mode addressed:** the source has made a prediction, what
happened?

### 5. TELOS signing (ADR-0035)

`write_signature`, `signing_key_id`, `signed_at` columns on `ohm_nodes`
provide cryptographic attribution. Wired through OhmStore and the write
path. Not a verification layer per se but provides provenance.

**Failure mode addressed:** who actually wrote this, can we trust the
attribution?

### 6. Calibration (OHM-8fdb)

`empirical_half_life()` learns from supersession events per observation
type. `effective_reliability()` decays source `p_accurate` toward a
community prior when sources go unverified. Both feed back into
`/source-reliability/{agent_id}`.

**Failure mode addressed:** the fixed 90/30-day half-lives in layer 2
are wrong for some observation types; learned half-lives converge
toward truth.

### 7. Heartbeat nudges (multiple)

The heartbeat emits several nudge types when conditions warrant:
- `verification_overdue` (layer 2)
- `sacred_references` (high confidence + zero observations; ADR-018.4)
- `challenge_nudge` (low challenge ratio; ADR-018.4)
- `epistemic_nudges` (web-research nodes without source-tier or
  outcome; OHM-secv)
- `consensus_nudge` (CAUSES edges supported only by SUPPORTS without
  outcomes; OHM-2yq2)
- `orphan_rate_nudge` (non-fragment orphan rate > 10%; OHM-jx4q item 4)

**Failure mode addressed:** the agent should be aware of these states
on each heartbeat.

### 8. Cross-link requirement (ADR-018, OHM-tjzh)

Derived-claim node types (pattern, idea, task, decision, plus
forward-compat synthesis/observation/interpretation/challenge) MUST
reference at least one existing node via `connects_to`. The server
rejects writes with HTTP 422 otherwise. Prevents the dead-end rate from
growing with new claims.

**Failure mode addressed:** new claims floating free with no
contextual anchor.

## Cross-cutting observations

**Strengths:**
- The layers are **independent and composable**. An operator can
  enable or disable any one without breaking the others.
- The verification path is **mostly data-driven** (DECAY is a
  formula; SOURCE_TIER_CEILINGS is a constant table; CHALLENGED_BY is
  just an edge type). The agent doesn't have to remember to do
  anything; the system applies the layers automatically on write and
  on heartbeat.
- **Outcomes are first-class** in `ohm_outcomes`, which is the
  convergence point for several layers (decay, calibration, nudges).

**Weaknesses:**
- **No AND-gate visibility**: there's no single "is this claim
  fully verified?" check. An operator has to mentally compose: tier
  ceiling OK + decay within bounds + not challenged + outcome
  recorded + signed. The heartbeat nudges are *individual* signals,
  not a rollup.
- **Outcomes are agent-recorded**: `/record-outcome` requires the
  agent to acknowledge the outcome. If the agent forgets or moves on,
  the claim decays anyway (via the unverified path). This is by design
  but creates a failure mode where genuine outcomes go unrecorded.
- **Cross-graph corroboration is missing**: two independent sources
  making the same claim produce two independent CAUSES edges, but
  nothing flags "this claim is corroborated by N independent sources".
  The `consensus_nudge` (layer 7) flags the inverse — consensus WITHOUT
  outcomes — but doesn't reward multi-source convergence.
- **Source reputation decay is per-source, not per-edge-type**: a
  source reliable about cattle health may be unreliable about stock
  prices. `source_reliability` is one row per agent; the calibration
  layer (6) doesn't break it down by domain.
- **The verification pipeline trusts the agent's claim_type**: a
  PREDICTS edge with confidence 0.9 from a `research` provenance
  gets the same half-life as a PREDICTS edge from a `field-test`
  provenance. The provenance is stored but not used to modulate
  half-life.

## Gaps and Recommendations

### Recommended: cross-graph corroboration (Tier 1)

When two independent sources (different `source_author` or
`provenance`) make the same L3 claim (same `to_node` and same
`edge_type`), flag the edge as **corroborated** and bump the effective
confidence by a small factor (e.g. 1.1× with diminishing returns at N).
This is the missing AND-gate for "is this claim well-supported?".

**Why it matters:** currently the agent has to scan the graph to
find supporting edges. A simple "corroborated by N sources" attribute
on the edge would make the verification state explicit.

**Effort:** 1-2 days. Surface as a new column `corroboration_count`
on `ohm_edges` populated by a periodic background job, or computed
on read. New endpoint `/edges/{id}/corroboration` to expose the
detail.

### Recommended: domain-aware source reputation (Tier 1.5)

Extend `source_reliability` to break down by domain (or by
`provenance` prefix). An agent reliable about cattle should have a
high reliability in cattle-related claims and a separate, possibly
lower, reliability elsewhere.

**Why it matters:** today `p_accurate` is a single number per source;
cross-domain claims are scored against the agent's global
reliability. This rewards generalists over specialists and punishes
newcomers who are reliable in their domain.

**Effort:** 2-3 days. Schema change to `source_reliability` to add a
`(source_id, domain)` composite key, with default domain = `"*"` for
unscoped. Backfill via existing `ohm_outcomes` history.

### Recommended: outcome-provenance tie (Tier 2)

When an agent records an outcome via `/record-outcome`, the outcome
should automatically credit the *original* edge's `source_tier` and
`source_author`, not the agent recording the outcome. Today the
outcomes table has its own `recorded_by` agent, but the verification
nudges score by the edge's `created_by` — these are usually the same
but not always (a different agent can record an outcome for another
agent's claim).

**Why it matters:** if a claim is verified by an outcome, the
"verified by" credit should flow to the *source* (the agent who
made the claim), not the verifier. This is a small data-model fix.

**Effort:** 0.5 day. Add `claimed_by` and `verified_by` columns to
`ohm_outcomes`, backfill from the originating edge's
`created_by`, and update the nudges to credit `claimed_by`.

### Deferred: agent reputation decay (Tier 3)

We have source reliability decay (layer 6) but not agent reputation
decay. An agent who hasn't heartbeated in 30 days is currently
treated identically to one who heartbeated 1 minute ago. The
`stale_agents` count in graph_health flags this but doesn't act on
it.

**Why deferred:** the impact is small for current scale; agents are
usually long-running. Revisit when there are >20 agents.

## Summary

The verification stack is well-layered and composable. The main gap
is **cross-graph corroboration** — the ability to flag a claim as
"corroborated by N independent sources" without manually scanning
the graph. A second tier-1 gap is **domain-aware source reputation**,
which would replace the single per-source reliability number with a
per-(source, domain) breakdown. Both are tractable and would compose
cleanly with the existing layers without disrupting them.
