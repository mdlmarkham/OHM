# OHM CLI Reference

ADR-005: Self-documenting CLI as agent interface. Agents call `ohm`, not raw SQL.
Every command has `--help` and `--format json` for machine-readable output.

## Installation

```bash
pip install ohm
```

Or from source:
```bash
git clone https://github.com/mdlmarkham/OHM.git
cd OHM
pip install -e ".[dev]"
```

## Configuration

Environment variables:
- `OHM_DB_PATH` — Path to DuckDB file (default: `~/.ohm/ohm.duckdb`)
- `OHM_ACTOR` — Default agent name for attribution (default: `unknown`)
- `OHM_CONFIG` — Path to ohmd config file (default: `~/.ohm/ohmd.json`)
- `OHM_NO_AUTH` — Set to `1` to disable authentication (dev mode only)

Global flags (available on all commands):
- `--db <path>` — Database path override
- `--actor <name>` — Agent name for attribution
- `--format human|json` — Output format (default: human)

---

## Graph Commands (`ohm graph`)

### Schema & Status

```bash
# Show node types, edge types, and layer descriptions
ohm graph schema
ohm graph schema --format json

# Show L1-L4 layer descriptions with examples
ohm graph layers

# Quick status: node count, edge count, schema version, active agents
ohm graph status
ohm graph status --format json

# Detailed statistics: edges by layer/type, nodes by type, challenge ratio
ohm graph stats
ohm graph stats --format json

# Apply pending schema migrations
ohm graph upgrade
ohm graph upgrade --dry-run    # Show what would be applied
```

### Reading the Graph

```bash
# Bounded-depth traversal from a node (default: 3 hops)
ohm graph neighborhood <node-id>
ohm graph neighborhood <node-id> --depth 2 --layer L3
ohm graph neighborhood <node-id> --direction outgoing
ohm graph neighborhood <node-id> --mermaid    # Mermaid diagram output

# Shortest path between two nodes (BFS, max depth 10)
ohm graph path <from-id> <to-id>
ohm graph path <from-id> <to-id> --max-depth 5
ohm graph path <from-id> <to-id> --mermaid    # Mermaid diagram output

# Downstream failure impact analysis (follows L2/L3 edges forward)
ohm graph impact <node-id>
ohm graph impact <node-id> --depth 5
ohm graph impact <node-id> --mermaid          # Mermaid diagram output

# Full provenance and challenge audit for an edge
ohm graph confidence <edge-id>

# Change feed — what changed since a timestamp
ohm graph listen
ohm graph listen --since 2026-05-01T00:00:00Z
ohm graph listen --since last-check

# Server-Sent Events stream — real-time change feed
ohm graph events
ohm graph events --since 2026-05-01T00:00:00Z
ohm graph events --topics "democracy,constitution"
ohm graph events --agent metis

# Natural language or structured query
ohm graph query "democratic backsliding"
ohm graph query --filter-type CAUSES --layer L3 --confidence-min 0.7
```

### Writing the Graph

```bash
# Create an edge (auto-creates nodes if they don't exist)
ohm graph write --from <id> --to <id> --type CAUSES --layer L3
ohm graph write --from <id> --to <id> --type CAUSES --layer L3 \
    --confidence 0.94 --condition "when supermajority controls parliament" \
    --provenance research

# Update your own edge (only the owner can update)
ohm graph update <edge-id> --confidence 0.95
ohm graph update <edge-id> --provenance "updated after peer review"
ohm graph update <edge-id> --condition "revised scope"

# Record an observation on a node
ohm graph observe <node-id> --type measurement --value 4.2 --sigma 0.3
ohm graph observe <node-id> --type anomaly --value 8.5 --sigma 2.1 \
    --baseline 3.0 --source signal

# Challenge an edge (creates CHALLENGED_BY, never modifies original)
ohm graph challenge <edge-id> --reason "conditions too narrow" --confidence 0.5

# Support an edge (creates SUPPORTS, never modifies original)
ohm graph support <edge-id> --reason "3 additional cases confirmed" --confidence 0.85
```

### Substrate Methods (deterministic, agent-independent)

```bash
# Combine multiple observations on a node into a single value
ohm graph aggregate <node-id>
ohm graph aggregate <node-id> --method weighted       # Inverse-variance (default)
ohm graph aggregate <node-id> --method mean           # Simple arithmetic mean
ohm graph aggregate <node-id> --method max_confidence # Highest-confidence observation
ohm graph aggregate <node-id> --method consensus      # Majority-direction check

# Detect anomalous observations (|value - baseline| / sigma > threshold)
ohm graph anomalies
ohm graph anomalies --sigma 2.0 --layer L3 --limit 50

# Graph structural health metrics (orphans, low-confidence edges, stale agents)
ohm graph health

# Apply confidence decay to stale edges (exponential, 30-day half-life)
ohm graph decay
ohm graph decay --threshold 0.1 --layer L3
ohm graph decay --dry-run    # Show what would decay without modifying
```

