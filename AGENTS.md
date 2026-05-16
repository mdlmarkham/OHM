# Agent Instructions — OHM

**Shared awareness, individual judgment.** Multi-agent knowledge graph built on DuckDB + recursive CTEs.

## Quick Start

```bash
# Install (Python 3.12+)
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Run CLI
ohm graph schema
ohm graph layers

# Or via module
python -m ohm.cli graph schema
```

## Project Architecture

OHM is a Python package under `src/ohm/` with a `src`-layout. The CLI is the primary interface — agents interact with the graph through `ohm` commands, not raw SQL.

```
src/ohm/
├── __init__.py          # Package metadata, version
├── exceptions.py        # Exception hierarchy with exit codes (0-5)
├── schema.py            # DDL, validation, edge-type/layer constraints
├── db.py                # DuckDB connection lifecycle, schema init
├── cli/__init__.py      # Full argparse command tree (serve, graph, state, snapshot, diff)
└── queries/__init__.py  # 7 parameterized CTE query functions
tests/
├── conftest.py          # Fixtures: test_db, sample_graph_small/medium/large
├── test_schema.py       # Schema validation + DDL execution tests
├── test_cli.py          # CLI argument parsing tests (23 commands)
├── test_queries.py      # CTE query correctness tests (18 scenarios)
└── test_exceptions.py   # Error type + exit code tests
```

**Key design decisions** (see [docs/adr/](docs/adr/README.md)):
- **ADR-001**: Recursive CTEs over DuckPGQ for graph traversal (zero-dependency, survives DuckDB upgrades)
- **ADR-002**: Quack protocol for concurrent multi-agent access (requires `ohmd` daemon)
- **ADR-003**: Agent-owned edges with challenge semantics (no overwrites, CHALLENGED_BY/SUPPORTS edges)
- **ADR-004**: Three-layer data architecture (local DuckDB cache → DuckLake shared backend → private)
- **ADR-005**: Self-documenting CLI as agent interface (agents call `ohm`, not raw SQL)

## Conventions

### Type Hints
All public functions use full type hints with `from __future__ import annotations`. DuckDB connection is typed as `DuckDBPyConnection` behind `TYPE_CHECKING`.

### Error Handling
Use the exception hierarchy in `src/ohm/exceptions.py`. Every error has an exit code:
- `OHMError` (1) → `DaemonNotRunningError` (2), `AuthenticationError` (3), `PermissionDeniedError` (4), `NodeNotFoundError` (5), `EdgeNotFoundError` (5), `ValidationError` (1), `ConfigurationError` (1)

Include `correlation_id` for debugging. CLI dispatcher catches `OHMError` and exits with the correct code.

### DuckDB Patterns
- **Schema init**: Call `initialize_schema(conn)` — idempotent, uses `CREATE TABLE IF NOT EXISTS`
- **Row conversion**: Use `_rows_to_dicts(result)` from queries module — DuckDB returns tuples, not dicts
- **No foreign keys**: DuckDB doesn't support `REFERENCES` constraints. Enforce referential integrity in application layer
- **Arrays as TEXT**: DuckDB has limited array support in CTEs. Store lists as delimited strings where needed
- **Extensions**: Load `json` extension on connect. Others loaded on demand

### Testing
- All tests use in-memory DuckDB (`:memory:`) — no file I/O
- Fixtures in `conftest.py` provide pre-built graphs at three sizes
- Test functions follow `test_<scenario>` naming
- Each test gets a fresh database via the `test_db` fixture

### Imports
- Absolute imports from `ohm.*` (e.g., `from ohm.schema import initialize_schema`)
- Standard library → third-party → local ordering
- `from __future__ import annotations` at top of every module

## Beads Workflow

This project uses **bd** (beads) for issue tracking. Issues use prefix `ohm-<hash>`.

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd create "title" -t feature -p 0 --parent <epic-id>  # Create child issue
```

### Backlog Structure
Active epics (use `bd list` for current state):
- **OHM-y2i**: P0 — Production Daemon (auth, Quack, error handling, systemd)
- **OHM-xgm**: P1 — DuckLake + Time Travel (shared backend, snapshot, diff, heartbeat sync)
- **OHM-a35**: P1 — Agent Integration (Métis, Clio, Hephaestus, Socrates, agent config)
- **OHM-3w1**: P2 — TOPO Instantiation (industrial schema, CLI, shared daemon)
- **OHM-l5k**: P1 — CI/CD pipeline
- **OHM-xj4**: P1 — marimo-pair integration
- **OHM-dy9**: P2 — Performance benchmarks

Issues tagged with **PM input needed** are blocked on product decisions. Issues without that tag are actionable now.

### Session Completion (MANDATORY)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

1. **File issues for remaining work** — Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed):
   ```bash
   python -m pytest tests/ -v
   python -m ruff check src/ tests/
   python -m mypy src/
   ```
3. **Update issue status** — Close finished work, update in-progress items
4. **PUSH TO REMOTE** — This is MANDATORY:
   ```bash
   git pull --rebase
   git add .beads/ && git commit -m "chore: sync beads state" || true
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** — Clear stashes, prune remote branches
6. **Verify** — All changes committed AND pushed
7. **Hand off** — Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing — that leaves work stranded locally
- NEVER say "ready to push when you are" — YOU must push
- If push fails, resolve and retry until it succeeds

## Security

- **Never hardcode credentials.** Read from environment variables or config files
- **Token auth**: Agent tokens passed via `--actor` flag or `$OHM_ACTOR` env var
- **Boundary enforcement**: No agent can overwrite another agent's L3/L4 edges (ADR-003)
- **SQL injection**: All user-provided values in CTE queries must be validated before interpolation. Use parameterized queries where DuckDB supports them
- **File permissions**: DuckDB files and config should be readable only by the agent user

## Common Pitfalls

1. **DuckDB doesn't support `REFERENCES` constraints.** Don't add foreign keys to DDL — enforce in application code
2. **DuckDB `fetchall()` returns tuples, not dicts.** Always use `_rows_to_dicts()` from queries module
3. **Recursive CTEs can't reference themselves in subqueries.** Keep CTE logic simple — avoid `NOT EXISTS (SELECT FROM cte)` patterns
4. **`bd sync` works fine** — it exports issues to `.beads/issues.jsonl` and is git-tracked
5. **Use `bd doctor`** to diagnose daemon issues; `bd list` and `bd show` for status
6. **The `pyproject.toml` was converted from pixi format to PEP 621.** Don't revert to pixi-style `[package]`/`[dependencies]` sections


