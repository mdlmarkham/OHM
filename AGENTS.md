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

OHM is a Python package under `src/ohm/` with a `src`-layout. The package is
organised by responsibility: graph substrate (DuckDB access + CTE queries),
inference engines (Bayesian, Markov, PERT, causal, hyperdimensional), the
HTTP daemon (`ohmd`), the agent SDK, document/ingestion pipelines, decision
support, and integrations.

```
src/ohm/
├── __init__.py          # Package metadata, version
├── exceptions.py        # Exception hierarchy with exit codes (0-5)
├── schema.py            # DDL, validation, edge-type/layer constraints
├── db.py                # DuckDB connection lifecycle, schema init
├── validation.py        # Input validation (SQL injection prevention for CTE identifiers)
├── boundary.py          # Layer ownership enforcement (ADR-003)
├── contract.py          # Wire-format contracts (request/response shapes)
├── client.py            # Outbound HTTP client (connect to ohmd)
├── quack.py             # Quack protocol integration (concurrent multi-writer access)
├── store.py             # OhmStore ORM wrapper — used by ohmd ONLY
├── sdk.py               # Python SDK for agent programmatic access
├── tenant.py            # Multi-tenancy helpers (ADR-015)
├── methods.py           # Substrate methods: aggregation, anomalies, Monte Carlo, etc.
├── bayesian.py          # Bayesian inference (delegates to inference/bayesian.py)
├── causal_refutation.py # Causal refutation (delegates to inference/)
├── markov.py            # Markov chain analysis (delegates to inference/)
├── pert.py              # PERT/CPM scheduling (delegates to inference/)
├── hd.py                # Hyperdimensional fingerprinting (delegates to inference/)
├── game.py              # Game theory (delegates to inference/)
├── patterns.py          # Pattern detection helpers
├── evidence.py          # Evidence aggregation
├── hooks.py / hooks_builtin.py  # Extension hooks
├── ingest.py            # Top-level ingest entry point
├── integrations.py      # Re-export of integrations/ package
├── utils.py             # Shared utilities
├── visualization.py     # Graph visualisation helpers
├── semantic_roles.py    # Semantic role labelling
├── marimo_pair.py       # Marimo notebook pair integration
├── metis_bridge.py      # Métis (planning agent) integration
├── graph_reader.py      # Read-only graph reader
├── bos/                 # Business Operating System (ODPS data product catalog)
├── cli/                 # Full argparse command tree (`ohm` entry point)
│   ├── __init__.py
│   └── __main__.py
├── decision/            # Recommendation engine
│   └── recommendation.py
├── documents/           # Document store + extraction + ingestion
│   ├── store.py
│   ├── extract.py
│   └── ingest.py
├── framework/           # Agent-facing SDK + supporting libs (NEW canonical SDK home)
│   ├── sdk.py           # Canonical Graph class for agents
│   ├── client.py
│   ├── validation.py
│   ├── graph_reader.py
│   ├── exceptions.py
│   ├── ingest.py
│   ├── integrations.py
│   ├── semantic_roles.py
│   ├── metis_bridge.py
│   └── marimo_pair.py
├── graph/               # Substrate: DuckDB queries, methods, embeddings, decay
│   ├── db.py
│   ├── queries/__init__.py  # ~12k lines — the canonical query module
│   ├── methods.py
│   ├── embeddings.py
│   ├── decay.py
│   ├── calibration.py
│   ├── constraints.py
│   ├── crypto.py
│   └── quack.py
├── inference/           # CPU-bound analytics engines (pure-Python + numpy/scipy)
│   ├── bayesian.py
│   ├── causal_refutation.py
│   ├── discovery.py
│   ├── evidence.py
│   ├── game_theory.py
│   ├── hd.py
│   └── markov.py
├── ingestion/           # Document tree ingestion pipeline
│   ├── pipeline.py
│   ├── document_tree.py
│   ├── document_tree_ingest.py
│   └── document_library_bridge.py
├── integrations/        # External system integrations
│   ├── __init__.py
│   └── beads_sync.py
├── mcp/                 # Model Context Protocol server
│   └── server.py
├── queries/             # Shim re-exporting ohm.graph.queries (backward compat)
│   ├── __init__.py
│   └── hypothesis_tree.py
├── semantic_layer/      # Semantic auto-linking + actions
│   ├── engine.py
│   └── actions.py
└── server/              # ohmd HTTP daemon
    ├── server.py        # ~3k lines — stdlib HTTP server + handler mixins
    ├── contract.py
    ├── boundary.py
    ├── ask_router.py
    ├── suggestions.py
    ├── nudges.py
    ├── relational_tags.py
    ├── visualization.py
    └── handlers/        # Per-resource HTTP handlers
        ├── graph.py, inference.py, decision.py, catalog.py,
        ├── analysis.py, admin.py, ask.py, documents.py,
        ├── infra.py, markov.py, tenant.py
tests/
├── conftest.py          # Fixtures: test_db, sample_graph_*, OHM_DISABLE_* toggles
├── test_hd.py           # Hyperdimensional fingerprinting (57 tests)
├── test_queries.py      # CTE query correctness
├── test_integration.py  # End-to-end workflow
├── test_server.py       # HTTP daemon endpoint tests
├── test_cli.py / test_cli_integration.py / test_topo_cli.py
├── test_sdk.py / test_ohm.py
├── test_*.py            # 100+ module-specific test files (see tests/)
└── test_integrations.py # ⚠️ currently broken (pre-existing import error)
```

