# PM Update — May 16, 2026 16:22 EDT

This replaces all previous PM updates. Read this first.

## STOP — Read This Before Starting Work

The backlog was restructured. Old issues (OHM-346, OHM-cxy, OHM-dn1, OHM-5zg, OHM-16s) are ALL CLOSED. Run `git pull && bd list` to see current state.

## Completed Issues (DO NOT REBUILD)

These are DONE. Do not start work on them:
- ✅ OHM-y2i.1: Error handling (commit 89e2848)
- ✅ OHM-y2i.2: Token auth (commit 2567515)
- ✅ OHM-y2i.3: Health endpoints (commit 89e2848)
- ✅ OHM-l5k: CI/CD (commit 89e2848)
- ✅ OHM-dw1: CI deps (commit 2567515)
- ✅ OHM-4w7: Dead queries.py removed (commit 2567515)
- ✅ OHM-hol: Dead query.py removed (commit 2567515)

## PM Decisions (ALL RESOLVED — nothing is blocked on PM)

You flagged 20 issues as "blocked on PM decisions." They are not blocked. Here are the answers:

| Question | Decision | Source |
|---|---|---|
| Token format | `secrets.token_urlsafe(32)`, Bearer tokens | VISION.md |
| Auth roles | Two roles: `read-write` (full) and `read-only` (GET only) | OHM-y2i.2 |
| Auth default | **FAIL CLOSED.** No tokens = deny writes. `--no-auth` flag for dev | OHM-y2i.7 |
| Quack version | Use current stable, pin in pyproject.toml | ADR-002 |
| Deployment | systemd (OHM-y2i.5) | VISION.md |
| DuckLake sync | Heartbeat-based, 5-15 min intervals | VISION.md |
| DuckLake storage | Local file per agent, shared backend | ADR-004 |
| Catalog type | DuckLake catalog (not HMS/nessie) | ADR-004 |
| Heartbeat interval | 5-15 min, configurable per agent | VISION.md |
| Confidence thresholds | Per-agent, configurable. Default 0.7 | schema.py |
| Visibility rules | L1/L2 shared, L3/L4 agent-owned, Private never shared | ADR-003 |
| Agent sync patterns | Push on heartbeat, pull on startup | VISION.md |
| Agent interface | **SDK is primary. CLI is for diagnostics only.** | VISION.md, AGENTS.md |
| Conflict resolution | Last-write-wins for L2, agent-owned for L3/L4 | VISION.md |
| Confidence decay | Yes, 30-day half-life for L4 predictions | VISION.md |
| Observation aggregation | Accumulate, don't collapse. Three observations, one node | VISION.md |
| TOPO domain | Deferred (P2). OHM first | VISION.md |
| TOPO repo | Same repo, different CLI commands | ADR-005 |
| marimo-pair | Deferred (P1). Package as Python library | VISION.md |

## Current Priority Order (what to build next)

### P0 — Security and tests (blocking team testing)

1. **OHM-y2i.6** — Parameterized queries. Replace f-string SQL interpolation with `?` placeholders. Keep validation.py as defense-in-depth. Five locations in queries/__init__.py and cli/__init__.py.
2. **OHM-y2i.7** — Auth fail-closed. Add `--no-auth` flag and `OHM_NO_AUTH` env var. Default behavior: POST requires auth. GET: auth required if tokens configured, open if no tokens. `--no-auth`: everything open (dev mode).
3. **OHM-654** — Server tests. 17 HTTP endpoints, zero coverage. Test each endpoint for happy path and error responses.
4. **OHM-5of** — Security audit (broader: TLS, rate limiting, request size cap, plaintext tokens). After y2i.6 and y2i.7 are done.

### P1 — Agent interface improvements (after P0s)

5. **OHM-a35.6** — Auto-generate node IDs from labels. Allow unicode in labels, sanitize to ascii for IDs. `create_node('AND→OR conversion')` → id=`and-or-conversion_842917`.
6. **OHM-a35.7** — Richer SDK. `create_node()` returns full dict not just ID. Add `find_or_create()`, `get_node()`, `get_edge()`, `update_confidence()`, `search()`.
7. **OHM-a35.8** — Document SDK as primary interface (already done in VISION.md and AGENTS.md).
8. **OHM-tjr** — SDK tests.
9. **OHM-ad3** — Module boundary docs (already done in AGENTS.md).
10. **OHM-n16** — Document store.py vs queries/ (already done in AGENTS.md).

## ⚠️ Known Issues

- **CI will fail** until `ruff`, `mypy`, and `pytest-benchmark` are installed in the test environment. They're in pyproject.toml dev deps now (OHM-dw1 closed) but may not be in CI's pip cache yet.
- **Benchmark tests** (test_benchmarks.py) need `pytest-benchmark` plugin. Run with `pytest tests/ --ignore=tests/test_benchmarks.py` to skip them.
- **134 tests pass** (down from 144 because query.py and queries.py tests were removed with the dead code).

## Architecture Direction (locked)

- **SDK is the primary agent interface.** CLI is for human diagnostics. ohmd is for shared HTTP access.
- **Challenge edges, not modification.** ADR-002. Locked.
- **DuckDB, not Postgres.** Locked.
- **Quack for concurrent access.** Locked.
- **Attribution on every write.** Locked.
- **Two codepaths are intentional:** `queries/` for direct-connection (CLI, SDK, tests), `store.py` for daemon (ohmd only). Do not merge them.

## What NOT To Do

- Do NOT rebuild closed issues (y2i.1, y2i.2, y2i.3, l5k, dw1, 4w7, hol)
- Do NOT add NL query parsing (removed, dead code)
- Do NOT merge store.py and queries/ (intentional separation)
- Do NOT wire agents through the CLI for regular operations
- Do NOT ask for PM decisions — they're all answered above