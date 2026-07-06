# Single-Project OHM Deployment

The simplest way to use OHM: one agent, one local DuckDB, no daemon, no HTTP server, no MCP. The agent uses OHM as a Python library with zero network overhead.

This is the right choice when:
- You have one agent (or one project) and don't need multi-tenant isolation.
- You want zero-latency local reads and writes.
- You don't need an HTTP API for other tools.
- You might sync to a shared DuckLake later but want to start simple.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Quick start](#quick-start)
3. [With DuckLake sync](#with-ducklake-sync)
4. [From single project to local daemon](#from-single-project-to-local-daemon)
5. [Security](#security)
6. [Troubleshooting](#troubleshooting)

---

## How it works

```text
┌─────────────────────────┐
│       Your agent        │
│  (Python process)       │
│                         │
│  OhmStore.for_agent()   │
│         │               │
│  ┌──────▼──────┐        │
│  │ local DuckDB │        │
│  │ ~/.ohm/agents│        │
│  │  /{name}/    │        │
│  │  ohm.duckdb  │        │
│  └─────────────┘        │
└─────────────────────────┘

Optional later:
  DuckLake sync ←→ shared Parquet ←→ other agents
```

No daemon. No HTTP. No MCP. Just a Python library reading and writing a local DuckDB file.

---

## Quick start

### 1. Install OHM

```bash
pip install ohm
```

### 2. Use the store

```python
from ohm.store import OhmStore

store = OhmStore.for_agent("my-agent")

# Write
store.write_node(id="concept-x", label="My Concept", type="concept",
                 content="This is a test node")
store.write_edge(from_node="concept-x", to_node="concept-y",
                edge_type="CAUSES", layer="L3", confidence=0.85)

# Read
node = store.get_node("concept-x")
neighbors = store.neighborhood("concept-x", depth=2)

# Query
results = store.search("test")
```

That's it. The local DuckDB is created automatically at `~/.ohm/agents/my-agent/ohm.duckdb`.

### 3. Optional: domain template

If your project uses a domain-specific schema (e.g., devsecops, manufacturing), pass it during initialization:

```python
store = OhmStore.for_agent(
    agent_name="my-agent",
    domain_config="devsecops.json"  # or "topo.json", "datapipelines.json"
)
```

This creates domain tables (e.g., `topo_events`, `topo_plans`) alongside the core OHM schema.

---

## With DuckLake sync

When you want multiple agents or machines to share knowledge, add a DuckLake sync target.

```python
from ohm.store import OhmStore

store = OhmStore.for_agent(
    agent_name="my-agent",
    ducklake_path="s3://my-bucket/ohm-lake.ducklake"  # or local path
)

# Normal reads/writes are local (zero latency)
store.write_node(id="concept-z", label="Z", type="concept")

# Sync pushes local changes to DuckLake and pulls changes from other agents
result = store.sync_heartbeat()
print(result)  # → {"pushed": 2, "pulled": 5, "last_sync": "2026-07-06T12:00:00Z"}
```

DuckLake stores data as Parquet files, so it works with any S3-compatible storage (AWS, GCS, MinIO, local filesystem).

### Sync schedule

```python
import time

while True:
    store.sync_heartbeat()
    time.sleep(300)  # sync every 5 minutes
```

Or use your agent's heartbeat loop — `sync_heartbeat()` is idempotent and fast.

### Conflict resolution

DuckLake uses last-write-wins by `updated_at` timestamp. In practice, knowledge graph conflicts are rare because agents write different perspectives (different nodes and edges), not competing updates to the same row.

---

## From single project to local daemon

When you outgrow single-project mode — you add a second agent, need MCP access for Copilot, or want multi-tenant isolation — you can add a daemon without changing your data:

1. **Start `ohmd`** pointing at your existing DuckDB (or DuckLake).
2. **Provision tenants** for each context (devops, dataops, etc.).
3. **Add MCP sidecars** for Copilot or other agents.
4. **Continue using `OhmStore.for_agent()`** for local operations — the daemon is optional alongside the library.

The local DuckDB continues to work. The daemon adds HTTP endpoints and MCP access. No migration needed.

See [Local Agent Deployment](local-copilot-ohm.md) for the full guide.

For other scenarios:
- **Multi-tenant local**: [Local Agent Deployment](local-copilot-ohm.md) / [Windows](windows-copilot-ohm.md) — system daemon with per-tenant MCP.
- **Remote**: [Remote Daemon Deployment](remote-copilot-ohm.md) — agents on multiple machines, CI/CD, cloud.

---

## Security

In single-project mode, security is simple:

- The local DuckDB file is only readable by the current user.
- No network exposure — no ports, no tokens, no TLS.
- DuckLake sync credentials (if used) should be stored in environment variables or a secrets manager, not in the code.

```bash
# DuckLake credentials via environment
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export DUCKLAKE_PATH=s3://my-bucket/ohm-lake.ducklake
```

```python
import os
store = OhmStore.for_agent(
    agent_name="my-agent",
    ducklake_path=os.environ["DUCKLAKE_PATH"]
)
```

---

## Troubleshooting

### DuckDB file locked

Only one process can write to a DuckDB file at a time. If you see "database is locked", either:
- Use DuckLake sync (multiple agents, each with their own local DB).
- Close the conflicting connection.

### DuckLake sync fails

- Check S3 credentials and bucket permissions.
- Ensure the DuckLake path is writable.
- Check network connectivity to the S3 endpoint.

### Schema errors after upgrading OHM

Run `store.migrate()` to apply any pending schema migrations. The store logs the migration version on startup.

### Missing domain tables

Pass `domain_config` when creating the store. Domain tables are only created when a domain template is specified.

```python
store = OhmStore.for_agent("my-agent", domain_config="devsecops.json")
```
