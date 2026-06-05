# ADR-018: Verification Loops — Constraint Persistence Enforcement

**Date:** 2026-05-28
**Status:** Proposed
**Author:** metis
**Tags:** verification, constraint-persistence, evaluation-trap, karpathy, governance

## Context

OHM has infrastructure for verification but uses none of it:

- `record_outcome` / `source_reliability`: **0 outcomes recorded** across all agents
- `CHALLENGED_BY` edges: 51 out of 1,335 total (2.3% challenge ratio)
- `compound_confidence`: Returns 1.0 for heavily-observed nodes regardless of whether observations confirm or contradict
- No automated mechanism checks whether L3 interpretations match reality over time

This is the **Constraint Persistence Decay** problem (Karpathy Rule 6 → Rule 8 gap): constraints written as confidence values persist without verification, becoming sacred references. The Evaluation Trap operates at the governance layer:

1. **Measurement Created** → Agent writes confidence = 0.88 on an L3 edge
2. **Sacred Reference** → Confidence persists in graph without challenge
3. **Optimization** → Other agents cite the confidence as evidence
4. **Epistemic Closure** → Unverified confidence compounds into compound_confidence = 1.0

## Decision

Add three verification loop mechanisms to OHM:

### 1. Verification Loop Nudge (ADR-018.1)

When agents create CAUSES, PREDICTS, EXPECTS, or EXPECTED_LIKELIHOOD edges, the nudge system prompts them to record outcomes when reality validates or falsifies the claim. This is the **behavioral layer** (OR-gate: each prompt independently improves verification).

### 2. Constraint Persistence Nudge (ADR-018.2)

When agents create high-confidence nodes (≥ 0.85) without observations, prompt for verification. High confidence without evidence = sacred reference. This prevents the Evaluation Trap from forming.

### 3. Automated Verification Scheduling (ADR-018.3 — Future)

**Structural layer** (AND-gate: must work with nudges). A scheduled process that:

1. Scans L3 edges with CAUSES/PREDICTS/EXPECTS types
2. For each, checks if any outcome has been recorded
3. If no outcome after N days (configurable, default 14), generates a `verification_overdue` nudge
4. After 30 days without outcome, decays the edge confidence by a configurable factor (default 0.95/period)
5. After 90 days without outcome, marks the edge as `unverified` in metadata

This is the structural enforcement of Rule 8 (Verification Loops): claims that aren't verified decay, rather than persisting as sacred references.

## The AND-OR Architecture

| Mechanism | Gate Type | Layer | Error Reduction |
|-----------|-----------|-------|-----------------|
| Verification nudge | OR-gate | Behavioral | Prompt agents to verify |
| Constraint persistence nudge | OR-gate | Behavioral | Prevent sacred references |
| Automated confidence decay | AND-gate | Structural | Unverified claims decay |
| Source reliability tracking | AND-gate | Structural | Agent trust calibrated by outcomes |

The nudges (OR-gate) achieve partial adoption — some agents will verify, some won't. The automated decay (AND-gate) ensures that **even without agent cooperation**, unverified claims can't persist indefinitely at their original confidence.

## Compound Confidence Fix (ADR-018.4 — DEPLOYED)

`compound_confidence` now includes three new weighting factors:

1. **Staleness decay** — observations lose weight with 30-day half-life (configurable via `?half_life=`)
2. **Source diversity correlation** — same-agent same-day observations are correlated (0.9), not independent; different agents (0.2) compound independently. Default on via `?diversity=true`
3. **Verification factor** — unverified causal edges reduce confidence by up to 30%. Verified edges (with recorded outcomes) maintain full confidence
4. **Source reliability weighting** — agent p_accurate modulates observation weight. Default on via `?source_weights=true`

Results (before → after):
- hormuz_and_gate: 1.0 → 0.6934 (43 obs, 3 agents, diversity_corr=0.6, vf=1.0)
- concept-truce-treadmill: 1.0 → 0.6563 (17 obs, 1 agent, vf=1.0)
- oil_or_gate_pricing: 1.0 → 0.6571 (14 obs, vf=1.0)
- concept-warsh-doom-loop: 1.0 → 0.4152 (1 obs, strong staleness)
- concept-noble-lie-and-gate: 1.0 → None (0 obs, vf=0.7 for unverified edge)

## Implementation Plan

1. ✅ Nudges extended (ADR-018.1 + ADR-018.2) — `nudges.py` updated
2. ⬜ Automated verification scheduler — new `/admin/verification-scan` endpoint
3. ⬜ Confidence decay engine — extend `compound_confidence` with staleness
4. ⬜ Agent heartbeat integration — include verification status in heartbeat response
5. ⬜ Dashboard — show unverified claims, overdue verifications, reliability scores

## Connection to Trap Research

This is the structural enforcement of the Deterministic Constraint as Universal Escape Pattern:

- **Trap:** Confidence persists without verification → sacred reference → epistemic closure
- **Escape:** Structural enforcement of verification (claims decay without outcomes)
- **Direction:** AND-gate in escape direction — all verification mechanisms must hold

The 41%→11%→5% cascade from Karpathy constraint engineering:
- Nudges alone operate in the 11%→41% range (behavioral)
- Automated decay operates in the 5%→11% range (structural)
- Both together achieve the 5% floor

## References

- Karpathy Rule 6: Constraint Persistence (compliance decays past 200 lines)
- Karpathy Rule 8: Verification Loops (every step produces verifiable artifact)
- Shashwat data: 41%→11% (behavioral) → 5% (structural)
- Evaluation Trap: Measurement → Sacred Reference → Optimization → Epistemic Closure
- AGT architecture: Sub-millisecond interception = structural constraint enforcement
- Cequence: Persona-scoped MCP = structural scope isolation