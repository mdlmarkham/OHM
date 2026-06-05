# OHM Backlog — 2026-06-05 (Final)

## Dependency Chain

```
OHM-aznh (DONE) ──→ OHM-tjzh (REVIEW) ──→ OHM-wdrg ──→ OHM-cuu0 ──→ OHM-tnwa
                       │                         │            │
                       │                         │            └── vault isolation + promotion gate
                       │                         └── source citation ✅, migration ✅
                       └── cross-link ✅, dead-end cleanup ✅
                       
                    OHM-vrfy (CLOSED) ──────────┘ 
                    
OHM-smrt (assessment throughput)
OHM-g0kv (dedup)
```

## P1 — Critical Path

| ID | Status | Title | Progress |
|----|--------|-------|----------|
| OHM-aznh | **CLOSED** | Hook Architecture | ✅ Shipped |
| OHM-tjzh | **REVIEW** | Cross-Link Constraint | Hooks enforced ✅, dead-end 158→81 ✅ |
| OHM-wdrg | open | Source Citation Architecture | L2 edges 0→712 ✅, L3:L2 33:1→1.7:1 ✅ |
| OHM-wdrg.3 | **CLOSED** | Source String Migration | All 372 obs migrated ✅ |
| OHM-cuu0 | open | Per-Agent Vaults | ⬜ (blocked on tjzh review, wdrg done) |
| OHM-tnwa | open | Gap Analysis Synthesis | ⬜ (blocked on cuu0) |
| OHM-vrfy | **CLOSED** | Verification Scheduling (ADR-018) | Scan ✅, Decay ✅, Heartbeat ✅, Migration ✅ |

## Shipped This Session

| Component | Endpoint | ADR | Purpose |
|-----------|----------|-----|---------|
| Verification scan | GET /admin/verification-scan | 018.3 | Unverified edges, high-conf no obs, source reliability |
| Verification decay | POST /admin/verification-decay | 018.3 | Unverified 30d half-life, verified 365d |
| Heartbeat nudge | POST /heartbeat | 018.1 | verification_overdue list per agent |
| Dead-end cleanup | (API) | tjzh | 158 → 81 dead ends |
| Source migration | POST /admin/observation-source-urls | 013 | 372 observations, 100% coverage |
| AGENTS.md | — | 018.1 | Verification protocol for all agents |
| ADR-018 | — | — | Status: Proposed → Accepted |

## Key Metrics

- 727 nodes, 1657 edges, 398 observations
- Dead ends: 81 (80 valid source sinks)
- L3:L2 ratio: 1.7:1 (target: 3:1 to 5:1 ✅ exceeded)
- Verification rate: 25.6%, Challenge ratio: 3.5%
- Verification decay: 50 edges decayed (43 unverified, 7 verified)
- source_url coverage: 100%
- 1731 tests passing

## Remaining Work

| Priority | Issue | What's Left |
|----------|-------|-------------|
| P1 | OHM-tjzh | Review and close — all work done |
| P1 | OHM-cuu0 | Per-agent vaults (unblocked now) |
| P1 | OHM-tnwa | Gap analysis (blocked on cuu0) |
| P2 | OHM-smrt | Assessment throughput for Stage 4 |
| P2 | OHM-g0kv | Content hashing and dedup |
| Dashboard | ADR-018.5 | Show unverified claims, reliability scores |