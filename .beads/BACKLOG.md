# OHM Backlog — 2026-06-05

## Dependency Chain

```
OHM-aznh (DONE) ──→ OHM-tjzh ──→ OHM-wdrg ──→ OHM-cuu0 ──→ OHM-tnwa
                       │               │              │
                       │               │              └── vault isolation + promotion gate
                       │               └── source citation enforcement
                       └── cross-link constraint (blocks everything downstream)
                       
                    OHM-vrfy ──────────┘ (verification scheduling, depends on tjzh only)
                    
OHM-smrt (assessment throughput, depends on wdrg)
OHM-g0kv (dedup, soft-depends on smrt)
OHM-6lvk (doctor, depends on tjzh, soft-depends vrfy)
OHM-vj3i (trajectory, soft-depends vrfy)
```

## P1 — Critical Path

| ID | Status | Title | Blocks | Depends On |
|----|--------|-------|--------|------------|
| OHM-aznh | **CLOSED** | Hook Architecture | — | — |
| OHM-tjzh | **in_progress** | Cross-Link Constraint | cuu0, tnwa | aznh (done) |
| OHM-wdrg | open | Source Citation Architecture | cuu0, smrt | — |
| OHM-cuu0 | open | Per-Agent Vaults + Promotion Gate | tnwa | tjzh, wdrg |
| OHM-tnwa | open | Gap Analysis Synthesis | — | cuu0, tjzh |
| OHM-vrfy | open | Verification Scheduling (ADR-018) | — | tjzh |

**Next session priority:**
1. Close tjzh (hooks deployed, need enforcement-by-default + orphan cleanup)
2. Push wdrg.3 and wdrg.7 (in-progress, mechanical)
3. Start vrfy (verification scan endpoint + confidence decay engine)

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

55 issues (all P5). Client comms (SMS/Slack/Teams/Email), domain templates, cross-tenant patterns. Deferred until core graph is stable. Not current priority.

## Key Metrics (2026-06-05)

- 711 nodes, 1215 edges, 394 observations
- 148 dead-end nodes (21%)
- 33:1 L3:L2 edge ratio (target: 3:1 to 5:1)
- 4.1% challenge ratio (target: >8%)
- 0 outcomes recorded (target: >10)
- 1731 tests passing