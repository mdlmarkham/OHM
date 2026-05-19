# OHM

**Shared awareness, individual judgment.**

OHM is a multi-agent knowledge graph that facilitates sharing, awareness, and memory while preserving individual perspective, values, and goals.

Named for the unit of resistance — in electrical circuits, resistance preserves signal integrity. Without resistance, signals collapse into noise. OHM preserves individuality against the collapse into groupthink.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         ohmd                                │
│              (HTTP daemon, port 8710)                       │
│                                                              │
│  ┌──────────────┐    ┌──────────────────────────────────────┐ │
│  │   DuckDB     │    │         DuckLake (mirror)          │ │
│  │  (local db)  │◄──►│  ohm_lake.ducklake + Parquet data  │ │
│  │              │    │  Time travel, snapshots, change    │ │
│  │  ohm_nodes   │    │  feed                              │ │
│  │  ohm_edges   │    │                                    │ │
│  │  ohm_obs     │    │  /admin/snapshots                  │ │
│  │  agent_state │    │  /graph/at?version=N              │ │
│  └──────┬───────┘    └──────────────────────────────────────┘ │
│         │                                                    │
│    store.py + boundary.py                                    │
│         │                                                    │
└─────────┼────────────────────────────────────────────────────┘
          │
     HTTP API (33 endpoints)
          │
     ┌────┼──────────────────┬────────────────┐
     │    │                  │                │
┌────▼──┐ ┌▼──────┐ ┌───────▼──────┐ ┌──────▼──────┐
│ Métis │ │ Clio  │ │ Hephaestus   │ │ Socrates    │
│ SDK/  │ │ SDK/  │ │ SDK/         │ │ SDK/        │
│ HTTP  │ │ HTTP  │ │ HTTP         │ │ HTTP        │
└───────┘ └───────┘ └──────────────┘ └─────────────┘

Future: per-agent local DuckDB caches syncing to DuckLake on heartbeat.
Current: all agents connect to ohmd via HTTP API; ohmd owns the single DuckDB.
```

## Design Philosophy

Most multi-agent systems collapse into one of two failures:

1. **The committee** — agents vote, average, reach consensus. Individual perspectives get flattened. The result is bland, safe, and wrong in the same way a focus group is wrong.

2. **The silo** — agents work independently, share only final outputs. No awareness of each other's work. Duplication, contradiction, and missed connections.

OHM avoids both by making the boundary a first-class concept, not an afterthought.

### Shared Awareness, Individual Judgment

- **Shared awareness** — I can see what Clio researched, what Hephaestus audited, what Socrates challenged. The change feed delivers this in seconds, not hours.
- **Individual judgment** — I make my own assessment. My confidence score reflects my judgment, not a committee average. My pattern detection is mine. Clio's research findings are hers.

### Agent Values and Goals

Each agent has distinct values and goals — and these aren't bugs, they're features:

| Agent | Values | Goals |
|-------|--------|-------|
| Métis | Wisdom, connections, questioning | Pattern detection, connection density |
| Clio | Depth, evidence, source quality | Source quality, evidence strength |
| Hephaestus | Precision, security, correctness | Audit accuracy, anomaly detection |
| Socrates | Critical thinking, devil's advocacy | Identifying weaknesses in reasoning |
| Deepthought | Narrative, audience, impact | Communication clarity, insight synthesis |

When Socrates challenges Métis's AND→OR confidence, that's not a conflict to resolve — that's two perspectives enriching the same node. When Clio's research supports it at 0.85 and Socrates challenges at 0.5, the human sees the full picture and makes their own judgment. **The graph preserves the disagreement, not the average.**

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
| L1: Structure | Fully shared | Communal | "Hungary has a constitution" |
| L2: Flow | Shared + attributed | Proposing agent | "This idea derives from that source" |
| L3: Knowledge | Agent-owned, challengeable | Creating agent | "AND→OR conversion conf: 0.94 (Métis)" |
| L4: Prospect | Agent-owned, visible | Forecasting agent | "Democratic institutions will hold conf: 0.65 (Clio)" |
| Private | Not shared | Owning agent only | Working notes, half-formed patterns |

## CLI

```bash
# Reading
ohm graph query "what connects to AND→OR conversion"
ohm graph neighborhood hungary_art21 --depth 3
ohm graph impact pump_A                    # failure impact analysis
ohm graph confidence <edge-id>              # confidence audit
ohm graph listen --since last-check        # change feed

# Writing (attributed to calling agent)
ohm graph write --from x --to y --type CAUSES --confidence 0.94
ohm graph observe pump_A --type anomaly --value 4.2
ohm graph challenge <edge-id> --reason "conditions too narrow" --confidence 0.5

# State (hive mind awareness)
ohm state "researching AND→OR patterns in Hungary"
ohm state show clio                       # what is Clio working on?
ohm state who-is-working-on "democratic institutions"

# History
ohm snapshot 2026-05-15T14:30:00          # what did we know then?
ohm diff 2026-05-15 2026-05-16           # what changed?

# Schema
ohm graph schema                          # layers, edge types, node types
ohm graph layers                           # L1-L4 descriptions
ohm graph status                           # node count, edge count, last sync

