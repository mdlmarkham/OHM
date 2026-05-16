# OHM CLI Reference

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
- `OHM_AGENT` — Default agent name for attribution (default: `ohm`)
- `OHM_CONFIG` — Path to ohmd config file (default: `~/.ohm/ohmd.json`)

## Commands

### Reading

```bash
ohm graph neighborhood <node-id>           # All edges within N hops (default: 3)
ohm graph neighborhood <node-id> --depth 2 --layer L3
ohm graph path <from> <to>                  # Shortest path between two nodes
ohm graph impact <node-id>                   # Downstream impact analysis
ohm graph confidence <edge-id>              # Audit: challenges, supports, provenance
ohm graph listen [--since <timestamp>]      # Change feed since last check
```

### Writing

```bash
# Create/update a node
ohm graph write --id <id> --label <label> --type <type> [--content <text>] \
    [--confidence 0.94] [--visibility team] [--provenance research] [--tags tag1,tag2]

# Create an edge
ohm graph write --from <id> --to <id> --edge-type CAUSES --layer L3 \
    [--confidence 0.94] [--condition <json>] [--provenance conversation]

# Create an observation
ohm graph observe <node-id> --type anomaly --value 4.2 --sigma 2.1 --source signal

# Challenge an edge (creates new edge, does not modify original)
ohm graph challenge <edge-id> --reason "conditions too narrow" --confidence 0.5

# Support an edge
ohm graph support <edge-id> --reason "3 additional cases confirmed" --confidence 0.85
```

### State (Hive Mind Awareness)

```bash
ohm state set "researching AND→OR patterns" --patterns "and-or,hungary" --services "research,critique"
ohm state show              # All agents
ohm state show metis        # Specific agent
ohm state who-is-working-on "democratic institutions"
```

### Schema & Status

```bash
ohm graph status            # Node count, edge count, last sync
ohm graph schema            # Node types, edge types, layers
ohm graph layers             # L1-L4 descriptions with examples
```

### Daemon

```bash
ohm serve start [--host 127.0.0.1] [--port 8710]    # Start ohmd
ohm serve status                                       # Check if running
ohm serve token <agent_name>                           # Generate auth token
```

## Output Formats

All commands support `--format json` for machine-readable output:

```bash
# Human-readable (default)
ohm graph neighborhood hungary_art21 --depth 2
# L1: CONTAINS → hungary → democratic_institutions
# L3: CAUSES (0.94, métis) → and_or_conversion → democratic_escape

# JSON (for agents)
ohm graph neighborhood hungary_art21 --depth 2 --format json
```

## Agent Attribution

All writes are attributed to the calling agent. Set your agent name via:

```bash
# Environment variable
export OHM_AGENT=metis

# Or per-command
ohm graph write --from X --to Y --type CAUSES --agent metis
```

## Boundary Rules

1. **Any agent can write** to L1 (Structure) and L2 (Flow) layers
2. **Only the owning agent** can update their own L3/L4 edges
3. **Any agent can challenge** any L3/L4 edge (creates a new edge, never modifies)
4. **No agent can delete** another agent's edge
5. **Private layer** is never shared or promoted automatically

## Exit Codes

- 0: Success
- 1: General error
- 2: Graph not found or ohmd not running
- 3: Authentication error (invalid token)
- 4: Permission denied (trying to overwrite another agent's edge)
- 5: Node or edge not found