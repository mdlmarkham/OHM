# OHM

**Shared awareness, individual judgment.**

OHM is a multi-agent knowledge graph that facilitates sharing, awareness, and memory while preserving individual perspective, values, and goals.

Named for the unit of resistance — in electrical circuits, resistance preserves signal integrity. Without resistance, signals collapse into noise. OHM preserves individuality against the collapse into groupthink.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    DuckLake (shared truth)                   │
│                                                              │
│  L1: Structure  — Fully shared, all agents read/write       │
│  L2: Flow       — Shared with attribution                   │
│  L3: Knowledge  — Agent-owned, challengeable                 │
│  L4: Prospect   — Agent-owned, visible                        │
│  Private        — Agent-only, not shared                      │
│                                                              │
│  Change feed: who wrote what when (awareness)                 │
│  Time travel: what did the graph know at time T?              │
│  Agent state: what is each agent thinking?                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
                      Quack (HTTP)
                           │
     ┌─────────────────────┼─────────────────────┐
     │                     │                       │
┌────▼─────┐        ┌──────▼──────┐        ┌──────▼──────┐
│  Métis   │        │   Clio      │        │ Hephaestus  │
│ Local    │        │ Local       │        │ Local       │
│ DuckDB   │        │ DuckDB      │        │ DuckDB      │
│ cache    │        │ cache       │        │ cache      │
│          │        │             │        │             │
│ Working  │        │ Research    │        │ Audit       │
│ memory   │        │ notes       │        │ findings    │
│          │        │             │        │             │
│ marimo   │        │ marimo      │        │ marimo      │
│ notebook │        │ notebook    │        │ notebook    │
└──────────┘        └─────────────┘        └─────────────┘
```

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

## Key Boundaries

1. **No agent can overwrite another agent's edges.** Challenges create separate edges.
2. **Every L3/L4 edge has an owner.** Confidence reflects the owner's judgment.
3. **Private layer is never shared.** Working notes, half-formed patterns, personal observations stay local.
4. **Promotion from private to shared is per-agent.** No global confidence threshold.
5. **The change feed carries intent, not just data.** "Clio researched X and found evidence weak" is more useful than "Clio wrote note Y."

## Technology Stack

- **DuckDB** — local cache per agent (working memory)
- **DuckLake** — shared backend (canonical truth, time travel, change feed)
- **Quack** — concurrent access (multi-agent reads/writes via HTTP)
- **Recursive CTEs** — graph traversal (zero-dependency, standard SQL)
- **ohmd** — persistent daemon (owns the DuckDB file, runs Quack server)

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