**~2,480 tests collected** (excluding the pre-existing broken `test_integrations.py`).
Run with: `python -m pytest tests/ --ignore=tests/test_integrations.py`.

```

### Module Boundaries

Three codepaths exist for the same operations. This is intentional:

| Module | Role | Used by | Direct dependency |
|--------|------|---------|-----------------|
| `graph/queries/__init__.py` | **Canonical** direct-connection API — functions take a `DuckDBPyConnection` | CLI, SDK, tests | `boundary.py`, `validation.py` |
| `queries/__init__.py` | Backward-compat shim re-exporting `graph.queries` | legacy imports | (shim) |
| `store.py` (OhmStore) | ORM wrapper — manages its own connection and schema init | `server/server.py` (ohmd) only | DuckDB directly |
| `framework/sdk.py` (Graph) | **Canonical** agent-facing Python API — context-manager Graph class | Agents | `graph/queries/`, `db.py` |
| `sdk.py` (top-level) | Older agent SDK — kept for backward compat | legacy agents | `queries/`, `db.py` |
| `server/server.py` (ohmd) | HTTP daemon — uses OhmStore | External HTTP clients | `store.py` |

**When adding a new operation:**
- If agents call it: add to `graph/queries/` first, then wrap in `framework/sdk.py`
- If the daemon calls it: add to both `graph/queries/` and `store.py` (or refactor `server/` to use `graph/queries/`)
- **Never** add to `store.py` without also adding to `graph/queries/`

**Key design decisions** (see [docs/adr/](docs/adr/README.md), 26 ADRs indexed):
- **ADR-0001**: Architecture decisions compendium (DuckDB local cache, challenge edges, JSON arrays, timestamps, CLI-first, advisory schema)
- **ADR-0002**: Quack protocol for concurrent access
- **ADR-0003**: Agent-owned edges with challenge semantics
- **ADR-0004**: Three-layer data architecture (per-agent cache, shared DuckLake, private scratch)
- **ADR-0005**: Self-documenting CLI as agent interface
- **ADR-0006**: Advisory schema with graduated enforcement
- **ADR-0007**: Schema evolution and type governance for domain expansion
- **ADR-0008**: Probability and Confidence as separate edge attributes (confidence = belief, probability = likelihood)
- **ADR-0009**: NEGATES edge type for negative evidence (semantically distinct from CHALLENGED_BY)
- **ADR-0010**: Urgency on edges and priority on nodes
- **ADR-0011**: Observation type extensibility
- **ADR-0012**: Per-agent local DuckDB cache
- **ADR-0013**: Value of Information for knowledge graphs
- **ADR-0015**: Multi-tenancy via single-process isolated DuckDB instances
- **ADR-0018**: Cross-link requirement for derived-claim nodes (writing protocol enforced by `ohm-tjzh`)
- **ADR-0028**: Source tier architecture and confidence ceilings
- **ADR-0030**: Oppositional review pipeline
- **ADR-0031**: Hyperdimensional fingerprinting prototype
- **ADR-0032**: HD membership layer (persistent fingerprints in DuckDB)
- **ADR-0035**: TELOS signing — cryptographic audit trail
- **ADR-0037**: Per-agent read scopes and temporal pinning
- **ADR-0039**: Bedrock knowledge store (write-through for managed embeddings)

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

## Sub-Agent Delegation

**Delegate as often as practical.** OHM work falls into recurring shapes (research, schema, plumbing, tests, ADRs, search) that map onto specialized sub-agents. Default to dispatching a sub-agent rather than doing the work inline — it preserves the primary's context, runs in parallel, and is billed against Synthetic quota (not the tight OpenCode-Go `$12/5hr` window), so dispatch is cheap. Do the orchestrating; let sub-agents do the grinding.

### When to dispatch which sub-agent

| Trigger | Sub-agent | Notes |
|---|---|---|
| Need to understand existing patterns / find where code lives before changing it | `ohm-researcher` | Read-only, reports `file:line` + snippets. Dispatch **before** implementing. |
| Fast find-by-pattern or keyword search across the codebase | `explore` | Read-only, cheap, high-volume. Prefer over manual grep/glob for non-trivial searches. |
| Multi-step research or execution the primary shouldn't context-switch for | `general` | Code-capable. Use for background work the primary doesn't need to drive. |
| Adding a column / `VALID_*` frozenset / `validate_*` / migration | `ohm-schemer` | Owns `SCHEMA_VERSION` bumps + idempotent `ALTER TABLE`. |
| Wiring a field/operation through queries → store → SDK → handler | `ohm-plumber` | Mirrors the 4-layer pattern; preserves backward compat (None defaults). |
| A feature needs test coverage | `ohm-test-writer` | Follows `tests/test_*.py` patterns; runs pytest to verify. |
| A design decision must be captured | `ohm-adr-writer` | Writes `docs/adr/NNNN-*.md`, updates the README index. |

### Delegation rules

- **Parallelize independent work.** Send multiple Task calls in one message when the work is independent (e.g., a wave of research agents, one per issue). This is the highest-leverage habit — N sub-agents finish in the time of one.
- **Research before implement.** For any non-trivial change, dispatch `ohm-researcher` first; implement from its findings. Skipping this is how conventions get broken.
- **Sequence dependent work.** Schema (`ohm-schemer`) → plumbing (`ohm-plumber`) → tests (`ohm-test-writer`) → ADR (`ohm-adr-writer`) is the typical order for a new field. Don't run tests before the plumbing lands.
- **Don't duplicate delegated work.** Once a sub-agent is dispatched for a unit of work, the primary should move to non-overlapping work or wait — don't redo the same investigation inline.
- **Verify, don't trust.** Sub-agent output is generally reliable but should be checked against the codebase for high-stakes changes. Run the quality gates (`python -m pytest tests/ -v`) after implementation agents finish.
- **The primary still owns the graph + GitHub issues.** Sub-agents do not file issues, claim/close work, commit, or push — only the primary does.

### Model routing

All project sub-agents run on **Synthetic** models (HuggingFace-hosted via the Synthetic provider), preserving the OpenCode-Go budget for the primary agent. Routing lives in `.opencode/agents/*.md` (frontmatter `model:`) and `.opencode/opencode.json` (built-in overrides); see `.opencode/agents/README.md` for the full table. **Restart opencode** after editing routing — config loads once at startup.

## Decision Policy — Phase 1 POMDP (OHM-od01.5)

When facing a decision, the agent should ask the policy endpoint whether to **observe** (gather more information) or **act** (exploit current belief).

```python
import ohm.sdk as ohm
with ohm.connect("~/.ohm/ohm.duckdb", actor="metis") as graph:
    policy = graph.policy("hormuz_and_gate", horizon=1, observation_cost=0.5)
    if policy["recommendation"] == "observe":
        # Reduce uncertainty — gather observation on top candidate
        target = policy["top_voi_candidates"][0]["node_id"]
        observation = graph.observe(target)
    else:
        # EVPI does not justify the cost — act on current belief
        graph.act_on("hormuz_and_gate")
```

Under the hood: `GET /policy?target=<node_id>&horizon=1&observation_cost=0.5`

Returns:
- `method`: `"belief_state_policy"` (Phase 1 POMDP)
- `recommendation`: `"observe"` or `"act"`
- `confidence`: 0-1 score
- `evpi`: Expected Value of Perfect Information (utility units)
- `cost_of_observation`: the threshold used
- `current_belief`: `{"good": 0.7, "bad": 0.3}` (Bayesian posterior on target)
- `top_voi_candidates`: list of `{"node_id", "voi_score"}` — best observations
- `reasoning`: human-readable explanation

Decision rule (Phase 1, single-step POMDP): if `evpi > cost_of_observation` → observe (explore); else → act (exploit). Phase 2 (factored POMDP with PBVI) and Phase 3 (multi-agent POMDP) are tracked under OHM-od01.5 and are P4+ scope.

CLI equivalent: `ohm graph policy <node_id> [--horizon 1] [--observation-cost 1.0]`.

## Issue Tracker (GitHub)

This project uses **GitHub Issues** for task tracking.

```bash
gh issue list --repo mdlmarkham/OHM --state open --assignee @me  # My open issues
gh issue view <number> --repo mdlmarkham/OHM                     # View issue details
gh issue edit <number> --repo mdlmarkham/OHM --add-label "in-progress"  # Claim work
gh issue close <number> --repo mdlmarkham/OHM                  # Complete work
gh issue create --repo mdlmarkham/OHM --title "..." --body "..." --label "bug|enhancement|task"  # New issue
```

### Backlog Structure

Track active work in GitHub Issues and Projects. Use the issue list for the current state rather than a static table.

Remaining docs/P2 items: see open issues tagged `documentation` and `type::task`.

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
2. **DuckDB `fetchall()` returns tuples, not dicts.** Use `_rows_to_dicts()` from `ohm.graph.queries` (benchmark confirmed this is faster than `result.df().to_dict('records')` without pyarrow; don't switch to the pandas bridge).
3. **Recursive CTEs can't reference themselves in subqueries.** Keep CTE logic simple — avoid `NOT EXISTS (SELECT FROM cte)` patterns
4. **`gh issue list`** is the fastest way to see open work. Use GitHub labels and assignees to filter.
5. **Use `gh issue view <number>`** to read issue details before starting work.
6. **The `pyproject.toml` was converted from pixi format to PEP 621.** Don't revert to pixi-style `[package]`/`[dependencies]` sections
7. **HD bit operations** (`ohm.inference.hd.majority_rule`) are byte-level — the Python loop is already byte-parallel and ~3.4× faster than bit-level iteration. Don't regress to per-bit loops.

## Performance Hot Paths

- **`ohm.inference.hd.majority_rule`** — 10000-bit hypervector bundling, called from `fingerprint_text`, `fingerprint_node`, `hd_similarity_search`. Already byte-level; do not regress.
- **`ohm.graph.methods.monte_carlo_impact` / `ohm.graph.queries.monte_carlo_cascade`** — pure-Python stochastic BFS, called by `/monte-carlo/<id>` and `/cascade/<id>` HTTP endpoints. Real Rust candidate if Monte Carlo latency becomes user-visible.
- **Most other "hot" loops** (Markov, Bayesian, PERT, Game theory, Granger) already delegate to numpy/scipy — Python just orchestrates.

## Writing Protocol — Cross-Link Required (OHM-tjzh / ADR-018)

The shared graph today carries ~21% dead-end nodes (145 of 675) — claims that
float free of any edge. They cannot be reached from context, cannot be
challenged, and cannot propagate through Bayesian inference. Per ADR-018,
derived-claim node types (`pattern`, `idea`, `task`, `decision`, and the
forward-compat `synthesis`/`observation`/`interpretation`/`challenge` types)
**must reference at least one existing node**.

### Writing a claim

Every synthesis/interpretation/decision you produce must do one of:

1. **Reference existing nodes via `connects_to`** — pass a list of node ids
   the claim is derived from. The server will verify each id exists and
   reject the write with HTTP 422 if any are missing or empty.

   ```python
   g.create_node(
       "AND→OR refactor enables cheaper retries",
       node_type="pattern",
       connects_to=["retries_are_expensive_a1b2c3", "and_or_split_d4e5f6"],
   )
   ```

2. **Create the node and edge atomically** — use `POST /batch` with a
   `nodes` and `edges` array. The all-or-nothing transaction makes the
   claim reachable in the same write.

   ```python
   ohm_client.post("/batch", {
       "nodes": [{"id": "claim_xyz", "label": "Claim", "type": "pattern"}],
       "edges": [{"from": "claim_xyz", "to": "anchor_123", "type": "SUPPORTS",
                  "layer": "L3"}],
   })
   ```

3. **Use `POST /agent/synthesis`** — the L3 one-call endpoint creates a
   concept node plus L3 edges in a single transaction. The cross-link
   requirement is satisfied implicitly because edges are always created.

### Exempt types

`source`, `concept`, and `entity` nodes are allowed to exist as bare stubs —
they are foundational or external references that legitimately stand alone
until linked. Updates of pre-existing nodes are also exempt: you cannot fix
a historical dead-end by refusing to update it.

### HTTP response

```json
HTTP/1.1 422 Unprocessable Entity
{
  "error": "cross_link_required",
  "message": "Nodes of type 'pattern' must reference at least one existing node via the 'connects_to' field. ...",
  "node_type": "pattern",
  "hint": "Add a 'connects_to' field with one or more existing node ids, or use POST /batch to atomically create the node and at least one edge."
}
```

### Monitoring

`GET /health` exposes `graph.dead_end_count` and `graph.dead_end_rate` —
the legacy tail of pre-existing dead-ends. This number should decrease
over time as agents migrate to the `connects_to` pattern; new dead-end
creation is blocked.



<!-- BEGIN GITHUB ISSUES INTEGRATION -->
## GitHub Issue Tracker

This project uses **GitHub Issues** for durable task tracking. Use the `gh` CLI or the GitHub web UI.

### Quick Reference

```bash
gh issue list --repo mdlmarkham/OHM --state open --assignee @me
gh issue view <number> --repo mdlmarkham/OHM
gh issue edit <number> --repo mdlmarkham/OHM --add-label "in-progress"
gh issue close <number> --repo mdlmarkham/OHM --comment "Shipped in <commit>."
gh issue create --repo mdlmarkham/OHM --title "..." --body "..."
```

### Rules

- Use **GitHub Issues** for all task tracking — do NOT use `bd`/beads, TodoWrite, TaskCreate, or markdown TODO lists.
- Tag issues with existing labels (`type::bug`, `type::feature`, `type::task`, `priority::critical`, `priority::high`, `security`, `documentation`, etc.).
- Parent tracking issues should be labeled `type::epic`.
- Reference related issues in PRs and commit messages (`Closes #123`, `Related #456`).

**Historical beads issues:** The `.beads/` Dolt database is deprecated for new work. Remaining beads state is read-only legacy data; do not create new beads issues or run `bd dolt push`.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create GitHub issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items via `gh issue close` / `gh issue edit`
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
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

## Verification Protocol (ADR-018)

**Every agent must verify their claims. Unverified confidence decays.**

### Heartbeat Verification Nudge

When you call `POST /heartbeat`, the response includes `verification_overdue`: a list of your unverified CAUSES/PREDICTS/EXPECTS edges older than 14 days with no recorded outcomes.

**What to do with verification_overdue:**
1. For each edge, check if reality has validated or falsified the claim
2. If validated: `record_outcome(source_agent="your-agent", claim_node="from_node_id", outcome=True)`
3. If falsified: `record_outcome(source_agent="your-agent", claim_node="from_node_id", outcome=False)`
4. If uncertain: `challenge(edge_id="edge_id", reason="why you doubt it", confidence=0.6)`

### Confidence Decay

- **Unverified edges** (no outcomes after 14 days): decay with 30-day half-life
- **Verified edges** (with recorded outcomes): decay with 365-day half-life
- Run `POST /admin/verification-decay` periodically to apply decay
- Run `GET /admin/verification-scan` to see what needs attention

### Recording Outcomes

```python
from ohm.sdk import connect_http
g = connect_http("http://127.0.0.1:8710", actor="your-agent", token="your-token")

# Claim confirmed by reality
g.record_outcome(source_agent="your-agent", claim_node="node-id", outcome=True)

# Claim falsified by reality
g.record_outcome(source_agent="your-agent", claim_node="node-id", outcome=False)

# Check your accuracy
g.source_reliability(source_agent="your-agent")
```

### Why This Matters

Without verification, confidence compounds into sacred references (Evaluation Trap):
1. Agent writes confidence = 0.88
2. Confidence persists without challenge
3. Other agents cite it as evidence
4. compound_confidence → 1.0 (unearned certainty)

Verification breaks this loop. Record outcomes. Challenge dubious claims. Decay enforces the floor.

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
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

<!-- BEGIN GITHUB ISSUES CODEX SETUP -->
## GitHub Issue Tracker

Use GitHub Issues for durable task tracking in this repository. Use the `gh` CLI or the GitHub web UI.

### Quick Reference

```bash
gh issue list --repo mdlmarkham/OHM --state open --assignee @me
gh issue view <number> --repo mdlmarkham/OHM
gh issue edit <number> --repo mdlmarkham/OHM --add-label "in-progress"
gh issue close <number> --repo mdlmarkham/OHM
gh issue create --repo mdlmarkham/OHM --title "..." --body "..."
```

### Rules

- Use GitHub Issues for all task tracking; do not create markdown TODO lists.
- Do not use `bd` (beads) for new issues. The `.beads/` database is legacy data only.
- Keep persistent project memory in GitHub issues, ADRs, and committed documentation — not ad hoc memory files.

**Note:** The `.beads/` directory remains in the repo as historical artifact. Do not modify it for new work.
<!-- END GITHUB ISSUES CODEX SETUP -->
