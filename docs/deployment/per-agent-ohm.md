# Deploying OHM for a Single Agent — Local DuckDB Mode

This guide covers the simplest OHM deployment: one agent, one local DuckDB file, no daemon, no HTTP. This is the mode used by a single developer who wants fast local memory before deciding whether to share with a team daemon.

This guide implements the packaging/defaults workstream for the small-team multi-agent mesh (`OHM-s139`).

---

## Table of contents

1. [When to use local mode](#when-to-use-local-mode)
2. [Quick start with `ohm standup --mode local`](#quick-start-with-ohm-standup---mode-local)
3. [Manual SDK setup](#manual-sdk-setup)
4. [Optional DuckLake sync](#optional-ducklake-sync)
5. [Path layout](#path-layout)
6. [Upgrading and backup](#upgrading-and-backup)
7. [Moving to a shared daemon](#moving-to-a-shared-daemon)

---

## When to use local mode

Use local mode when:

- You are the only user/agent on the machine.
- Latency matters more than sharing.
- You want zero infrastructure: no `ohmd`, no systemd, no port.
- You may later sync to a team DuckLake or migrate to a daemon.

Do **not** use local mode when:

- Multiple agents on the same machine need concurrent writes to the same DB.
- You need HTTP-based tools (MCP, remote SDK, CI/CD).

For those cases, use [Local Daemon + Per-Tenant MCP](local-copilot-ohm.md) or [Remote Daemon](remote-copilot-ohm.md).

---

## Quick start with `ohm standup --mode local`

### 1. Install OHM

```bash
pip install ohm
```

### 2. Create the local store

```bash
ohm standup --mode local --agent-id my-agent
```

You will be prompted whether to configure DuckLake sync. Say `n` for a fully local store, or `y` and provide a shared DuckLake path if you want to push/pull team memory.

Expected output:

```text
OHM standup — local per-agent store
? Configure DuckLake sync for shared knowledge? [y/N]: n
  Creating local store for agent 'my-agent' ...
  ✓ Local DB ready at /home/you/.ohm/agents/my-agent/ohm.duckdb
  ✓ Agent config written to /home/you/.ohm/agents/my-agent/agent.json
  ✓ Marker node written and verified
```

### 3. Use the store from Python

```python
from ohm.store import OhmStore

store = OhmStore.for_agent("my-agent")

store.write_node(
    id="concept-local-first",
    label="Local-first OHM memory",
    type="concept",
    content="This node lives in my local DuckDB and may sync to DuckLake.",
    confidence=0.9,
    provenance="my-agent",
)

node = store.get_node("concept-local-first")
print(node)
```

### 4. (Optional) sync to shared DuckLake

If you configured DuckLake, call sync on heartbeat:

```python
result = store.sync_heartbeat()
print(result)
# → {"pushed": 3, "pulled": 7, "last_sync": "..."}
```

If you did not configure sync, you can move the local DB to a shared daemon later.

---

## Manual SDK setup

If you prefer not to use `ohm standup`, create the store directly:

```python
from ohm.store import OhmStore

store = OhmStore.for_agent(
    agent_name="my-agent",
    # optional: point at a shared DuckLake for sync
    # ducklake_path="/var/lib/ohm/ohm_lake.ducklake",
)
```

`OhmStore.for_agent()` will:

- Create `~/.ohm/agents/my-agent/` if it does not exist.
- Create `~/.ohm/agents/my-agent/ohm.duckdb`.
- Initialize the OHM schema.
- Attach and pull from DuckLake if a path is provided and available.

---

## Optional DuckLake sync

DuckLake is a shared catalog that lets multiple per-agent stores exchange memory without a central daemon. It is the recommended bridge between local mode and team sharing (`OHM-ur0u`).

### Requirements

- A writable DuckLake catalog file and data directory.
- The same catalog accessible to all agents that need to share.

### Configure at standup time

```bash
ohm standup --mode local --agent-id my-agent
# → y
# → DuckLake path: /var/lib/ohm/ohm_lake.ducklake
```

### Configure later

Edit `~/.ohm/agents/my-agent/agent.json`:

```json
{
  "agent_id": "my-agent",
  "mode": "local",
  "db_path": "/home/you/.ohm/agents/my-agent/ohm.duckdb",
  "ducklake_path": "/var/lib/ohm/ohm_lake.ducklake"
}
```

Then call `sync_heartbeat()` from your agent.

### Concurrency note

DuckLake uses file locks. Only one agent should sync at a time. If you need concurrent writers, use [Quack mode](quack-mode.md) (`OHM-gdql`) or a shared `ohmd` daemon.

---

## Path layout

After `ohm standup --mode local --agent-id my-agent`:

```text
~/.ohm/
└── agents/
    └── my-agent/
        ├── agent.json       # standup-generated metadata
        ├── ohm.duckdb       # local knowledge graph
        └── ohm.duckdb.wal   # write-ahead log while open
```

The `agent.json` file is read-only metadata; the actual connection is through `OhmStore.for_agent("my-agent")`.

---

## Upgrading and backup

### Backup

```bash
cp -r ~/.ohm/agents/my-agent ~/.ohm/agents/my-agent.backup.$(date +%Y%m%d)
```

### Schema upgrades

When you upgrade the `ohm` Python package, the next `OhmStore.for_agent()` call will run schema migrations automatically. If you keep the package up to date, no manual SQL is required.

### Pruning

Local mode has no automatic retention policy. To limit disk usage:

- Delete old observations via the SDK.
- Use `store.sync_heartbeat()` to push stable memories to DuckLake, then start a fresh local DB.

---

## Moving to a shared daemon

When you outgrow local mode, migrate to a system-level daemon with per-tenant MCP:

1. Stand up a shared `ohmd --multi-tenant` daemon: see [Local Daemon Deployment](local-copilot-ohm.md).
2. Provision a tenant for your agent.
3. Re-point your agent to use the daemon via SDK or MCP instead of `OhmStore.for_agent()`.
4. Optionally seed the new tenant from your local DuckDB by replaying important nodes/edges.

There is currently no automatic migration tool; this is tracked under `OHM-s139` follow-ups.

---

## Growth-stage summary

| Stage | Recommended topology |
|---|---|
| 1 agent, 1 laptop | `ohm standup --mode local` |
| 2–3 agents, same LAN, occasional sharing | Local mode + shared DuckLake |
| 2+ agents, real-time collaboration, or remote access | `ohm standup --mode greenfield` + local MCP sidecars |
| SaaS / CI / browser / mobile agents | Daemon + `ohm-gateway` (ADR-028) |

### Path A: federated sharing via DuckLake

Add a shared DuckLake catalog to an existing local store:

```bash
# Edit ~/.ohm/agents/my-agent/agent.json
{
  "agent_id": "my-agent",
  "mode": "local",
  "db_path": "/home/you/.ohm/agents/my-agent/ohm.duckdb",
  "ducklake_path": "/var/lib/ohm/ohm_lake.ducklake"
}
```

Then call `store.sync_heartbeat()` from each agent. This is the smallest step up from solo local mode and avoids introducing a daemon or network surface.

### Path B: remote MCP connections

When agents are off-machine or you need real-time shared state:

```bash
# 1. Stand up a multi-tenant daemon
sudo ohm standup --mode greenfield \
  --multi-tenant \
  --template personal-knowledge \
  --tenant shared \
  --agent-id my-agent \
  --service-mode systemd

# 2. Optionally deploy the hosted gateway (ADR-028)
ohm-gateway --config /etc/ohm/gateway.json
```

Re-point the local agent from `OhmStore.for_agent("my-agent")` to the HTTP SDK using the generated customer key.

### Hybrid mode

You can run both at once:

- Keep a **local DuckDB** for private scratch notes and fast drafts.
- Use the **shared daemon** for team-vetted knowledge.
- Periodically promote important local nodes to the shared tenant via the SDK or MCP tools.

This matches the `promote_to_shared` pattern described in the agent network runbook.

---

## Related

- [Single-Project Deployment](single-project-ohm.md) — library mode without even a per-agent directory.
- [Local Daemon Deployment](local-copilot-ohm.md) — one daemon, multiple tenants, MCP sidecars.
- [Remote Daemon Deployment](remote-copilot-ohm.md) — HTTPS/TLS shared daemon.
- `OHM-s139` — lightweight per-agent OHM deployment packaging.
- `OHM-ur0u` — selective cross-instance sharing.
- `OHM-gdql` — Quack multi-reader mode.
