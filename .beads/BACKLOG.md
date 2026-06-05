# OHM Backlog — 2026-06-05 (Session 3)

## Dependency Chain

```
OHM-aznh (DONE) ──→ OHM-tjzh (REVIEW) ──→ OHM-wdrg ──→ OHM-cuu0 ──→ OHM-tnwa
                       │                         │            │
                       │                         │            └── vault isolation + promotion gate
                       │                         └── source citation ✅ (L3:L2 = 1.7:1)
                       └── cross-link ✅, dead-end cleanup ✅ (158→81)
                       
                    OHM-vrfy (in_progress) ────┘ (scan ✅, decay ✅, heartbeat ✅, AGENTS.md ⬜)
                    
OHM-smrt (assessment throughput)
OHM-g0kv (dedup)
```

## P1 — Critical Path

| ID | Status | Title | Progress |
|----|--------|-------|----------|
| OHM-aznh | **CLOSED** | Hook Architecture | ✅ Shipped |
| OHM-tjzh | **REVIEW** | Cross-Link Constraint | Hooks enforced ✅, dead-end 158→81 ✅, ready for review |
| OHM-wdrg | open | Source Citation Architecture | L2 edges 0→712 ✅, L3:L2 33:1→1.7:1 ✅ |
| OHM-wdrg.3 | **CLOSED** | Source String Migration | All 372 observations migrated ✅ |
| OHM-cuu0 | open | Per-Agent Vaults | ⬜ (blocked on tjzh, wdrg) |
| OHM-tnwa | open | Gap Analysis Synthesis | ⬜ (blocked on cuu0, tjzh) |
| OHM-vrfy | **in_progress** | Verification Scheduling (ADR-018) | Scan ✅, Decay ✅, Heartbeat ✅, AGENTS.md ⬜ |

## Key Metrics (2026-06-05 11:00)

- 727 nodes, 1657 edges, 398 observations
- Dead ends: 81 (80 valid source sinks, 1 system node)
- L3:L2 ratio: 1.7:1 ← exceeded 3:1 target
- Challenge ratio: 3.5%
- Verification rate: 25.6% (31 outcomes on 121 causal edges)
- Verification decay: 50 edges decayed first run (43 unverified, 7 verified)
- Heartbeat: returns verification_overdue list per agent
- source_url coverage: 375/372 (100% of sources with source field)
- 1731 tests passing

## Shipped This Session

| Component | Endpoint | Purpose |
|-----------|----------|---------|
| Verification scan | GET /admin/verification-scan | Unverified edges, high-conf no obs, source reliability |
| Verification decay | POST /admin/verification-decay | ADR-018.3: unverified edges decay 30d half-life |
| Heartbeat integration | POST /heartbeat | verification_overdue list per agent |
| Dead-end cleanup | (API) | 158 → 81 dead ends (semantic connections + manual) |
| Source URL migration | POST /admin/observation-source-urls | 243 observations migrated (100% coverage) |

## Remaining

1. OHM-vrfy: AGENTS.md update (verification protocol for all agents)
2. OHM-smrt: Increase assessment throughput for Stage 4
3. OHM-cuu0: Per-agent vaults (unblocked once tjzh reviewed)
4. OHM-tnwa: Gap analysis synthesis (unblocked once cuu0 done)