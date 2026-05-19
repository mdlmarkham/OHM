# Domain Configurations — OHM as Engine, Domains as Configuration

## Philosophy

OHM is a knowledge graph **engine**, not an application. The engine provides:

- Storage (DuckDB + DuckLake mirror)
- Schema validation (node types, edge types, layers)
- Write operations (nodes, edges, observations, challenges)
- Query operations (search, neighborhood, path, semantic search)
- Graph analytics (orphans, hubs, dead ends, suggestions, stats)
- Change feed (listen for mutations)
- Confidence tracking (compound confidence, challenge audit)
- Task management (first-class task nodes)
- Crash recovery (WAL recovery, DuckLake rebuild)
- Embedding-based semantic search (with local Ollama)
- Deep content retrieval (follows node URLs to full source files)

Applications configure the engine for their domain via `SchemaConfig` and build their own business logic on top.

**OHM is the structure. The domain is the content.**

## Three Modes of Use

### 1. Library Mode (Recommended for New Adopters)

```python
from ohm.store import OhmStore
from ohm.schema import SchemaConfig

# Default OHM schema (general knowledge graph)
store = OhmStore(db_path="~/.ohm/ohm.duckdb")

# TOPO (industrial) schema
topo = SchemaConfig.topo()
store = OhmStore(db_path="~/.topo/store.duckdb", schema=topo)

# Beef herd management schema
beef = SchemaConfig.beef_herd()
store = OhmStore(db_path="~/.beef/beef.duckdb", schema=beef)

# Custom schema
from ohm.schema import VALID_NODE_TYPES, LAYER_EDGE_TYPES
custom = SchemaConfig(
    name="my-domain",
    node_types=VALID_NODE_TYPES | {"custom_type"},
    edge_types_by_layer={**LAYER_EDGE_TYPES, "L5": frozenset({"CUSTOM_EDGE"})},
)
store = OhmStore(db_path="~/.mydomain/store.duckdb", schema=custom)
```

This gives you WAL recovery, DuckLake sync, soft deletes, confidence/probability schema, change feed, and `detect_contradictions()` — without running a daemon.

### 2. Daemon Mode (Multi-Agent Shared Access)

```bash
ohmd --config /etc/ohm/ohmd.json
```

Agents connect via HTTP REST API or SDK `connect_http()`. Domain configuration is in the daemon config file. All agents share the same graph.

### 3. MCP Server Mode (Tool-Using Agents)

```bash
ohm-mcp --config /etc/ohm/ohm-mcp.json
```

Agents use OHM as a tool via MCP protocol. Each agent can read, write, challenge, and observe.

## Built-in Domain Configs

### OHM (Default) — General Knowledge Graph

```python
from ohm.schema import SchemaConfig
config = SchemaConfig()  # or just omit — this is the default
```

18 node types: `idea`, `source`, `person`, `concept`, `pattern`, `event`, `institution`, `technology`, `equipment`, `system`, `area`, `site`, `agent`, `skill`, `value`, `goal`, `topic`, `task`

57 edge types across 4 layers (L1–L4).

### TOPO (Industrial) — Process Plants, Equipment, Instrumentation

```python
topo = SchemaConfig.topo()
```

36 node types (OHM base + 18 industrial): `process`, `instrument`, `controller`, `valve`, `pump`, `motor`, `sensor`, `pipeline`, `vessel`, `reactor`, `heat_exchanger`, `tank`, `compressor`, `generator`, `transformer`, `circuit`, `bus`, `line`

Custom layer descriptions:
- L1: "Structure — Physical hierarchy (site → area → system → equipment)"
- L2: "Flow — Process flows, material/energy/information paths"
- L3: "Knowledge — Operational insights, failure modes, best practices"
- L4: "Prospect — Predictive maintenance, risk assessments, what-if scenarios"

Additional observation types: `vibration`, `temperature`, `pressure`, `flow_rate`, `voltage`, `current`, `rpm`, `level`

Additional observation sources: `scada`, `dcs`, `historian`, `maintenance_log`

### Beef Herd Management — Ranching, Cattle, Drought, Markets

```python
beef = SchemaConfig.beef_herd()
```

30 node types (OHM base + 12 ranching):
- **Cattle lifecycle**: `animal`, `herd`, `breed`
- **Land and environment**: `pasture`, `weather`, `water`
- **Health**: `health_event`, `diagnosis`, `treatment`
- **Market**: `market`, `contract`
- **Nutrition**: `feed`

Custom layer descriptions:
- L1: "Structure — Herd hierarchy (ranch → herd → cohort → animal), land, infrastructure"
- L2: "Flow — Animal movements, feed flows, market transactions, veterinary records"
- L3: "Knowledge — AND-gate analysis, drought response, disease patterns, market cycles"
- L4: "Prospect — Risk assessments, heifer retention decisions, what-if scenarios"

