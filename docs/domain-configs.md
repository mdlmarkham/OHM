# Domain Configurations — OHM as Engine, Domains as Configuration

## Philosophy

OHM is a knowledge graph **engine**, not an application. Applications like TOPO
(industrial), beef herd management, or cybersecurity configure the engine for
their domain via `SchemaConfig`.

## Three Modes of Use

### 1. Library Mode (Recommended for New Adopters)

```python
from ohm.store import GraphStore
from ohm.schema import SchemaConfig

# OHM default schema
store = GraphStore(db_path="~/.ohm/ohm.duckdb")

# TOPO (industrial) schema
topo = SchemaConfig.topo()
store = GraphStore(db_path="~/.topo/store.duckdb", schema=topo)

# Beef herd management schema
beef = SchemaConfig.beef_herd()
store = GraphStore(db_path="~/.beef/beef.duckdb", schema=beef)

# Custom schema
custom = SchemaConfig(
    name="my-domain",
    node_types=VALID_NODE_TYPES | {"custom_type"},
    edge_types_by_layer={**LAYER_EDGE_TYPES, "L5": frozenset({"CUSTOM_EDGE"})},
)
store = GraphStore(db_path="~/.mydomain/store.duckdb", schema=custom)
```

This gives you WAL recovery, DuckLake sync, soft deletes, confidence/probability
schema, change feed, and `detect_contradictions()` — without running a daemon.

### 2. Daemon Mode (Multi-Agent Shared Access)

```bash
ohmd --config /etc/ohm/ohmd.json
```

Agents connect via HTTP REST API or SDK `connect_http()`. Domain configuration
is in the daemon config file.

### 3. MCP Server Mode (Tool-Using Agents)

```bash
ohm-mcp --config /etc/ohm/ohm-mcp.json
```

Agents use OHM as a tool via MCP protocol. Domain configuration is in the
MCP server config.

## Built-in Domain Configs

| Domain | SchemaConfig | Node Types | Edge Types | Key Additions |
|--------|-------------|------------|------------|---------------|
| OHM (default) | `SchemaConfig()` | 18 | 57 | General knowledge graph |
| TOPO (industrial) | `SchemaConfig.topo()` | 36 | 57 | Process equipment, instrumentation, industrial observations |
| Beef Herd | `SchemaConfig.beef_herd()` | 30 | 57 | Animal/herd lifecycle, health events, market, weather |
| Task Management | Built-in (`task` node type) | 18+task | 57+BLOCKS | Status tracking, assignment, due dates |

## Task Management (Built-In)

All domain configs include `task` as a node type. Tasks are first-class nodes:

```python
# Create a task linked to domain concepts
store.write_node(
    id="task-drought-response-plan",
    label="Drought Response Plan for 2026",
    type="task",
    content="Develop contingency plan for 60-day drought scenario",
    priority="P1",
    task_status="open",
    assigned_to="ranch-manager",
    due_date="2026-06-15T00:00:00Z",
)

# Link task to concepts via edges
store.write_edge(
    from_node="task-drought-response-plan",
    to_node="concept-drought-perturbation",
    edge_type="REFERENCES",
    layer="L3",
)

store.write_edge(
    from_node="task-drought-response-plan",
    to_node="agent-ranch-manager",
    edge_type="DELEGATED_TO",
    layer="L3",
)
```

### Task Lifecycle

```
task:open → DELEGATED_TO agent → task:in_progress → task:review → task:done
                 ↘ task:blocked → DEPENDS_ON other_task → unblock → task:in_progress
```

### Task Statuses

| Status | Meaning |
|--------|---------|
| `open` | New task, not yet started |
| `in_progress` | Agent is actively working on it |
| `blocked` | Waiting on dependency or external input |
| `review` | Awaiting review by another agent |
| `done` | Completed |
| `cancelled` | No longer needed |

### Querying Tasks

```bash
# All open tasks
GET /tasks?status=open

# Tasks assigned to a specific agent
GET /tasks?assigned_to=socrates

# High-priority tasks
GET /tasks?priority=P0&status=open

# Tasks due this week (filtered client-side from due_date)
GET /tasks?status=in_progress
```

## Adoption Path for TOPO

The TOPO maintainer identified the right path: **use OHM as a library first,
keep TOPO's CLI on top.**

```python
# TOPO adopts OHM's store + queries, keeps its own CLI
from ohm.store import GraphStore
from ohm.queries import query_neighborhood, query_impact
from ohm.schema import SchemaConfig

topo = SchemaConfig.topo()
store = GraphStore(db_path="~/.mct/store.duckdb", schema=topo)
```

### Migration Prerequisites

1. Schema migration from TOPO's current table names to OHM's `ohm_*` naming
   (the migration framework in `ohm.schema` handles this)
2. TOPO's 20+ CLI command groups need to be re-layered on top of OHM's query
   primitives (the bulk of the work)
3. Per-agent local DuckDB caches (ADR-004) — not yet built, but the architecture
   supports it via DuckLake as source of truth

### What TOPO Gets Immediately

- WAL recovery and crash resilience
- DuckLake sync for time-travel and backup
- Soft deletes (no more DB corruption from hard DELETE)
- Confidence/probability schema
- Change feed for multi-agent awareness
- Embedding-based semantic search (with local Ollama)
- Task management with graph context
- Schema validation with domain-specific node/edge types