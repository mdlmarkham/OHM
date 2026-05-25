# ADR-016: Quack Production Readiness Criteria

**Date:** 2026-05-25
**Status:** Decided

## Context

DuckDB's Quack protocol (HTTP-based client-server, multi-reader/multi-writer) is implemented in `src/ohm/graph/quack.py` and tested via `tests/test_quack.py`. The current OHM architecture uses per-tenant write mutexes (single-writer) via `OhmStore` with a `threading.Lock`. Quack offers concurrent multi-writer access but requires careful activation criteria before production use.

## Decision

Quack mode may be activated in production when ALL of the following criteria are met:

### 1. Extension Stability

| Criterion | Threshold | Verification |
|-----------|-----------|--------------|
| Quack available in DuckDB stable | Yes — DuckDB ≥ 1.1.0 | `quack.is_available()` returns True without nightly |
| No known critical bugs in Quack | Confirmed via DuckDB release notes | Check DuckDB changelog for Quack fixes |
| Extension loads reproducibly | 10/10 consecutive loads succeed | Integration test `test_quack_concurrent_load` |

Currently, Quack ships from `core_nightly` only. Migration to stable is required before production activation.

### 2. Security Requirements

| Criterion | Threshold | Verification |
|-----------|-----------|--------------|
| Token auth | 32+ char token, stored in env var | `QUACK_TOKEN` env var set; short tokens rejected |
| TLS proxy in front of Quack | Enabled in production | Traefik/nginx terminates TLS; Quack URI uses localhost only |
| No inline tokens in SQL | All tokens via DuckDB secrets | `quack.query_remote()` uses parameterized queries only |
| Secret scope validation | Scope string validated before use | `validate_quack_uri()` and `validate_quack_token()` reject injection chars |

### 3. Test Coverage

| Criterion | Threshold | Verification |
|-----------|-----------|--------------|
| Concurrent write test | 10 agents, 50 writes each, 0 data loss | `test_ten_agents_concurrent_writes_no_data_loss` passes |
| Token auth test | Invalid token rejected, valid token accepted | `test_quack_auth_rejects_invalid_token` passes |
| Deadlock-free test | 100 concurrent reads + writes, no timeout | `test_concurrent_read_write_no_deadlock` passes |
| Graceful degradation | Quack unavailable → falls back to mutex | `test_quack_unavailable_falls_back` passes |

### 4. ohmd Configuration

Activation is controlled by the `quack` config flag (default: `false`):

```json
{
  "quack": true,
  "quack_uri": "quack:localhost",
  "quack_token_env": "QUACK_TOKEN"
}
```

When `quack: true`, the daemon:
1. Loads the Quack extension via `START QUACK FROM core_nightly`
2. Starts the Quack server on `quack_uri`
3. Registers token auth via DuckDB secrets
4. Falls back to single-writer mutex if Quack is unavailable

### 5. Activation Path

```
Phase 1 (current): Single-writer mutex, Quack disabled
  └── ohm.quack.is_available() → False (no production activation)

Phase 2 (pre-production): Quack on non-production instance
  └── DEV_QUACK=true ohmd --quack
  └── Verify concurrent writes work, no deadlocks

Phase 3 (staged rollout): Quack on single tenant
  └── quack:true in tenant-specific config
  └── Monitor: error rate, write latency, deadlock events

Phase 4 (GA): Quack enabled by default for multi-tenant
  └── quack defaults to true when multi_tenant:true
  └── Remove mutex path for Quack-capable deployments
```

## Consequences

- **Opt-in only**: Quack mode requires explicit `quack: true` in config
- **Fallback preserved**: If Quack extension fails to load, daemon continues with mutex-based single-writer
- **No automatic upgrade**: Must manually migrate from mutex to Quack per deployment
- **Monitoring required**: Quack mode should emit metrics for concurrent access contention

## Notes

- OHM's per-tenant mutex (`TenantManager._locks`) is the current production path
- DuckDB CE has no native encryption at rest — see OHM-yl1f for HIPAA requirements
- The concurrent write test (`test_ten_agents_concurrent_writes_no_data_loss`) currently fails — this is a known gap blocking Quack activation