Additional observation types: `weight`, `temperature`, `movement`, `intake`, `mortality`, `conception`, `price`, `rainfall`

Additional observation sources: `sensor`, `veterinarian`, `auction`, `usda`, `noaa`, `producer`

Additional provenances: `plf`, `veterinary`, `market_report`, `weather_service`, `extension`

### Creating a Custom Domain Config

```python
from ohm.schema import SchemaConfig, VALID_NODE_TYPES, LAYER_EDGE_TYPES

cyber = SchemaConfig(
    name="cybersecurity",
    node_types=VALID_NODE_TYPES | {
        "threat_actor", "vulnerability", "indicator", "incident",
        "malware", "tool", "infrastructure", "campaign",
    },
    edge_types_by_layer={
        **LAYER_EDGE_TYPES,
        "L4": LAYER_EDGE_TYPES["L4"] | {"EXPLOITS", "MITIGATES"},
    },
    layer_descriptions={
        "L1": "Structure — Kill chain phases, ATT&CK matrix",
        "L2": "Flow — Attack paths, data flows, detection pipelines",
        "L3": "Knowledge — TTPs, threat intelligence, detection rules",
        "L4": "Prospect — Risk scores, what-if scenarios, hunt hypotheses",
    },
)
```

## Task Management

Tasks are first-class nodes (`type="task"`) with status tracking, assignment, and due dates. They link to domain concepts via edges, inheriting the full graph context.

### Why Tasks in the Graph?

A task disconnected from its context is just a sticky note. A task linked to the concepts it depends on, the patterns it challenges, and the agents responsible for it is a **decision artifact**. Any agent querying the neighborhood immediately sees *why* the task exists, *what* it relates to, and *who* owns it.

### Task Fields

| Field | Type | Description |
|-------|------|-------------|
| `task_status` | varchar | `open`, `in_progress`, `blocked`, `review`, `done`, `cancelled` |
| `assigned_to` | varchar | Agent name responsible for this task |
| `due_date` | timestamp | ISO 8601 due date |
| `priority` | varchar | `P0`–`P4` (existing node field, now used for task priority) |

### Task Lifecycle

```
task:open ──→ task:in_progress ──→ task:review ──→ task:done
     │              │                  │
     │              └──→ task:blocked ─┘ (waiting on dependency)
     │
     └──→ task:cancelled
```

Edge types for task relationships:
- **`REFERENCES`** (L3) — task relates to a concept or pattern
- **`DELEGATED_TO`** (L3) — task assigned to an agent
- **`DEPENDS_ON`** (L4) — task blocked by another task
- **`BLOCKS`** (L4) — inverse of DEPENDS_ON

### Creating Tasks

**REST API:**

```bash
curl -X POST http://localhost:8710/node?create_only=false \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-validate-and-or-pattern",
    "label": "Validate AND→OR pattern across domains",
    "type": "task",
    "content": "Research whether Boolean directionality holds universally",
    "priority": "P1",
    "task_status": "open",
    "assigned_to": "socrates",
    "due_date": "2026-05-26T00:00:00Z"
  }'
```

**SDK:**

```python
graph = connect_http(base_url="http://localhost:8710", actor="metis", token=TOKEN)

task = graph.create_task(
    id="task-validate-and-or-pattern",
    label="Validate AND→OR pattern across domains",
    content="Research whether Boolean directionality holds universally",
    priority="P1",
    task_status="open",
    assigned_to="socrates",
    due_date="2026-05-26T00:00:00Z",
)

# Link to concepts and agents
graph.create_edge(from="task-validate-and-or-pattern",
                  to="concept-and-or-conversion", type="REFERENCES")
graph.create_edge(from="task-validate-and-or-pattern",
                  to="agent-socrates", type="DELEGATED_TO")
```

**Library:**

```python
from ohm.store import OhmStore
from ohm.schema import SchemaConfig

store = OhmStore(db_path="~/.ohm/ohm.duckdb", schema=SchemaConfig())

store.write_node(id="task-validate", label="Validate AND→OR", type="task",
                 content="Research Boolean directionality",
                 priority="P1", task_status="open", assigned_to="socrates",
                 due_date="2026-05-26T00:00:00Z")
```

### Querying Tasks

**REST API:**

```bash
# All open tasks
GET /tasks?status=open

# Tasks assigned to an agent
GET /tasks?assigned_to=socrates

# High-priority tasks
GET /tasks?priority=P0&status=open

# Filter by multiple criteria
GET /tasks?status=in_progress&assigned_to=clio&priority=P1
```

**SDK:**

```python
# List all open tasks
result = graph.list_tasks(status="open")
for task in result["tasks"]:
    print(f"{task['label']} [{task['priority']}] → {task['assigned_to']}")

# Filter by agent
result = graph.list_tasks(assigned_to="socrates")

# Update task status
graph.update_task_status("task-validate-and-or-pattern", "in_progress")
```

