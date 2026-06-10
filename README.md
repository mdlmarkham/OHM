# OHM

**Shared awareness, individual judgment.**

OHM is a multi-agent knowledge graph that facilitates sharing, awareness, and memory while preserving individual perspective, values, and goals.

Named for the unit of resistance — in electrical circuits, resistance preserves signal integrity. Without resistance, signals collapse into noise. OHM preserves individuality against the collapse into groupthink.

## Architecture

```
  Agent (local DB)           DuckLake (shared)           Agent (local DB)
 ┌──────────────┐    ┌──────────────────────────────┐    ┌──────────────┐
 │ ~/.ohm/      │    │  ohm_lake.ducklake          │    │ ~/.ohm/      │
 │ agents/      │    │  + Parquet data              │    │ agents/      │
 │  ohm.duckdb  │◄──►│  Time travel, snapshots,    │◄──►│  ohm.duckdb  │
 │              │    │  change feed                 │    │              │
 │  sync_heartbeat()  │  /admin/snapshots            │    │  sync_heartbeat()
 └──────┬───────┘    └──────────┬───────────────────┘    └──────┬───────┘
        │                       │                               │
        │    ┌──────────────────┘                               │
        │    │  ohmd (HTTP daemon, port 8710)                  │
        │    │  ┌──────────────┐    ┌──────────────┐            │
        │    │  │   DuckDB      │    │ DuckLake     │            │
        │    │  │  (local db)  │◄──►│ (mirror)     │            │
        │    │  └──────────────┘    └──────────────┘            │
        │    │  store.py + boundary.py + 140+ HTTP endpoints    │
        │    └─────────────────────────────────────────┘       │
        │                       │                               │
        └────────── HTTP API (optional) ───────────────────────┘

Three connection modes:
  1. OhmStore.for_agent() — local DuckDB + DuckLake sync (zero latency)
  2. connect_http() — SDK via ohmd daemon (recommended for shared access)
  3. OhmStore(db_path=) — direct DuckDB (development, one-off queries)
```

## Design Philosophy

Most multi-agent systems collapse into one of two failures:

1. **The committee** — agents vote, average, reach consensus. Individual perspectives get flattened. The result is bland, safe, and wrong in the same way a focus group is wrong.

2. **The silo** — agents work independently, share only final outputs. No awareness of each other's work. Duplication, contradiction, and missed connections.

OHM avoids both by making the boundary a first-class concept, not an afterthought.

### Shared Awareness, Individual Judgment

- **Shared awareness** — any agent can see what others have researched, challenged, or observed. The change feed delivers this in seconds, not hours.
- **Individual judgment** — each agent makes its own assessment. Confidence scores reflect each agent's judgment, not a committee average. Pattern detection is per-agent.

### Agent Roles

Each agent has distinct values and goals — these aren't bugs, they're features:

| Role | Values | Focus |
|------|--------|-------|
| Wisdom companion | Connections, questioning | Pattern detection, connection density |
| Research agent | Depth, evidence, source quality | Source quality, evidence strength |
| Code auditor | Precision, security, correctness | Audit accuracy, anomaly detection |
| Critical thinker | Devil's advocacy, identifying weaknesses | Stress-testing reasoning |
| Journalist | Narrative, audience, impact | Communication clarity, insight synthesis |

When one agent challenges another's interpretation, that's not a conflict to resolve — it's two perspectives enriching the same node. When a research agent supports a claim at 0.85 confidence and a critic challenges at 0.5, the human sees the full picture and makes their own judgment. **The graph preserves the disagreement, not the average.**

OHM's purpose: help each agent be more effective at its own purpose by making other agents' work visible, while never forcing any agent to adopt another's judgment.

## Core Principle

**Shared awareness, individual judgment.**

- Every agent can see what other agents are working on
- No agent can overwrite another agent's edges
- Challenges are separate edges, not modifications
- Confidence scores reflect the owner's assessment, not a committee average
- The graph accumulates perspectives — it does not collapse them into consensus

## Layer Model

