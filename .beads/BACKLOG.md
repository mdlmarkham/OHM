# OHM Backlog — 2026-06-05

## Dependency Chain

```
OHM-aznh (DONE) ──→ OHM-tjzh ──→ OHM-wdrg ──→ OHM-cuu0 ──→ OHM-tnwa
                       │               │              │
                       │               │              └── vault isolation + promotion gate
                       │               └── source citation enforcement ✅ (L3:L2 = 1.8:1)
                       └── cross-link constraint (hooks MANDATORY, orphan cleanup pending)
                       
                    OHM-vrfy ──────────┘ (verification scan deployed, decay engine pending)
                    
OHM-smrt (assessment throughput, depends on wdrg)
OHM-g0kv (dedup, soft-depends on smrt)
OHM-6lvk (doctor, depends on tjzh, soft-depends vrfy)
OHM-vj3i (trajectory, soft-depends vrfy)
```

## P1 — Critical Path

| ID | Status | Title | Blocks | Depends On | Progress |
|----|--------|-------|--------|------------|----------|
| OHM-aznh | **CLOSED** | Hook Architecture | — | — | ✅ Shipped |
| OHM-tjzh | **in_progress** | Cross-Link Constraint | cuu0, tnwa | aznh (done) | Hooks enforced ✅, orphan cleanup ⬜ |
| OHM-wdrg | open | Source Citation Architecture | cuu0, smrt | — | L2 edges: 0→710 ✅, L3:L2: 33:1→1.8:1 ✅ |
| OHM-cuu0 | open | Per-Agent Vaults + Promotion Gate | tnwa | tjzh, wdrg | ⬜ |
| OHM-tnwa | open | Gap Analysis Synthesis | — | cuu0, tjzh | ⬜ |
| OHM-vrfy | **in_progress** | Verification Scheduling (ADR-018) | — | tjzh | Scan endpoint ✅, Decay ⬜, Heartbeat ⬜ |

## P2 — Important, Not Blocking

| ID | Status | Title | Depends On |
|----|--------|-------|------------|
| OHM-smrt | open | Stage 4 Assessment Throughput | aznh (done), wdrg |
| OHM-g0kv | open | Content Hashing & Dedup | wdrg, smrt (soft) |
| OHM-vj3i | open | Temporal Regression Detection | vrfy (soft) |
| OHM-6lvk | open | Graph Health Scoring (Doctor) | tjzh, vrfy (soft) |
| OHM-od01.4 | open | Structure Learning (Causal Discovery) | od01 (parent) |

## P3-P4 — Later

| ID | Status | Title |
|----|--------|-------|
| OHM-wdrg.7 | **in_progress** | Backfill: Hormuz source nodes |
| OHM-od01.5 | open | POMDP Decision Intelligence |
| OHM-9iyh | open | PERT Elicitation Automation |

## Milestone: multi-tenant-v2

55 issues (all P5). Client comms (SMS/Slack/Teams/Email), domain templates, cross-tenant patterns. Deferred.

## Key Metrics (2026-06-05 09:30)

- 724 nodes, 1483 edges, 394+ observations
- 158 dead-end nodes (22%) ← needs cleanup
- L3:L2 ratio: 1.8:1 ← target exceeded (was 33:1)
- Challenge ratio: 7.27% ← improving (target: >8%)
- Outcomes recorded: 31 ← verification rate 25.6%
- Unverified causal edges (>14d): 42
- High-conf nodes with no obs: 376
- Hooks enforced: cross_link_check ✅, source_url_required ✅
- Verification scan endpoint: ✅ deployed
- 1731 tests passing
