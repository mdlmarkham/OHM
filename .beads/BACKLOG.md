# OHM Backlog — 2026-06-07

## Temporal Decay — Shipped ✅

```
OHM-xdd4 (CLOSED) ── Phase 1: Core Decay (schema + confidence_at + supersession)
OHM-xdd4.1-4 (CLOSED) ── Sub-issues: schema, confidence_at, supersession, neighborhood integration
OHM-wuki (CLOSED) ── Phase 2: Chain Validity (STL weakest-link bound)
OHM-v40d (SHIPPED ✅) ── Config bug: sync_interval_seconds nesting (commit 39fd6b0)
OHM-8fdb (SHIPPED ✅) ── Phase 3: Self-Calibration (commit 19a0e00)
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

## ADR-022: Layer Promotion Constraints — Shipped ✅

```
OHM-3ngi (SHIPPED ✅) ── ADR-022: Layer Promotion Constraints (batch optimization, commit eaedaf7)
```

**Implemented by Claude, merged to main:**
- `constraints.py` (457 lines): promotion gates, edge constraints, constraint checking
- `test_constraints.py` (439 lines): 26/26 tests passing
- `effective_layer()` + `constraint_status` on every node response
- Graduated enforcement: Advisory → Lenient → Strict
- Bug fix: constraint-report was referencing non-existent `layer` column; fixed to use `effective_layer()`

**Validated on production graph:** Hormuz AND-gate (43 obs, chain_validity 0.97) evaluates as effective_layer L1 because it has 0 sources, 0 outcomes, 0 REFERENCES edges. The constraints surface the Evaluation Trap correctly.

## Active — P0/P1

| ID | GitHub | Title | Status | Notes |
|----|--------|-------|--------|-------|
| OHM-v40d | #646 | Config bug: sync_interval_seconds nesting | **SHIPPED** ✅ | Commit 39fd6b0. Reads from ducklake sub-config first. 30/30 tests. |
| ~~OHM-3ngi~~ | ~~#645~~ | ~~ADR-022 Layer Gates~~ | **SHIPPED** ✅ | Batch constraint-report 106x speedup. Commit eaedaf7. |
| ~~OHM-8fdb~~ | ~~#647~~ | ~~Phase 3: Self-Calibration~~ | **SHIPPED** ✅ | Commit 19a0e00. Learned half-lives + authority decay. |
| ~~OHM-aznh~~ | ~~#665~~ | ~~INGEST Shell Hooks~~ | **SHIPPED** ✅ | All 13 sub-issues closed. Hooks live. |
| OHM-wdrg | #621 | Source Citation Architecture (ADR-013) | OPEN | L2 evidence layer enforcement. |
| OHM-a5rz | #644 | L0 Thinking Layer | OPEN | Fragments, scratch(), auto-linking. |
| OHM-tr71 | #636 | Proactive Discoverability | OPEN | Islands, suggestions, nudges. |

## Active — P2

| ID | GitHub | Title | Status | Notes |
|----|--------|-------|--------|-------|
| OHM-24g9 | #688/#666 | Phase 4: Weibull Generalization | OPEN | Wait for Phase 3 validation. |
| OHM-g0kv | #682/#707 | DEDUP: Content Hashing | OPEN | Idempotent ingestion. |
| OHM-6lvk | #684/#709 | DOCTOR: Graph Health Scoring | OPEN | Dependency-ordered remediation. |
| OHM-vj3i | CLOSED | TRAJ: Temporal Regression Detection | SHIPPED | In PR #627. |
| OHM-hflx | #681/#706 | Sacred references + challenge nudge | SHIPPED | In PR #627. |

## Dependency Chain (Updated)

```
SHIPPED:
  OHM-xdd4 (Phase 1) ──→ OHM-wuki (Phase 2) ──→ SHIPPED in PR #627
  OHM-3ngi (ADR-022) ──→ SHIPPED (constraints.py + tests)

ACTIVE:
  OHM-v40d ──→ Code fix for sync_interval_seconds (standalone, 1h)
  OHM-xdd4 (shipped) ──→ OHM-wuki (shipped) ──→ OHM-3ngi (shipped) ──→ OHM-8fdb (Phase 3, shipped) ──→ OHM-24g9 (Phase 4)
  OHM-aznh ──→ INGEST pipeline
  OHM-tr71 ──→ Proactive discoverability
  OHM-a5rz ──→ L0 Thinking Layer
  OHM-wdrg ──→ Source Citation Architecture
```

## Key Metrics (2026-06-07)

- **Nodes:** 1613 | **Edges:** 2456 | **Obs:** 1102
- **Schema:** v0.25.0 (temporal decay + chain validity + ADR-022)
- **Verification rate:** 33.1% | **Challenge ratio:** 6.2%
- **Write latency:** ~400ms (was 82s before config fix)
- **500 error rate:** 0% (was 16%)
- **Daemon:** v0.25.0, healthy
- **Config workaround:** `sync_interval_seconds: 0` at top level of ohmd.json
- **Tests:** 51/51 passing (decay + constraints)

## Next Actions

1. **OHM-v40d** → Send to Hephaestus as focused PR (10-line fix in server.py)
2. **OHM-8fdb** → Phase 3 SHIPPED commit 19a0e00
3. **Constraint-report optimization** → Batch `effective_layer()` for large graphs (currently O(n) queries per node)
4. **Source backfill** → Hormuz AND-gate has 0 source nodes, needs REFERENCES edges for L1→L2 promotion
5. **OHM-24g9** → Wait for Phase 3 validation before Weibull generalization
6. **OHM-tr71.4** → Island detection in heartbeat (partially shipped in PR #627)