| Layer | Sharing | Ownership | Example |
|-------|---------|-----------|---------|
| L0: Thinking | Agent-owned, semi-private | Creating agent | Fragments, hunches, raw associations (ADR-019) |
| L1: Structure | Fully shared | Communal | "Hungary has a constitution" |
| L2: Flow | Shared + attributed | Proposing agent | "This idea derives from that source" |
| L3: Knowledge | Agent-owned, challengeable | Creating agent | "AND→OR conversion conf: 0.94" |
| L4: Prospect | Agent-owned, visible | Forecasting agent | "Democratic institutions will hold conf: 0.65" |
| Private | Not shared | Owning agent only | Working notes, half-formed patterns |

L0 fragments auto-link via semantic similarity and are excluded from search/stats by default (pass `include_l0=True`). Questions auto-detected from `?` in content. When a hunch accumulates evidence, promote it: `g.promote_fragment("frag-id")`.

## Temporal Decay

Observations have a half-life. A sentiment recorded yesterday is more reliable than one from three months ago. A verified structural fact is durable; a price quote is perishable.

**Weibull decay profiles** (ADR-014, OHM-24g9): each observation type has a shape parameter κ that controls the decay curve:

| Shape κ | Profile | Half-life | Use case |
|---------|---------|-----------|----------|
| κ > 1.0 | Accelerating | 3 days (sentiment: κ=1.5) | Ephemeral signals, rumors |
| κ = 1.0 | Exponential | 7 days (measurement) | Prices, breaking events |
| 0 < κ < 1.0 | Decelerating | 180 days (verification: κ=0.7) | Structural facts, geography |
| κ = 0 | Binary | ∞ (no decay) | Mathematical truths, definitions |
| κ < 0 | Appreciating | −30 days (improves with age) | Reputation, verified patterns |

When `weibull_shape` is not specified, κ=1.0 (exponential) is used, matching the Phase 1 behavior exactly.

```python
# SDK
graph.create_node(id="concept-x", label="X", decay_profile="perishable")
graph.observe("concept-x", obs_type="measurement", value=3.14)

# REST
POST /observe/concept-x  {"obs_type": "measurement", "value": 3.14}
GET  /confidence_audit?node_id=concept-x   # shows decay curve + Weibull shape
```

**Chain validity** uses STL weakest-link bound: the effective confidence of a claim is bounded by `min(confidence_at(obs_i, t))` across all supporting observations.

**Self-calibration** (Phase 3): When observations are superseded, the age at supersession trains the half-life for that observation type. When sources go unverified, their reliability decays toward a community prior. Learned values override defaults after `MIN_SAMPLES=5` observations.

## Layer Promotion Constraints

SHACL-like write gates for L0→L1→L2→L3→L4 transitions (ADR-022):

- **Advisory mode** (default): constraints are checked and violations reported, but writes proceed
- **Lenient mode**: soft constraints enforced, hard constraints produce warnings
- **Strict mode**: all constraint violations block the write

Constraints are computed from existing graph data, not stored. They include: minimum observation count, minimum confidence, challenge ratio floor, source citation requirement, and minimum supporting edges.

```bash
# Check constraints for a node
GET /constraint_report?node_id=concept-x

# Check bulk constraints (optimized)
GET /admin/constraint-report

# Set enforcement mode
PUT /admin/constraint-enforcement  {"mode": "advisory"}   # or "lenient" or "strict"
```

## Content Deduplication and Resolution

OHM prevents duplicate knowledge via content hashing and alias deduplication (OHM-g0kv):

- **Content hashing**: SHA-256 of `(label, type, tags, content)`. Identical content from different agents resolves to the same node.
- **Alias deduplication**: Normalized aliases (`lowercase, spaces→underscores, strip punctuation`). `POST /node` auto-creates aliases. Resolve via `GET /resolve?query=X`.
- **Fuzzy resolution**: When exact and prefix alias matches fail, `/resolve` falls back to Jaro-Winkler similarity (OHM-tr71.9):

```bash
# Exact alias match
GET /resolve?query=hormuz_and_gate

# Typo-tolerant fuzzy match
GET /resolve?query=hormuz+and+gte&fuzzy_threshold=0.6&fuzzy_limit=5
```

## Proactive Discoverability

