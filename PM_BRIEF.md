# PM Brief for Coding Agent — May 16, 2026 15:15 EDT

## STOP — Your Data Is Stale

The backlog was restructured. Issues like OHM-346, OHM-cxy, OHM-dn1, OHM-5zg, OHM-16s are **all closed**. The current backlog has different IDs. Run `git pull && bd list` to get the current state.

## Already Done (by you)

- ✅ OHM-y2i.1: Error handling — you built this in commit 89e2848
- ✅ OHM-y2i.3: Health endpoints — you built this in commit 89e2848
- ✅ OHM-l5k: CI/CD — you built this in commit 89e2848

Don't start these again. They're closed.

## PM Decisions (all answered — nothing is blocked)

You flagged 20 issues as "blocked on PM decisions." Here are the answers:

| Question | Decision | Source |
|---|---|---|
| Token format | Bearer tokens, `secrets.token_urlsafe(32)` | VISION.md, OHM-y2i.2 |
| Auth roles | Two roles: `read-write` (full) and `read-only` (GET only) | VISION.md, OHM-y2i.2 |
| Auth default | **FAIL CLOSED.** No tokens = deny writes, not grant full access | OHM-5of |
| Quack version | Use whatever is current, pin in pyproject.toml | ADR-002 |
| Deployment | systemd (OHM-y2i.5) | VISION.md |
| DuckLake sync | Heartbeat-based, 5-15 min intervals | VISION.md |
| DuckLake storage | Local file per agent, shared backend | ADR-004 |
| Catalog type | DuckLake catalog (not HMS/nessie) | ADR-004 |
| Heartbeat interval | 5-15 min, configurable per agent | VISION.md |
| Confidence thresholds | Per-agent, configurable. Default 0.7 | schema.py |
| Visibility rules | L1/L2 shared, L3/L4 agent-owned, Private never shared | ADR-003 |
| Agent sync patterns | Push on heartbeat, pull on startup | VISION.md |
| TOPO domain | Deferred (P2). OHM first. | VISION.md |
| TOPO repo | Same repo, different CLI commands | ADR-005 |
| TOPO user profile | Deferred | VISION.md |
| marimo-pair | Deferred (P1). Package as Python library. | VISION.md |

**Nothing is blocked on PM decisions.** All "PM input needed" flags are resolved.

## What To Build Next (Priority Order)

### 1. OHM-5of (P0 BUG) — Security fixes
This is the most urgent. Three things:
1. **Auth fail-closed**: When `self.tokens` is empty, deny writes instead of granting full access. Add `--no-auth` flag for development.
2. **Parameterized queries**: Replace f-string interpolation in queries/__init__.py with parameterized queries. The validation module is defense-in-depth, not a replacement.
3. **Request size limit**: Cap `_read_body` at 1MB.

### 2. OHM-y2i.2 (P0) — Token auth
Bearer tokens, two roles, `/auth` endpoint. Full acceptance criteria in `bd show OHM-y2i.2`.

### 3. OHM-654 (P0) — Server tests
17 HTTP endpoints, zero test coverage. Every endpoint needs at least a happy path and error path test.

### 4. OHM-4w7 + OHM-hol (P1) — Dead code removal
Delete `src/ohm/queries.py` (shadows package) and `src/ohm/query.py` (unused NLP parser). Five minutes of work.

### 5. OHM-dw1 (P1) — CI dependency fix
Add `ruff`, `mypy`, and `pytest-benchmark` to dev dependencies. **CI is currently broken** because test.yml runs `ruff check` and `mypy` but neither is installed.

## ⚠️ CI Is Broken

The GitHub Actions workflow you created (`test.yml`) runs `ruff check` and `mypy`, but neither is in `pyproject.toml` dev dependencies. Every push to main will fail CI until OHM-dw1 is fixed. Also `pytest-benchmark` is missing — 8 benchmark tests error on missing fixture.

## Code Review Notes (from PM)

Your commit 89e2848 was solid. Notes:

- Error handling is clean. `_map_exception_to_http` maps all 7 exception types correctly.
- Health endpoints work: `/health` (uptime), `/ready` (DuckDB check), `/status` (enhanced).
- CI workflows are well-structured but will fail until deps are added.
- Benchmark tests need `pytest-benchmark` registered in pyproject.toml markers.

One concern: **two parallel codepaths** for the same operations (`store.py` vs `queries/`). This is intentional — `store.py` is the daemon's ORM, `queries/` is the direct-connection API. See AGENTS.md "Module Boundaries" section for the full table. Don't merge them; document the boundary.