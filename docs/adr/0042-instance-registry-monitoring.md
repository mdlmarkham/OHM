# ADR-042: Instance Registry and Monitoring for Local Agent Mesh

**Date:** 2026-07-08
**Status:** Accepted
**Related issues:** OHM-yzyk.5 (this work), ADR-002 (ohmd daemon), ADR-012 (per-agent local cache), ADR-015 (multi-tenancy)

## Context

In a small-team multi-agent mesh, multiple OHM instances run on a single host and across the network:

- **One `ohmd` daemon** owns the canonical DuckDB file and serves all HTTP clients (ADR-002).
- **Per-agent local stores** (`~/.ohm/agents/{name}/ohm.duckdb`) give each agent zero-latency reads and writes with periodic DuckLake sync (ADR-012).
- **Remote instances** may run on other hosts for multi-tenant deployments (ADR-015) or staging environments.

Operators and agents need to answer three questions that had no single tool to answer:

1. **Which instances exist?** There was no registry. An operator had to know URLs out of band. An agent connecting via SDK had to be told the endpoint explicitly.
2. **Are they healthy?** `GET /health` existed but only for a single known endpoint. Checking N instances meant N manual `curl` invocations.
3. **What is their sync status?** DuckLake sync lag (ADR-004) was not surfaced anywhere operators could see at a glance. Multi-tenant deployments had no way to enumerate which tenants and domain configs were live on a given instance.

No centralized registry existed. Discovery was tribal knowledge — the URL was passed through environment variables, config files, or human memory.

## Decision

A local-first, pull-based instance registry built on three pieces: a self-describing endpoint on each `ohmd`, a CLI discovery scanner that probes well-known locations, and a shared registry JSON file that both the SDK and MCP server can consume.

### 1. `GET /instance` — self-describing instance metadata

Each `ohmd` exposes `GET /instance` returning structured metadata about itself. The handler is `_get_instance` (`src/ohm/server/handlers/infra.py:211`), registered as a no-auth route so discovery tools can probe without credentials (`src/ohm/server/server.py:781`).

**No auth is intentional.** `/instance` sits alongside `/health`, `/ready`, and `/metrics` in the always-open infrastructure route list. A discovery probe must not require a token — the whole point is that a freshly started agent or operator can find what is running without already having credentials. The endpoint exposes identity and runtime state, not graph contents, so the information disclosure is minimal.

**Response shape:**

| Field | Type | Purpose |
|-------|------|---------|
| `instance_id` | string | Stable identifier (default `ohmd-{hostname}`, overridable via config `instance_id`) |
| `version` | string | OHM package version (`ohm.__version__`) |
| `purpose` | string | Human-readable role (e.g. "production", "staging") from config |
| `multi_tenant` | bool | Whether ADR-015 multi-tenancy is active |
| `tenants` | list[string] | Active tenant IDs (only when multi-tenant) |
| `domain_configs` | map[string, string] | Tenant → domain config name (only when multi-tenant) |
| `listen_url` | string | `http://{host}:{port}` the daemon is bound to |
| `ducklake` | object | `{enabled, sync_url, last_sync_at, lag_seconds}` — DuckLake sync status |
| `started_at` | ISO 8601 | Daemon start timestamp |
| `uptime_seconds` | float | Seconds since `_START_TIME` |
| `agent_count` | int | Distinct `created_by` agents active in the last 24h of edges |

The handler gathers tenant info from `ohm_tenants`, DuckLake sync status from `ohm_sync_state`, and agent activity from `ohm_edges` — all defensively wrapped so a missing table or query failure does not break the response.

### 2. `ohm instances discover` — well-known location scan + probe

The `ohm instances` CLI subcommand tree (`src/ohm/cli/__init__.py:728`) provides four subcommands: `list`, `discover`, `health`, and `show`.

`discover` (`_discover_instances`, `src/ohm/cli/__init__.py:3038`) assembles a candidate URL list from four well-known sources, then probes each with `GET /instance`:

| Source | What it reads | Why |
|--------|---------------|-----|
| Default port | `http://127.0.0.1:8710` | The canonical ohmd bind address |
| `OHM_URL` env var | `os.environ["OHM_URL"]` | Single explicit override |
| Per-agent configs | `~/.ohm/agents/*/ohm.json` → `ohm_url`/`url` key | Each agent's local store dir may record its endpoint |
| System configs | `/etc/ohm/ohmd*.json` → `host`/`port` | systemd-managed daemon configs, including multi-instance setups |