---

## State Commands (`ohm state`)

Hive mind awareness — know what other agents are working on.

```bash
# Set your current focus
ohm state set "researching AND→OR conversion patterns"

# Show all agents' state
ohm state show

# Show a specific agent's state
ohm state show metis

# Find collaborators by topic
ohm state who-is-working-on "democratic institutions"

# View focus history for an agent
ohm state history metis
```

---

## TOPO Commands (`ohm topo`)

Industrial knowledge graph — same engine, domain-specific schema.

```bash
# Show TOPO schema (industrial node/edge types)
ohm topo schema
ohm topo schema --format json

# Trace failure propagation from a node (industrial impact analysis)
ohm topo failure-analysis <node-id>
ohm topo failure-analysis <node-id> --depth 3
ohm topo failure-analysis <node-id> --edge-type FEEDS --edge-type DEPENDS_ON

# Map compliance relationships around a node
ohm topo compliance-map <node-id>
ohm topo compliance-map <node-id> --format json

# Comprehensive impact study (failure analysis + neighborhood)
ohm topo impact-study <node-id>
ohm topo impact-study <node-id> --depth 5
ohm topo impact-study <node-id> --format json
```

---

## Time Travel Commands

```bash
# Query graph state at a historical timestamp
ohm snapshot <iso-timestamp>
ohm snapshot 2026-05-01T00:00:00Z
ohm snapshot 2026-05-01T00:00:00Z --node <node-id>
ohm snapshot 2026-05-01T00:00:00Z --format json

# What changed between two timestamps
ohm diff <from-timestamp> <to-timestamp>
ohm diff 2026-05-01T00:00:00Z 2026-05-15T00:00:00Z
ohm diff 2026-05-01T00:00:00Z 2026-05-15T00:00:00Z --layer L3
ohm diff 2026-05-01T00:00:00Z 2026-05-15T00:00:00Z --agent metis
ohm diff 2026-05-01T00:00:00Z 2026-05-15T00:00:00Z --format json
```

---

## Daemon Commands (`ohm serve`)

```bash
# Start ohmd (HTTP + optional Quack server)
ohm serve start
ohm serve start --host 127.0.0.1 --port 8710
ohm serve start --no-auth    # Dev mode, no authentication required

# Check if ohmd is running
ohm serve status

# Show current daemon configuration
ohm serve config

# Generate an auth token for an agent
ohm serve token <agent-name>
ohm serve token metis --role read-write
```

---

## Output Formats

All commands support `--format json` for machine-readable output:

```bash
# Human-readable (default)
ohm graph neighborhood hungary_art21 --depth 2
# [hop 1] [L1] CONTAINS: hungary → democratic_institutions (conf: 1.0, by: metis)
# [hop 2] [L3] CAUSES: and_or_conversion → democratic_escape (conf: 0.94, by: metis)

# JSON (for agents and scripts)
ohm graph neighborhood hungary_art21 --depth 2 --format json
# [{"hop": 1, "layer": "L1", "edge_type": "CONTAINS", ...}, ...]

# Mermaid diagram (for documentation and notebooks)
ohm graph neighborhood hungary_art21 --depth 2 --mermaid
# ```mermaid
# flowchart LR
#     hungary_art21[hungary_art21]
#     democratic_institutions[democratic_institutions]
#     hungary_art21 -->|CONTAINS| democratic_institutions
# ```
```

---

## Agent Attribution

All writes are attributed to the calling agent. Set your agent name via:

```bash
# Environment variable (recommended)
export OHM_ACTOR=metis

# Or per-command
ohm graph write --from X --to Y --type CAUSES --actor metis
```

---

## Boundary Rules (ADR-003)

1. **Any agent can write** to L1 (Structure) and L2 (Flow) layers
2. **Only the owning agent** can update their own L3/L4 edges
3. **Any agent can challenge** any L3/L4 edge (creates a new edge, never modifies)
4. **No agent can delete** another agent's edge
5. **Private layer** is never shared or promoted automatically

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error / validation error |
| 2 | Daemon not running |
| 3 | Authentication error |
| 4 | Permission denied |
| 5 | Node or edge not found |

---

## See Also

- [Schema Reference](schema.md) — Node types, edge types, layer descriptions
- [Architecture Decisions](adr/0001-architecture-decisions.md) — ADR-001 through ADR-005
- [Agent Instructions](../AGENTS.md) — How coding agents should interact with OHM
- [Deployment Guide](deployment.md) — TLS, systemd, production configuration
- 3: Authentication error (invalid token)
- 4: Permission denied (trying to overwrite another agent's edge)
- 5: Node or edge not found