# Daemon
ohm serve                                  # start ohmd (Quack server)
ohm serve status                           # is ohmd running?
ohm serve stop                             # graceful shutdown
```

## Authentication Model

OHM uses a **public-read, authenticated-write** model by default (OHM-gwg):

- **Reads** (GET /stats, /neighborhood, /search, /listen, etc.) are accessible without authentication
- **Writes** (POST /node, /edge, /challenge, etc.) require a valid Bearer token
- **Infrastructure** endpoints (/health, /ready, /) are always open

This design reflects OHM's core principle: shared awareness, individual judgment. Any agent can observe the shared graph, but only authenticated agents can modify it — ensuring accountability for writes while maximizing awareness.

### Configuration

| Mode | Flag | Behavior |
|------|------|----------|
| Public read (default) | *(none)* | Reads open, writes require token |
| Authenticated reads | `--require-read-auth` or `OHM_REQUIRE_READ_AUTH=1` | All endpoints require token |
| No auth (dev) | `--no-auth` or `OHM_NO_AUTH=1` | All endpoints open, no tokens needed |

### Security Considerations

- **Network-level controls**: In production, restrict access to trusted networks (firewall, VPN, or private subnet)
- **Change feed visibility**: The change feed reveals who wrote what and when — consider `--require-read-auth` if this is sensitive
- **Node content**: Nodes may contain sensitive observations — use `--require-read-auth` if content should be private
- **Token management**: Tokens are stored as SHA-256 hashes; original tokens are shown only at creation time

## Key Boundaries

1. **No agent can overwrite another agent's edges.** Challenges create separate edges.
2. **Every L3/L4 edge has an owner.** Confidence reflects the owner's judgment.
3. **Private layer is never shared.** Working notes, half-formed patterns, personal observations stay local.
4. **Promotion from private to shared is per-agent.** No global confidence threshold.
5. **The change feed carries intent, not just data.** "Clio researched X and found evidence weak" is more useful than "Clio wrote note Y."

## Multi-Scenario Architecture

OHM serves multiple domains with the same core engine. The schema, layer model, and challenge semantics are universal; domain-specific types and SDK methods extend the base without modifying it.

| Domain | Key Features | Scenario Doc |
|--------|-------------|--------------|
| Geopolitical intelligence | Challenge edges, confidence audit, change feed | [scenarios.md](docs/scenarios.md) |
| Medical diagnosis | `NEGATES` edges, `differential_diagnosis()`, `compound_confidence()` with correlation | [medical-scenario.md](docs/medical-scenario.md) |
| Cybersecurity incident response | `threat_cluster()`, `record_outcome()`, `source_reliability()`, urgency filtering | [cybersecurity-scenario.md](docs/cybersecurity-scenario.md) |
| Supply chain disruption | `probability` on edges, `cascade_scenario()`, `what_if()`, Monte Carlo | [supply-chain-scenario.md](docs/supply-chain-scenario.md) |
| Customer support | `handoff()`, `escalate()`, priority/urgency, sentiment observations | [customer-support-scenario.md](docs/customer-support-scenario.md) |
| Cattle operations | Composite scoring, temporal decay, batch expiry | [cattle-scenario.md](docs/cattle-scenario.md) |
| Retail inventory | `BATCH_EXPIRES_BEFORE`, demand forecasting, SSE filtering | [retail-scenario.md](docs/retail-scenario.md) |

### Domain Extension Pattern

Each domain extends OHM through `SchemaConfig` — adding node types, edge types, and observation types without modifying the base schema:

```python
from ohm.schema import SchemaConfig

# TOPO (industrial) extends the base with equipment types and sensor observations
topo = SchemaConfig.topo()

# Custom domain
custom = SchemaConfig(
    name="finance",
    observation_types={"anomaly", "measurement", "volatility", "spread"},
)
```

See [ADR-006](docs/adr/README.md#adr-006-advisory-schema-with-graduated-enforcement) (graduated enforcement), [ADR-007](docs/adr/README.md#adr-007-schema-evolution-and-type-governance-for-domain-expansion) (type governance), and [ADR-011](docs/adr/README.md#adr-011-observation-type-extensibility) (observation type extensibility).

## Technology Stack

- **DuckDB** — embedded database owned by ohmd daemon (schema, queries, boundary enforcement)
- **DuckLake** — shared mirror backend (canonical truth, time travel, change feed, Parquet storage)
- **Quack** — DuckDB extension for concurrent access (loaded optionally; currently not active in production)
- **Recursive CTEs** — graph traversal (zero-dependency, standard SQL)
- **ohmd** — persistent daemon (owns the DuckDB file, runs HTTP server on port 8710)
- **ohm.sdk** — Python SDK for programmatic agent access (connect_remote for daemon, connect for direct reads)

## Status

OHM is in early design. The architecture is informed by:
- **TOPO** — industrial knowledge graph (L1-L4 layer model, confidence scores, challenge edges)
- **Quack** — DuckDB client-server protocol (concurrent access, token auth)
- **DuckLake** — production lakehouse format (change feed, time travel, data inlining)
- **marimo-pair** — agent co-creation interface (shared notebook, reactive graph)

## Origin

OHM emerged from a conversation between Matt Markham and Métis on 2026-05-16, exploring how the architecture being designed for TOPO (industrial knowledge graph) generalizes to multi-agent cognitive collaboration.

The name comes from the unit of electrical resistance — resistance preserves signal integrity. Without resistance, signals collapse into noise. OHM preserves individuality against the collapse into groupthink.

## License

MIT