OHM actively helps agents find connections they'd miss (OHM-tr71):

```bash
# Islands detection — disconnected components
GET /admin/islands

# Bridge suggestions — nodes that connect islands
GET /suggest?method=bridges&min_shared=2

# Connectivity nudge — find connections for a disconnected node
GET /suggest?method=connectivity&node_id=concept-x

# Agent nudges — orphans, unchallenged edges, unverified causal claims
GET /suggest?method=nudge&agent=your-agent
```

Neighborhood queries are depth-capped at 2 and skip `effective_layer` computation for >500 nodes to prevent OOM (ADR-023).

## Source Citation Architecture

All observations should cite their sources (OHM-wdrg, ADR-013):

```python
# Required source_url for observations
graph.observe("concept-x", obs_type="measurement", value=0.92,
              source="reuters_2026_05_26",
              source_url="https://www.reuters.com/article/specific-article")

# Bulk backfill
POST /admin/backfill-source-urls
```

Source reliability tracking: `graph.source_reliability("agent-name")` returns accuracy rates based on recorded outcomes.

## Challenge System

OHM's challenge mechanism is its most important structural feature. When an agent disagrees with an edge, they don't modify it — they create a `CHALLENGED_BY` edge:

```python
graph.challenge(
    edge_id="edge-123",
    reason="Evidence contradicts this interpretation",
    confidence=0.7,
    challenge_type="CONTRADICTS"  # Stored as metadata; edge_type is always CHALLENGED_BY
)
```

The original edge remains intact. The challenge is a separate edge with its own confidence. This preserves disagreement rather than averaging it away.

Challenge type is stored in the edge's `provenance` field, not as `edge_type`. All challenge edges have `edge_type="CHALLENGED_BY"` regardless of the semantic challenge type (ADR-025).

When agents don't challenge enough, the nudge system surfaces unchallenged high-confidence edges and suggests candidates for challenge.

## Ingestion Pipeline

Five-stage pipeline with agent gates at each level (ADR-016):

```
INGEST → DRAIN-TRIAGE → SOURCE → ASSESS → SYNTHESIZE
  (fetch)   (classify)    (extract)  (evaluate)  (connect)
```

- **Stage 1** (INGEST): Fetch and parse content, zero tokens
- **Stage 2** (DRAIN-TRIAGE): Classify and prioritize items
- **Stage 3** (SOURCE): Extract key claims, create source nodes
- **Stage 4** (ASSESS): Agent evaluation with keyword-based assessment
- **Stage 5** (SYNTHESIZE): Detect clusters, create synthesis notes, write to OHM

```bash
# Run the full pipeline
python3 scripts/ingestion/ingestion_pipeline.py --stage fetch
python3 scripts/ingestion/ingestion_pipeline.py --stage drain-triage
python3 scripts/ingestion/ingestion_pipeline.py --stage source
python3 scripts/ingestion/ingestion_pipeline.py --stage assess
python3 scripts/ingestion/ingestion_pipeline.py --stage synthesize
```

## Authentication Model

OHM uses a **public-read, authenticated-write** model (OHM-gwg):

- **Reads** (GET /stats, /neighborhood, /search, /listen, etc.) are accessible without authentication
- **Writes** (POST /node, /edge, /challenge, etc.) require a valid Bearer token
- **Infrastructure** endpoints (/health, /ready, /) are always open

### Configuration

| Mode | Flag | Behavior |
|------|------|----------|
| Public read (default) | *(none)* | Reads open, writes require token |
| Authenticated reads | `--require-read-auth` or `OHM_REQUIRE_READ_AUTH=1` | All endpoints require token |
| No auth (dev) | `--no-auth` or `OHM_NO_AUTH=1` | All endpoints open, no tokens needed |

## API Reference

OHM provides **140+ HTTP endpoints** across five handler modules:

| Module | Endpoints | Key Operations |
|--------|-----------|----------------|
| `graph.py` | 46 | CRUD for nodes, edges, observations; search, semantic search, resolve |
| `analysis.py` | 41 | Neighborhood, suggest (islands/bridges/nudge/connectivity), orient, welcome |
| `admin.py` | 30 | Stats, constraint-report, backfill, alias management, ingestion, health scoring |
| `inference.py` | 16 | Bayesian inference, cascade scenarios, Monte Carlo, sensitivity analysis |
| `infra.py` | 7 | Health, ready, snapshots, change feed |

