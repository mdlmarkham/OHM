# ADR-015: Source Citation Architecture

## Status
Accepted — 2026-05-27

## Context

OHM's knowledge graph has a severe evidence gap. An audit triggered by Socrates's challenge feedback revealed:

- **209 observations with zero `source_url` fields populated**
- **0 L2 (citation) edges** in the entire graph
- **10 source nodes** out of 515 total — all in the agent governance domain
- **L3:L2 edge ratio: 33:1** (764 L3 interpretation edges vs 23 L2 citation edges)

Agents write observations with `source="guardian_reuters_ap"` — a label, not a citation. When an interpretation is challenged, there is no way to trace it back to the original document.

This creates an epistemic fragility: confident interpretations without verifiable evidence. The Bayesian inference engine propagates confidence mathematically, but mathematically confident wrong answers are worse than uncertain right ones.

## Decision

Implement source citation enforcement through a combination of structural constraints and instructional culture.

### Structural (schema/API level, cannot bypass)

1. **Source nodes require `source_url`** — `POST /node` with `type=source` and no `source_url` returns HTTP 400. Updates to existing source nodes are exempt (may not have URL at creation time but can be added later).

2. **L3 edges annotated with `citation_status`** — The `/neighborhood` endpoint checks whether each L3 edge has any L2 REFERENCES edges in its neighborhood. If yes: `citation_status: "verified"`. If no: `citation_status: "unverified"`. This is informational only — does not block creation.

3. **Observation `source_url` field** — The schema already supports `source_url` on observations. Agents are prompted to populate it (instructional, not enforced — some observations are agent-internal reasoning with no external source).

### Instructional (prompt/culture level)

1. **Source Trigger added to OHM Writing Protocol** — 4th trigger (after interpretation, challenge, observation): every observation should include `source_url` when an external source exists.

2. **Socrates challenge protocol updated** — `citation_status: "unverified"` on L3 edges is grounds for challenge even if reasoning is sound. "Interpretation lacks traceable sources" is a valid challenge reason.

3. **Heartbeat backfill tasks** — Periodic cleanup to populate `source_url` on existing observations and create missing source nodes.

## Rationale for the Structural/Instructional Split

**Structural enforcement** prevents the worst cases (source nodes without URLs, untraceable claims). But making `source_url` required on *every* observation would choke throughput — some observations are agent-internal (pattern syntheses from existing graph data, challenge assessments) that don't have a single external source.

**Instructional guidance** covers the long tail — agents include sources by default, but aren't blocked when they can't. The backfill process catches what was missed.

The `citation_status` field is intentionally informational, not blocking. Forcing every L3 edge to have L2 backing would prevent agents from writing pure reasoning (which is legitimate L3 knowledge). Flagging is the right level — it enables quality auditing without preventing creation.

## Consequences

- **Positive:** Verifiable knowledge. Challenged interpretations can be traced to sources. Challenge ratio should increase as Socrates targets unverified claims.
- **Negative:** Slightly slower source node creation (requires URL). Some observations will remain without `source_url` (agent-internal). L3:L2 ratio will take time to improve from 33:1.
- **Target:** Reduce L3:L2 ratio to 5:1 or better within 30 days through backfill and new-writing discipline.

## Implementation

| Issue | Description | Status |
|-------|-------------|--------|
| OHM-wdrg.1 | Schema: Enforce source_url on source nodes | Done |
| OHM-wdrg.2 | API: Return citation_status on L3 edges | Done |
| OHM-wdrg.3 | Migration: Convert source strings to node references | Open |
| OHM-wdrg.4 | AGENTS.md: Add Source Trigger | Done |
| OHM-wdrg.5 | Socrates: Update challenge protocol | Done |
| OHM-wdrg.6 | This ADR | Done |
| OHM-wdrg.7 | Backfill source nodes for Hormuz | Open |

## References

- Socrates challenge nodes: `concept-survivorship-bias-domain-mapping`, `concept-unmeasured-confounding-causal`, `concept-selection-bias-and-or-domains`, `concept-bayesian-sparse-graph-limitations`, `concept-demand-rationing-alt-explanation`
- OHM stats at time of audit: 515 nodes, 820 edges, 209 obs, 0 L2 edges, 10 source nodes