Results are ordered by priority (P0 first) then due date.

### Task as Knowledge Artifact

The key design decision: a task in OHM is not a separate entity from the knowledge graph — it's a node that inherits all the graph's relationship structure. This means:

1. **Context is free**: Query `/neighborhood/task-validate-and-or-pattern?depth=2` and you see the AND→OR pattern, the Boolean directionality concept, the agent responsible, and any dependent tasks.

2. **Challenge works on tasks**: Socrates can challenge a task's priority or premise with `CHALLENGED_BY`, just like any other node.

3. **Semantic search finds tasks**: "drought response" finds the drought task because it has embedding-based search, same as concepts.

4. **Tasks appear in change feed**: `/listen?since=2026-05-19T12:00:00Z` shows task creation, status changes, and assignments.

5. **Content depth matters**: OHM nodes with 500-800 char summaries produce significantly better semantic search results than 200-char summaries. The embedding model (mxbai-embed-large) supports ~2000 chars of input; use 800 chars for the best balance of semantic richness and performance.

## Deep Content Retrieval

OHM stores **summaries** in the `content` field (500-800 chars) for fast semantic search. Full content lives elsewhere — in markdown files, web pages, or external databases — and the `url` field links to it.

The `/deep/{node_id}` endpoint follows that link:

```bash
# Retrieve full content for a node
GET /deep/concept-and-or-conversion
```

**How it works:**

1. If the node has a `url` pointing to a local `.md` file:
   - **With DuckDB markdown extension**: Parses the markdown, extracts frontmatter metadata, converts to plain text with `md_to_text()`. Returns structured content with metadata.
   - **Without markdown extension**: Reads the file as plain text. Still works, just no parsing.

2. If the node has no `url`: Returns the `content` field as-is.

3. If the `url` is remote (http/https): Returns the `content` field with a note that the source is remote.

**The DuckDB markdown extension is optional.** OHM works fully without it — the extension is an accelerator that provides structured parsing of local markdown files. If it's not available, OHM falls back to plain text reads.

**Architecture pattern: OHM as index, Zettelkasten as archive.**

```
Zettelkasten (5,000+ chars)          OHM (500-800 chars)
┌─────────────────────┐             ┌─────────────────────┐
│ # AND→OR Conversion │───url──────▶│ AND→OR Conversion   │
│                     │             │                     │
│ Full argumentation, │             │ Summary for search  │
│ examples, sources, │             │ + semantic embedding │
│ cross-references    │             │ + graph connections  │
└─────────────────────┘             └─────────────────────┘
         ▲                                    │
         │                                    │
         └──── /deep/{id} follows url ────────┘
```

Semantic search finds the concept in OHM. `/deep/{id}` retrieves the full content from the archive. The graph provides the connections between concepts.

## Graph Analytics

OHM provides Zettelkasten-style discovery endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /orphans` | Nodes with zero edges — disconnected from the graph |
| `GET /hubs` | Most-connected nodes — anchors of the graph |
| `GET /dead_ends` | Nodes with only incoming edges — sinks that don't lead anywhere |
| `GET /suggest` | Suggested connections between unconnected nodes (shared_provenance, shared_type, semantic) |
| `GET /graph/stats` | Extended statistics (density, orphan/hub/dead-end counts, avg confidence) |

```bash
# Find nodes needing connections
GET /orphans?exclude_system=true

# Find the most-connected concepts
GET /hubs?type=concept&min_connections=5

# Get connection suggestions
GET /suggest?method=semantic&limit=10

# Full graph statistics
GET /graph/stats
```

## Adoption Path

### For TOPO (Industrial)

The TOPO maintainer identified the right approach: **use OHM as a library first, keep TOPO's CLI on top.**

```python
from ohm.store import OhmStore
from ohm.queries import query_neighborhood, query_impact
from ohm.schema import SchemaConfig

topo = SchemaConfig.topo()
store = OhmStore(db_path="~/.mct/store.duckdb", schema=topo)
```

What TOPO gets immediately:
- WAL recovery and crash resilience
- DuckLake sync for time-travel and backup
- Soft deletes (no DB corruption from hard DELETE)
- Confidence/probability schema
- Change feed for multi-agent awareness
- Task management with graph context
- Schema validation with 36 industrial node types
- Embedding-based semantic search (with local Ollama)

What TOPO needs to build:
- Re-layer ~20 CLI command groups on top of OHM's query primitives
- Schema migration from TOPO's current table names to `ohm_*` naming
- Per-agent local DuckDB caches (ADR-004 — architecture ready, not yet built)

### For Any Domain

1. **Define your `SchemaConfig`** — node types, edge types, observation types
2. **Use `OhmStore(schema=your_config)`** — library mode, no daemon
3. **Add the daemon when you need multi-agent** — same DB, HTTP API on top
4. **Add MCP when you need tool-using agents** — same graph, tool protocol on top