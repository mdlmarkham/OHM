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
├── validation.py        # Input validation (SQL injection prevention for CTE identifiers)
├── boundary.py          # Layer ownership enforcement (ADR-003)
├── quack.py             # Quack protocol integration (concurrent multi-writer access)
├── store.py             # OhmStore ORM wrapper — used by ohmd ONLY
├── sdk.py               # Python SDK for agent programmatic access
├── server.py            # ohmd HTTP daemon — uses OhmStore, not queries/
├── cli/
│   ├── __init__.py      # Full argparse command tree (serve, graph, state, snapshot, diff)
│   └── __main__.py      # `python -m ohm.cli` entry point
├── methods.py           # Substrate methods: aggregation, anomalies, Monte Carlo, etc.
├── queries/
│   └── __init__.py      # Parameterized CTE query functions (direct-connection API)
tests/
├── conftest.py          # Fixtures: test_db, sample_graph_small/medium/large
├── test_schema.py       # Schema validation + DDL execution tests
├── test_exceptions.py   # Error type + exit code tests
├── test_boundary.py     # Layer ownership enforcement tests
├── test_queries.py      # CTE query correctness tests
├── test_cli.py          # CLI argument parsing tests (23 commands)
├── test_cli_integration.py  # End-to-end CLI tests against real DB
├── test_ohm.py           # OhmStore integration tests
├── test_integration.py   # Full workflow integration tests
├── test_server.py        # HTTP daemon endpoint tests
├── test_quack.py         # Quack protocol integration tests
└── test_topo_cli.py      # TOPO CLI tests
```

**603+ tests passing** across all modules.
```

### Module Boundaries

Two codepaths exist for the same operations. This is intentional:

| Module | Role | Used by | Direct dependency |
|--------|------|---------|-----------------|
| `queries/__init__.py` | Direct-connection API — functions take a DuckDBPyConnection | CLI, SDK, tests | `boundary.py`, `validation.py` |
| `store.py` (OhmStore) | ORM wrapper — manages its own connection and schema init | `server.py` (ohmd) only | DuckDB directly |
| `sdk.py` (Graph) | Agent-facing Python API — wraps `queries/` with context manager | Agents | `queries/`, `db.py` |
| `server.py` (ohmd) | HTTP daemon — uses OhmStore | External HTTP clients | `store.py` |

**When adding a new operation:**
- If agents call it: add to `queries/` first, then wrap in `sdk.py`
- If the daemon calls it: add to both `queries/` and `store.py` (or refactor server.py to use queries/)
- **Never** add to `store.py` without also adding to `queries/`

**Key design decisions** (see [docs/adr/](docs/adr/README.md)):
- **ADR-0001**: Architecture decisions compendium (DuckDB local cache, challenge edges, JSON arrays, timestamps, CLI-first, advisory schema)
- **ADR-0007**: Schema evolution and type governance for domain expansion
- **ADR-008** (inline): Probability and Confidence as separate edge attributes (confidence = belief, probability = likelihood)
- **ADR-009** (inline): NEGATES edge type for negative evidence (semantically distinct from CHALLENGED_BY)
- **ADR-010** (inline): Urgency ≠ priority (urgency = time-sensitivity on edges, priority = importance on nodes)
- **ADR-011** (inline): Observation type extensibility (domain-specific types without DDL migrations)

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

## Primary Agent Interface

**Agents should use the Python SDK (`ohm.sdk`), not the CLI.** The CLI is for human diagnostics and ad-hoc exploration, not agent operations.

```python
# Correct: SDK for agent operations
import ohm.sdk as ohm
with ohm.connect("~/.ohm/ohm.duckdb", actor="metis") as graph:
    node = graph.create_node("AND→OR conversion", node_type="pattern")
    graph.create_edge(from_node=node, to_node=target, edge_type="CAUSES", layer="L3")

# Incorrect: shelling out to CLI for every operation
import subprocess
subprocess.run(["ohm", "graph", "write", "--from", node, ...])
```

Why: The SDK runs in-process (no subprocess overhead), returns structured data (no text parsing), and supports batch operations. The CLI spawns a new process per command and returns text that must be parsed.

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
- **OHM-0e0**: P1 — Domain Flexibility ✅ (Complete: cattle, retail, temporal decay, SSE, batch expiry)
- **OHM-af8**: P1 — Multi-scenario Extensibility ✅ (Complete: medical, cybersecurity, supply chain, customer support)
- **OHM-xgm**: P1 — DuckLake + Time Travel (future)
- **OHM-a35**: P1 — Agent Integration (Métis, Clio, Hephaestus, Socrates) (future)
- **OHM-3w1**: P2 — TOPO Instantiation (future)

Schema v0.5.0 shipped: urgency, priority, probability, NEGATES, scenario edge types.

Remaining docs/P2 items (use `bd list` for current state).

### Session Completion (MANDATORY)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

1. **File issues for remaining work** — Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed):
   ```bash
   python -m pytest tests/ -v
   ```
   Linting/type-checking tools (ruff, mypy) are not yet in the project. Skip them for now.
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
- **Token auth**: Agent tokens in `/etc/ohm/ohmd.json` or `$OHM_TOKEN` env var. Bearer tokens for HTTP API.
- **Boundary enforcement**: No agent can overwrite another agent's L3/L4 edges (ADR-003)
- **SQL injection**: All user-provided values in CTE queries are validated. Parameterized queries used where DuckDB supports them.
- **File permissions**: `/etc/ohm/ohmd.json` (600), `/var/lib/ohm/` (root:root)
- **Request size**: POST bodies have no cap yet (OHM-zag). Be cautious with large payloads.

## Deployment

OHM runs as a systemd service:

```bash
# Check status
systemctl status ohmd

# Restart
sudo systemctl restart ohmd

# Logs
journalctl -u ohmd -f

# Config
cat /etc/ohm/ohmd.json

# Agent tokens
cat /root/olympus/shared/ohm-config.json
```

Agents connect via HTTP API on `127.0.0.1:8710`:

```python
import requests
headers = {"Authorization": "Bearer ohm-metis-u0-KEjbnU_WfJnmNq7rbzQ"}
response = requests.get("http://127.0.0.1:8710/stats", headers=headers)
```

Or via SDK (when daemon is stopped, for reads):

```python
from ohm.sdk import connect
with connect("/var/lib/ohm/ohm.duckdb", actor="metis") as g:
    stats = g.stats()
```

## Common Pitfalls

1. **DuckDB doesn't support `REFERENCES` constraints.** Don't add foreign keys to DDL — enforce in application code
2. **DuckDB `fetchall()` returns tuples, not dicts.** Always use `_rows_to_dicts()` from queries module
3. **Recursive CTEs can't reference themselves in subqueries.** Keep CTE logic simple — avoid `NOT EXISTS (SELECT FROM cte)` patterns
4. **`bd sync` works fine** — it exports issues to `.beads/issues.jsonl` and is git-tracked
5. **Use `bd doctor`** to diagnose daemon issues; `bd list` and `bd show` for status
6. **The `pyproject.toml` was converted from pixi format to PEP 621.** Don't revert to pixi-style `[package]`/`[dependencies]` sections



<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ccf33ec3 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