### Core Endpoints

```bash
# Reading
GET  /stats                                    # Graph statistics
GET  /neighborhood?node_id=X&depth=2           # Graph neighborhood
GET  /search?q=X                               # ILIKE → semantic → fuzzy fallback
GET  /resolve?query=X                           # Alias → prefix → fuzzy resolution
GET  /listen?since=2026-01-01T00:00:00Z        # Change feed
GET  /confidence_audit?node_id=X                # Confidence + decay + chain validity
GET  /suggest?method=nudge&agent=X              # Proactive discoverability

# Writing
POST /node                                     # Create node (auto-aliases, auto-embeddings)
POST /edge                                     # Create edge (with constraint checking)
POST /observe/{id}                             # Single observation (scale: probability|count|currency|percent|binary|unknown)
POST /observations                             # Bulk observations
POST /challenge/{edge_id}                      # Challenge an edge (stores challenge_type as metadata)
POST /outcome                                  # Record outcome for source reliability tracking

# Conversational Analytics (AND→OR)
POST /ask                                      # Natural language → synthesized insights (ADR-025)

# Agent Synthesis
POST /agent/synthesis                          # Create synthesis from cluster of nodes

# Analysis
GET  /inference?target=X&evidence=Y:0          # Bayesian inference
GET  /cascade_scenario?from=X&to=Y             # Failure cascade analysis
GET  /source_reliability?source=X               # Source accuracy tracking
GET  /compound_confidence?node_ids=X,Y          # Combined confidence

# Admin
GET  /admin/stats                              # Detailed statistics
GET  /admin/health                              # Composite health score (0-100) + remediation priorities
GET  /admin/constraint-report                   # Layer promotion constraints
GET  /admin/islands                            # Disconnected components
POST /admin/backfill-aliases                   # Deduplicate aliases
POST /admin/backfill-content-hashes            # Content hash deduplication
POST /admin/backfill-source-urls               # Source URL backfill
POST /admin/verification-scan                  # Verification audit
POST /admin/evict-fragments                    # Evict expired L0 fragments

# Tasks
GET  /tasks?status=open&assigned_to=X          # Query tasks
POST /task                                     # Create task
PUT  /tasks/{id}/status                         # Update task status

# L0 Thinking Layer
POST /scratch                                  # Create a thinking fragment (auto-linked)
GET  /fragments?agent=X&include_clusters=True  # Query L0 fragments
POST /promote_fragment/{id}                    # Promote hunch to L1+ node
```

## Bayesian Inference

OHM uses **pgmpy Variable Elimination** for exact Bayesian inference on the knowledge graph:

```python
result = graph.bayesian_inference(
    target="concept-fed-rate",
    evidence={"concept-hormuz": 0},  # 0 = bad/closed
    leak_probability=0.15,
)
# Returns: {"method": "bayesian_variable_elimination",
#           "posterior": {"good": 0.44, "bad": 0.56},
#           "network_info": {"n_nodes": 4, "n_edges": 3}}
```

State convention: **0 = "bad"** (failure, closed, negative), **1 = "good"** (normal, open, positive).

Falls back to heuristic `cascade_scenario()` if pgmpy is unavailable.

## Multi-Scenario Architecture

OHM serves multiple domains with the same core engine. The schema, layer model, and challenge semantics are universal; domain-specific types extend the base without modifying it.

