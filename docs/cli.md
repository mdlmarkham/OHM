# OHM CLI Design

## Command Reference

### Reading

```bash
ohm graph query <query>           # Natural language or structured query
ohm graph neighborhood <node-id>   # All edges within N hops (default: 3)
ohm graph neighborhood <node-id> --depth 2 --layer L3
ohm graph path <from> <to>         # Shortest path between two nodes
ohm graph impact <node-id>        # Failure impact analysis (downstream L2 + L3)
ohm graph confidence <edge-id>     # Full provenance, challenges, support
ohm graph listen [--since <ts>]    # Change feed since last check or timestamp
```

### Writing

```bash
ohm graph write --from <id> --to <id> --type CAUSES --confidence 0.94
ohm graph write --from <id> --to <id> --type DERIVES_FROM --layer L2
ohm graph observe <node-id> --type anomaly --value 4.2 --sigma 2.1
ohm graph challenge <edge-id> --reason "conditions too narrow" --confidence 0.5
ohm graph support <edge-id> --reason "3 additional cases" --confidence 0.85
ohm graph update <edge-id> --confidence 0.96 --provenance "expanded scope"
```

### State (Hive Mind Awareness)

```bash
ohm state "researching AND→OR patterns in Hungary"     # Set my current focus
ohm state show                                          # My current state
ohm state show clio                                     # What is Clio working on?
ohm state who-is-working-on "democratic institutions"  # Who's researching this?
ohm state history                                       # What have I been working on?
```

### History

```bash
ohm snapshot 2026-05-15T14:30:00    # What did we know at this time?
ohm diff 2026-05-15 2026-05-16     # What changed between these dates?
```

### Schema (Self-Documenting)

```bash
ohm graph schema      # Current node types, edge types, layers
ohm graph layers      # L1-L4 descriptions with examples
ohm graph status      # Node count, edge count, last sync, active agents
ohm graph stats       # Edge counts by layer, confidence distribution
```

### Daemon

```bash
ohm serve             # Start ohmd (Quack server, owns DuckDB file)
ohm serve status      # Is ohmd running? Connection info?
ohm serve stop        # Graceful shutdown (checkpoint, close connections)
ohm serve config      # Show current config (port, tokens, DuckLake path)
```

## Output Formats

All commands support `--format json` for machine-readable output (agents) and default to human-readable for interactive use.

```bash
# Human-readable (default)
ohm graph neighborhood hungary_art21 --depth 2
# L1: CONTAINS → hungary → democratic_institutions
# L2: DERIVES_FROM → reuters_investigation → hungary_art21
# L3: CAUSES (0.94, métis) → and_or_conversion → democratic_escape
# L3: CHALLENGED_BY (0.5, socrates) → "conditions too narrow"

# JSON (for agents)
ohm graph neighborhood hungary_art21 --depth 2 --format json
# {"nodes": [...], "edges": [...], "layers": {"L1": 2, "L2": 1, "L3": 2}}
```

## Change Feed Format

```bash
ohm graph listen --since last-check
# 
# Changes since 2026-05-16T08:00:00Z:
# 
# [L3] métis created CAUSES edge: and_or_pattern → hungary_democratic_escape (conf: 0.94)
# [L3] socrates challenged: and_or_pattern CAUSES hungary_democratic_escape (conf: 0.5, "conditions too narrow")
# [L3] clio created SUPPORTS edge: reuters_investigation → hungary_democratic_escape (conf: 0.85)
# [L1] atlas added node: supreme_court_ruling_2026
# [State] clio: researching DOJ self-dealing patterns
# 
# 5 changes, 3 agents active
```

## Exit Codes

- 0: Success
- 1: General error
- 2: Graph not found or ohmd not running
- 3: Authentication error (invalid token)
- 4: Permission denied (trying to overwrite another agent's edge)
- 5: Node or edge not found