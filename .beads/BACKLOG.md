# OHM Backlog — 2026-06-07

## Temporal Decay — Shipped ✅

```
OHM-xdd4 (CLOSED) ── Phase 1: Core Decay (schema + confidence_at + supersession)
OHM-xdd4.1-4 (CLOSED) ── Sub-issues: schema, confidence_at, supersession, neighborhood integration
OHM-wuki (CLOSED) ── Phase 2: Chain Validity (STL weakest-link bound)
OHM-v40d (OPEN) ── Config bug: sync_interval_seconds nesting (code fix, workaround in place)
OHM-8fdb (OPEN) ── Phase 3: Self-Calibration (learned half-lives + authority decay)
OHM-24g9 (OPEN) ── Phase 4: Weibull Generalization (continuous shape parameter)
```

**Shipped in PR #627 (2026-06-07):** Schema v0.25.0, 35/35 tests, daemon live.
- `half_life_days`, `valid_from`, `valid_to`, `supersedes_obs_id` on observations
- `confidence_at()` with 5 decay profiles (perishable, fast-perishable, durable, binary, appreciating)
- `supersede_observation()` + chain queries
- `chain_validity()` with STL weakest-link + multiplicative for syntheses
- `min_validity` parameter on neighborhood() queries
- Sacred references + challenge nudge in heartbeat
- Temporal regression detection tests
- Security fixes (hook injection, timeout DoS, SSRF)

**Live validation:** `neighborhood?min_validity=0.5` shows chain validity ranging from 0.812 (strong) to 0.016 (catastrophic — individual obs at 0.117, chain at 0.016). This is the Chronofy insight quantified on our own graph.

## Active — P0/P1

| ID | Status | Title | Notes |
|----|--------|-------|-------|
| OHM-v40d | OPEN | Config bug: sync_interval_seconds nesting | 10-line fix in server.py. Workaround in place (top-level config). Ready for Hephaestus. |
| OHM-8fdb | OPEN | Phase 3: Self-Calibration | Full spec in Beads. Dependent on Phase 1 (shipped). 10-14h. |
| OHM-aznh | OPEN | INGEST: Shell Hook Architecture | Staged pipeline for ingestion. |
| OHM-tr71 | OPEN | Proactive Discoverability | Islands, suggestions, nudges. |
| OHM-a5rz | OPEN | L0 Thinking Layer | Fragments, scratch(), auto-linking. |
| OHM-wdrg | OPEN | Source Citation Architecture (ADR-013) | L2 evidence layer enforcement. |

## Active — P2

| ID | Status | Title | Notes |
|----|--------|-------|-------|
| OHM-24g9 | OPEN | Phase 4: Weibull Generalization | Replaces 5 profiles with continuous shape param. Wait for Phase 3 validation. |
| OHM-g0kv | OPEN | DEDUP: Content Hashing + Alias Resolution | Idempotent ingestion. |
| OHM-6lvk | OPEN | DOCTOR: Graph Health Scoring | Dependency-ordered remediation. |
| OHM-4vs | DEFERRED | Large module decomposition | sdk.py 3309 lines, bayesian.py 2768 lines. |
| OHM-wdrg.3 | IN PROGRESS | Source string → source node migration | 372 observations migrated. |
| OHM-wdrg.7 | IN PROGRESS | Backfill source nodes for Hormuz observations | |
| OHM-tr71.4 | OPEN | Island detection in heartbeat nudges | |
| OHM-a5rz.27 | OPEN | Fragment TTL and soft eviction | |
| OHM-vj3i | CLOSED | TRAJ: Temporal Regression Detection | Shipped in PR #627. |
| OHM-hflx | CLOSED | Sacred references + challenge nudge | Shipped in PR #627. |

## Active — P3/P4

| ID | Status | Title | Notes |
|----|--------|-------|-------|
| OHM-tr71.7 | OPEN | Challenge nudge for low challenge ratio | |
| OHM-tr71.9 | OPEN | Fuzzy matching fallback for text search | |
| OHM-od01.4 | OPEN | Structure Learning: Causal Discovery | |
| OHM-od01.5 | OPEN | POMDP Decision Intelligence | |
| OHM-9iyh | OPEN | PERT elicitation automation | |

## Client Communication (DEFERRED)

All client comms issues (OHM-lbiv, OHM-tss4.14/16/17/18, OHM-1779791713260-23) are deferred. Not currently active.

## Dependency Chain (Updated)

```
SHIPPED:
  OHM-xdd4 (Phase 1) ──→ OHM-wuki (Phase 2) ──→ SHIPPED in PR #627

ACTIVE:
  OHM-v40d ──→ Code fix for sync_interval_seconds (standalone, 1h)
  OHM-8fdb (Phase 3) ──→ OHM-24g9 (Phase 4)
  OHM-aznh ──→ INGEST pipeline
  OHM-tr71 ──→ Proactive discoverability
  OHM-a5rz ──→ L0 Thinking Layer
  OHM-wdrg ──→ Source Citation Architecture
```

## Key Metrics (2026-06-07)

- **Nodes:** 1608 | **Edges:** 2598 | **Obs:** 1066
- **Schema:** v0.25.0 (temporal decay + chain validity)
- **Verification rate:** 33.1% | **Challenge ratio:** 3.5%
- **Write latency:** ~400ms (was 82s before config fix)
- **500 error rate:** 0% (was 16%)
- **Daemon:** PID 2221501, v0.25.0, healthy
- **Config workaround:** `sync_interval_seconds: 0` at top level of ohmd.json

## Next Actions

1. **OHM-v40d** → Send to Hephaestus as focused PR (10-line fix in server.py)
2. **OHM-8fdb** → Queue for Claude (Phase 3, full spec in Beads)
3. **OHM-24g9** → Wait for Phase 3 validation in production
4. **OHM-wdrg.3/.7** → Continue source node backfill
5. **OHM-tr71.4** → Island detection in heartbeat (partially shipped in PR #627)