Each candidate is probed with a short timeout (default 3s). A successful probe records the full `/instance` payload plus `discovered_url` and `health: "ok"`. A failed probe records a stub with `health: "unreachable"` and a truncated error string, so the registry shows what was tried, not just what worked.

### 3. Registry JSON at `~/.ohm/registry.json`

The discover command writes a registry file consumable by both the SDK and the MCP server:

```json
{
  "version": "1",
  "discovered_at": "2026-07-08T12:00:00+00:00",
  "instances": [
    {
      "instance_id": "ohmd-prod-host",
      "version": "0.42.0",
      "purpose": "production",
      "multi_tenant": true,
      "tenants": ["acme", "globex"],
      "domain_configs": {"acme": "default", "globex": "topo"},
      "listen_url": "http://127.0.0.1:8710",
      "ducklake": {"enabled": true, "sync_url": "...", "last_sync_at": "...", "lag_seconds": 4.2},
      "started_at": "2026-07-08T00:00:00+00:00",
      "uptime_seconds": 43200.0,
      "agent_count": 5,
      "discovered_url": "http://127.0.0.1:8710",
      "health": "ok"
    }
  ]
}
```

The path defaults to `~/.ohm/registry.json` and is overridable via `--output` / `--registry` flags on the CLI subcommands. The file is plain JSON — no server, no daemon, no lock. Any consumer reads it directly.

### 4. Prometheus `/metrics` endpoint

`GET /metrics` (`_get_infra_metrics`, `src/ohm/server/handlers/infra.py:312`) emits Prometheus exposition-format text when the client requests it (via `?format=prometheus` or `Accept: text/plain`). The OHM-yzyk.5 work extended the existing metrics with graph-level and instance-level gauges:

| Metric | Type | Source |
|--------|------|--------|
| `ohm_uptime_seconds` | gauge | Daemon uptime |
| `ohm_requests_total{method}` | counter | HTTP request counts by method |
| `ohm_errors_total{code}` | counter | 4xx / 5xx error counts |
| `ohm_rate_limited_total` | counter | Requests rejected by rate limiter |
| `ohm_request_duration_ms{quantile}` | summary | p50 / p95 / p99 latency |
| `ohm_nodes_total` | gauge | Active node count (`deleted_at IS NULL`) |
| `ohm_edges_total` | gauge | Active edge count |
| `ohm_observations_total` | gauge | Active observation count |
| `ohm_instance_uptime_seconds` | gauge | Per-instance uptime (mirrors `ohm_uptime_seconds`) |
| `ohm_ducklake_sync_lag_seconds` | gauge | Seconds since last DuckLake sync (from `ohm_sync_state`) |
| `ohm_ducklake_last_sync_timestamp` | gauge | Unix timestamp of last DuckLake sync |

`/metrics` is a no-auth route (`src/ohm/server/server.py:781`), consistent with Prometheus scrape conventions. A JSON view is also available (default when no Prometheus format is requested) for ad-hoc inspection.

### 5. `ohm instances health` — re-probe all registered instances

`health` (`src/ohm/cli/__init__.py:3157`) reads the registry file and re-probes each registered instance with `GET /instance`. It updates each entry's `health` field to `ok` or `unreachable` and refreshes the metadata from the live response. This gives operators a single command to check the mesh after a restart, network change, or suspected outage.

### 6. MCP tool `ohm_list_instances`

The MCP server exposes the registry to agents via the `ohm_list_instances` tool (`src/ohm/mcp/server.py:341`). The tool reads `~/.ohm/registry.json` directly and returns the instances list. If the registry does not exist, it returns a hint to run `ohm instances discover` first. This lets an MCP-connected agent discover the mesh topology without shelling out to the CLI.

## Mapping to existing concepts

| Existing concept | Relationship |
|------------------|-------------|
| `GET /health` (`infra.py`) | `/instance` is the richer sibling — health is a boolean, instance is structured metadata |
| `GET /ready` (`infra.py:292`) | Both are no-auth infra routes; `/instance` adds discovery, `/ready` adds readiness |
| ADR-002 ohmd daemon | `/instance` and `/metrics` run on the daemon; discovery probes the daemon |
| ADR-012 per-agent local cache | Agent config dirs (`~/.ohm/agents/*/ohm.json`) are a discovery source |
| ADR-015 multi-tenancy | `/instance` surfaces `tenants` and `domain_configs` only when multi-tenant is active |
| ADR-004 DuckLake | `/instance` and `/metrics` surface DuckLake sync lag for the first time |
| `GET /metrics/semantic` (`admin.py:1799`) | Semantic-layer metrics (YAML-defined); `/metrics` is Prometheus-style infrastructure metrics — different audiences |
| `ohm standup` `discover_instances` (`cli/standup.py:87`) | Earlier single-endpoint probe; the `ohm instances` tree generalizes it to a full registry |