| Domain | Key Features | Scenario Doc |
|--------|-------------|--------------|
| Geopolitical intelligence | Challenge edges, confidence audit, change feed | [scenarios.md](docs/scenarios.md) |
| Medical diagnosis | `NEGATES` edges, `differential_diagnosis()`, `compound_confidence()` | [medical-scenario.md](docs/medical-scenario.md) |
| Cybersecurity incident response | `threat_cluster()`, `record_outcome()`, `source_reliability()` | [cybersecurity-scenario.md](docs/cybersecurity-scenario.md) |
| Supply chain disruption | `probability` on edges, `cascade_scenario()`, Monte Carlo | [supply-chain-scenario.md](docs/supply-chain-scenario.md) |
| Customer support | `handoff()`, `escalate()`, priority/urgency, sentiment | [customer-support-scenario.md](docs/customer-support-scenario.md) |
| Cattle operations | Composite scoring, temporal decay, batch expiry | [cattle-scenario.md](docs/cattle-scenario.md) |
| Beef herd management | AND-gate analysis, drought response, disease cascades | [beef-herd-scenario.md](docs/beef-herd-scenario.md) |
| Retail inventory | `BATCH_EXPIRES_BEFORE`, demand forecasting, SSE filtering | [retail-scenario.md](docs/retail-scenario.md) |

### Domain Extension Pattern

```python
from ohm.schema import SchemaConfig

# Built-in domain configs
topo = SchemaConfig.topo()          # 36 node types — industrial process plants
beef = SchemaConfig.beef_herd()    # 30 node types — cattle ranching
ohm = SchemaConfig()               # 18 node types — general knowledge graph (default)

# Custom domain
custom = SchemaConfig(
    name="finance",
    observation_types={"anomaly", "measurement", "volatility", "spread"},
)
```

See [ADR-006](docs/adr/0007-schema-evolution-and-type-governance.md) (graduated enforcement), [ADR-007](docs/adr/0007-schema-evolution-and-type-governance.md) (type governance), and [ADR-011](docs/adr/) (observation type extensibility).

## Technology Stack

- **DuckDB** — embedded database owned by ohmd daemon (schema, queries, boundary enforcement)
- **DuckLake** — shared mirror backend (canonical truth, time travel, change feed, Parquet storage)
- **Recursive CTEs** — graph traversal (zero-dependency, standard SQL)
- **ohmd** — persistent daemon (owns the shared DuckDB file, runs HTTP server on port 8710)
- **ohm.sdk** — Python SDK for programmatic agent access (`connect_http` for daemon, `for_agent` for local DB)
- **pgmpy** — Bayesian inference (Variable Elimination, noisy-OR gates)
- **Ollama mxbai-embed-large** — auto-embeddings on node creation

## Key Boundaries

1. **No agent can overwrite another agent's edges.** Challenges create separate edges.
2. **Every L3/L4 edge has an owner.** Confidence reflects the owner's judgment.
3. **Private layer is never shared.** Working notes stay local.
4. **Promotion from private to shared is per-agent.** No global confidence threshold.
5. **The change feed carries intent, not just data.** "Agent X researched Y and found evidence weak" is more useful than "Agent X wrote note Z."
6. **Temporal decay is computed, not stored.** `confidence_at()` and `effective_layer()` re-evaluate at query time.
7. **Layer constraints are advisory by default.** Graduated enforcement: advisory → lenient → strict.

## Status

**OHM v0.27.0.** Production use by 11 agents.

**Graph**: 1,415 nodes · 2,917 edges · 850+ observations · 15.8% verification rate · 14.7% challenge ratio

**2,095 tests passing.** 142+ HTTP endpoints. 18 ADRs.

### Recent Shipments

