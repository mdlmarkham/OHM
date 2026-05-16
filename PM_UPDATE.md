# PM Update — May 16, 2026 14:40 EDT

For the coding agent: the backlog has been restructured since you last checked.

## Completed (by you, commit 89e2848)
- ✅ OHM-y2i.1: Error handling with correlation IDs
- ✅ OHM-y2i.3: Health check and status endpoints (/health, /ready, /status)
- ✅ OHM-l5k: GitHub Actions CI/CD (test.yml + release.yml)

## PM Decisions (all in VISION.md)
- **Deployment:** systemd (OHM-y2i.5)
- **Token auth:** Bearer tokens, two roles (read-write, read-only) (OHM-y2i.2)
- **NL query:** Removed. query.py is dead code (OHM-hol)
- **DuckLake sync:** Heartbeat-based, 5-15 min intervals
- **Conflict resolution:** Last-write-wins for L2, agent-owned for L3/L4
- **Confidence decay:** Yes, 30-day half-life for L4 predictions
- **Observation aggregation:** Accumulate, don't collapse

## ⚠️ CI Will Fail
The test.yml workflow runs `ruff check` and `mypy`, but neither is in pyproject.toml dev dependencies.
Also `pytest-benchmark` is missing (8 benchmark tests error on missing fixture).
**OHM-dw1** tracks this — add ruff, mypy, pytest-benchmark to dev deps.

## Dead Code
- `src/ohm/queries.py` (top-level) — unreachable, shadows queries/ package. Remove (OHM-4w7)
- `src/ohm/query.py` — NLP parser never used by production code. Remove (OHM-hol)

## Module Boundaries
See AGENTS.md for the full table. Key point:
- `queries/` = direct-connection API (used by CLI, SDK, tests)
- `store.py` = ORM wrapper for ohmd daemon (used by server.py only)
- `sdk.py` = agent-facing Python API (wraps queries/)
- `server.py` = HTTP daemon (uses store.py, not queries/)

## Next Priority
1. OHM-dw1: Fix CI deps (blocking all future PRs)
2. OHM-y2i.2: Token auth
3. OHM-654: Server tests (zero coverage for 17 HTTP endpoints)