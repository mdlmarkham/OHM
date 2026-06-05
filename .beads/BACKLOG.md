# OHM Backlog — 2026-06-05 (Session 2)

## Dependency Chain

```
OHM-aznh (DONE) ──→ OHM-tjzh ──→ OHM-wdrg ──→ OHM-cuu0 ──→ OHM-tnwa
                       │               │              │
                       │               │              └── vault isolation + promotion gate
                       │               └── source citation enforcement ✅ (L3:L2 = 1.8:1)
                       └── cross-link constraint (hooks MANDATORY ✅, dead-end cleanup ✅)
                       
                    OHM-vrfy ──────────┘ (verification scan ✅, decay engine ✅, heartbeat ⬜)
                    
OHM-smrt (assessment throughput, depends on wdrg)
OHM-g0kv (dedup, soft-depends on smrt)
OHM-6lvk (doctor, depends on tjzh, soft-depends vrfy)
OHM-vj3i (trajectory, soft-depends vrfy)
```

## P1 — Critical Path

| ID | Status | Title | Blocks | Depends On | Progress |
|----|--------|-------|--------|------------|----------|
| OHM-aznh | **CLOSED** | Hook Architecture | — | — | ✅ Shipped |
| OHM-tjzh | **in_progress** | Cross-Link Constraint | cuu0, tnwa | aznh (done) | Hooks enforced ✅, dead-end cleanup 158→81 ✅, orphan cleanup ⬜ |
| OHM-wdrg | open | Source Citation Architecture | cuu0, smrt | — | L2 edges: 0→712 ✅, L3:L2: 33:1→1.7:1 ✅ |
| OHM-cuu0 | open | Per-Agent Vaults + Promotion Gate | tnwa | tjzh, wdrg | ⬜ |
| OHM-tnwa | open | Gap Analysis Synthesis | — | cuu0, tjzh | ⬜ |
| OHM-vrfy | **in_progress** | Verification Scheduling (ADR-018) | — | tjzh | Scan ✅, Decay ✅, Heartbeat ⬜, AGENTS.md ⬜ |

## P2 — Important, Not Blocking

| ID | Status | Title | Depends On |
|----|--------|-------|------------|
| OHM-smrt | open | Stage 4 Assessment Throughput | aznh (done), wdrg |
| OHM-g0kv | open | Content Hashing & Dedup | wdrg, smrt (soft) |
| OHM-vj3i | open | Temporal Regression Detection | vrfy (soft) |
| OHM-6lvk | open | Graph Health Scoring (Doctor) | tjzh, vrfy (soft) |
| OHM-od01.4 | open | Structure Learning (Causal Discovery) | od01 (parent) |

## Milestone: multi-tenant-v2

55 issues (all P5). Deferred.

## Key Metrics (2026-06-05 10:00)

- 727 nodes, 1657 edges, 398+ observations
- Dead ends: 81 (80 source = valid sinks, 1 system node)
- L3:L2 ratio: 1.7:1 ← target exceeded (was 33:1)
- Challenge ratio: 3.64% ← needs improvement (target: >8%)
- Outcomes recorded: 31, verification rate: 25.6%
- Verification decay: 50 edges decayed (43 unverified, 7 verified)
- Hooks enforced: cross_link_check ✅, source_url_required ✅
- Verification scan: ✅, Decay engine: ✅
- 1731 tests passing

## Verification Decay Results (First Run)

| Edge Type | Verified? | Age (days) | Original Conf | New Conf | Half-life |
|-----------|-----------|------------|---------------|----------|-----------|
| CAUSES | No | 15 | 0.95 | 0.67 | 30d |
| CAUSES | Yes | 15 | 0.92 | 0.89 | 365d |
| CAUSES | No | 14 | 0.90 | 0.65 | 30d |
| PREDICTS | No | 15 | 0.90 | 0.64 | 30d |

**50 edges affected total.** Verified edges barely decay (0.92 → 0.89 after 15 days). Unverified edges decay meaningfully (0.95 → 0.67 after 15 days). This is the structural enforcement of ADR-018.