## Consequences

**Positive:**
- A single command (`ohm instances discover`) reveals every OHM instance reachable from the host — no more tribal knowledge of URLs
- Agents can discover the correct endpoint programmatically: SDK reads the registry JSON, MCP agents call `ohm_list_instances`
- Health monitoring works across the mesh: `ohm instances health` re-probes all registered instances in one pass
- Prometheus `/metrics` with graph counts, latency, and DuckLake sync lag plugs into standard scraping/alerting with no extra exporter
- Multi-tenant deployments are self-describing — `tenants` and `domain_configs` in `/instance` show what is live without querying the DB
- No-auth infra routes (`/instance`, `/metrics`) match the existing `/health` and `/ready` pattern — discovery and scraping need no token

**Negative:**
- Discovery is pull-based — there is no push registration. An `ohmd` that starts after the last `discover` run is invisible until the next scan. Remote instances not in any well-known config location must be added manually (e.g., by setting `OHM_URL` or editing a config the scanner reads)
- No-auth `/instance` discloses identity and topology metadata (instance ID, version, tenant list, agent count) to anyone who can reach the port. This mirrors `/health` but is richer; deployments exposing ohmd to untrusted networks should firewall or bind to localhost
- The registry JSON is a snapshot, not live — `health` reflects the moment of the last probe, not real-time status. `ohm instances health` must be re-run to refresh
- `/metrics` graph-count queries hit the DB on every scrape; very high scrape frequencies add load. The queries are simple `COUNT(*)` with `deleted_at IS NULL` filters but are not cached

**Neutral:**
- The registry is local-first (no central server, no coordinator), consistent with OHM's local-DuckDB architecture. Each host maintains its own `~/.ohm/registry.json`. There is no cross-host aggregation — a host only sees instances reachable from its own network position
- The registry file format is versioned (`"version": "1"`) to allow future schema evolution without breaking consumers

## Alternatives considered

1. **Centralized registry server with push heartbeats.** A dedicated service that every `ohmd` registers with on startup and heartbeats periodically. Rejected as too heavy for a small-team mesh — it introduces a new single point of failure, a new process to run and monitor, and a new config surface, all to solve a problem that pull-based discovery from well-known locations handles adequately. Push registration remains a future option if the mesh grows beyond what local scanning can cover.

2. **DNS-based service discovery (SRV records, mDNS).** Rejected as it requires infrastructure (DNS server with SRV records, or mDNS daemon) that is not reliably available in small-team and single-host setups. It also does not carry the rich metadata (version, tenants, DuckLake status) that `/instance` provides. DNS gives you a hostname; `/instance` gives you the full picture.

3. **No registry — manual `curl /health` per instance.** Rejected as it does not scale beyond 2–3 instances. It provides no persistent record, no SDK/MCP consumption path, no health re-check workflow, and no Prometheus integration. It is the status quo this ADR replaces.

## References

- `src/ohm/server/handlers/infra.py:211` — `_get_instance` handler
- `src/ohm/server/handlers/infra.py:312` — `_get_infra_metrics` (Prometheus endpoint)
- `src/ohm/server/server.py:781` — no-auth infra route list
- `src/ohm/server/server.py:2569` — `/instance` route registration
- `src/ohm/cli/__init__.py:728` — `ohm instances` CLI subcommand tree
- `src/ohm/cli/__init__.py:3038` — `_discover_instances` (well-known location scan)
- `src/ohm/cli/__init__.py:3110` — `_handle_instances` (list/discover/health/show dispatch)
- `src/ohm/mcp/server.py:341` — `ohm_list_instances` MCP tool registration
- `src/ohm/mcp/server.py:540` — `ohm_list_instances` handler (reads registry JSON)
- `src/ohm/cli/standup.py:87` — earlier single-endpoint `discover_instances`
- `tests/test_instance_registry.py` — endpoint, metrics, and route registration tests
- ADR-002 — ohmd daemon
- ADR-004 — Three-layer data architecture (DuckLake)
- ADR-012 — Per-agent local DuckDB cache
- ADR-015 — Multi-tenancy