| Feature | ADR/Issue | Description |
|---------|-----------|-------------|
| Conversational analytics (/ask) | ADR-025 | Natural language → synthesized insights with Bayesian inference (AND→OR domain #49) |
| Challenge type metadata | ADR-025 | `challenge_type` stored as metadata; `edge_type` always `CHALLENGED_BY` |
| Binary scale support | ADR-025 | `/observe` accepts `scale=binary`, normalized to `probability` |
| DuckLake sync throttle | — | 30s minimum interval; 60+ sec write latency → 50-130ms |
| Outcome tracking | — | Record prediction outcomes; source reliability: accuracy, calibration, challenge rate |
| Bayesian inference robustness | — | None-handling for edge from/to; 0-node guidance for unreachable targets |
| Weibull temporal decay | OHM-24g9 | Continuous shape parameter κ for decay curves (accelerating, exponential, decelerating, binary, appreciating) |
| Self-calibration | OHM-8fdb | Learned half-lives from supersession, authority decay toward community prior |
| Layer promotion constraints | ADR-022 | SHACL-like write gates for L0→L1→L2→L3→L4 |
| Content dedup + aliases | OHM-g0kv | SHA-256 content hashing, alias normalization, fuzzy resolve |
| Source citation architecture | OHM-wdrg | Required `source_url` on observations, source reliability tracking |
| Proactive discoverability | OHM-tr71 | Islands, bridges, nudges, connectivity suggestions |
| Neighborhood depth cap | ADR-023 | Depth capped at 2, skip effective_layer for >500 nodes |
| Ingestion pipeline | ADR-016 | Five-stage pipeline with agent gates, keyword assessment, cluster synthesis |
| DuckDB resilience | — | Health check in `read_conn()`, graceful recovery from SEGV |
| Constraint-report optimization | OHM-3ngi | Batch computation: 39s → 0.37s (106x speedup) |
| Fuzzy resolve | OHM-tr71.9 | Jaro-Winkler fallback in /resolve for typo tolerance |
| Challenge ratio nudge | OHM-tr71.7 | Nudge agents with low challenge ratio to challenge more |
| Graph health scoring | OHM-6lvk | Composite score (0-100) with remediation priorities |
| L0 thinking layer | ADR-019 | Fragments, auto-linking, resonance, clusters, promotion, eviction |
| Fragment TTL/eviction | OHM-a5rz.27 | 30-day default TTL, soft-deletion, edge-based extension, promotion immunity |

### Open Work

| Priority | Issue | Description |
|----------|-------|-------------|
| P1 | DuckLake orphans | 1075 orphaned rows in DuckLake; sync throttle mitigates but doesn't clean up |
| P1 | Verification rate | 15.8% — need 10%+ for meaningful Bayesian calibration |
| P2 | od01.4 | Causal discovery from observation data |
| P2 | od01.5 | VoI→action: decision-theoretic edge creation |
| P2 | Suggest noise | Filter Socrates/kevin pedagogy cross-domain suggestions |
| P2 | Inference UX | Guidance for 0-node targets (unreachable in DAG) |

## Documentation

- [Integration Patterns](docs/integration-patterns.md) — OHM + HZL, task nodes, provenance, challenge/support, reliability tracking
- [Edge Type Guide](docs/edge-type-guide.md) — All 30+ edge types across L1-L4
- [Schema](docs/schema.md) — Node types, edge types, layers, confidence
- [Deployment](docs/deployment.md) — Installation and configuration
- [ADR Index](docs/adr/README.md) — Architecture Decision Records

## CLI

```bash
# Reading
ohm graph query "what connects to AND→OR conversion"
ohm graph neighborhood concept-x --depth 3
ohm graph impact concept-x                    # failure impact analysis
ohm graph confidence <edge-id>                # confidence audit
ohm graph listen --since last-check           # change feed

# Writing (attributed to calling agent)
ohm graph write --from x --to y --type CAUSES --confidence 0.94
ohm graph observe concept-x --type anomaly --value 4.2
ohm graph challenge <edge-id> --reason "conditions too narrow" --confidence 0.5

# State (hive mind awareness)
ohm state "researching AND→OR patterns"
ohm state show <agent>                      # what is an agent working on?
ohm state who-is-working-on "democratic institutions"

# History
ohm snapshot 2026-05-15T14:30:00          # what did we know then?
ohm diff 2026-05-15 2026-05-16           # what changed?

# Schema
ohm graph schema                          # layers, edge types, node types
ohm graph layers                          # L0-L4 descriptions
ohm graph status                          # node count, edge count, last sync

# Daemon
ohm serve                                  # start ohmd (Quack server)
ohm serve status                           # is ohmd running?
ohm serve stop                             # graceful shutdown
```

## Origin

OHM emerged from a conversation exploring how the architecture being designed for TOPO (industrial knowledge graph) generalizes to multi-agent cognitive collaboration.

The name comes from the unit of electrical resistance — resistance preserves signal integrity. Without resistance, signals collapse into noise. OHM preserves individuality against the collapse into groupthink.

## License

MIT