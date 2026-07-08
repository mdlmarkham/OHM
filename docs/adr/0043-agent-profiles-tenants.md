# ADR-043: Agent Profiles — Multi-Instance Access for a Single Agent

**Date:** 2026-07-08
**Status:** Accepted
**Related issues:** OHM-yzyk.3 (this work), ADR-015 (multi-tenancy), ADR-042 (instance registry), ADR-012 (per-agent local cache)

## Context

In a small-team multi-agent mesh, a single agent may need to access multiple OHM instances during a single work session:

- **Different tenants on the same `ohmd`** — ADR-015 lets one daemon serve N isolated tenants. A "platform" agent may need to read the `devops` tenant, write to the `security` tenant, and consult the `ops` tenant, all on the same `127.0.0.1:8710`.
- **Separate `ohmd` daemons** — ADR-042's registry surfaces multiple reachable instances (production daemon, staging daemon, a teammate's local daemon). An agent may need to compare graphs across daemons or push a finding to a remote instance.
- **A mix of local and remote instances** — ADR-012 gives each agent a local DuckDB file for zero-latency work. An agent might read its local cache, sync to a shared DuckLake-backed daemon, and also push a summary to a remote team daemon — three stores, one agent.

Today, selecting the right OHM store for each operation means hardcoding URLs, tokens, and tenant IDs in every call site:

```python
# What every agent does today — hardcoded per call
g_local = ohm.connect("~/.ohm/agents/metis/ohm.duckdb", actor="metis")
g_devops = ohm.connect_http("http://127.0.0.1:8710", actor="metis",
                             token="ohm-metis-...", tenant_id="devops")
g_remote = ohm.connect_http("http://10.0.0.5:8710", actor="metis",
                            token="ohm-metis-staging-...", tenant_id="ops")
```

This couples every agent to specific URLs and tokens, makes instance switching a code change, and offers no team-wide declarative configuration. ADR-042 solved the *discovery* half of this problem (which instances exist); this ADR solves the *connection* half (how an agent picks and uses the right one for each operation).

A single-profile version of this already exists in the MCP server: `src/ohm/mcp/config.py` loads one connection config (`ohm_url`, `token`, `agent_id`, `tenant_id`, `token_type`, `domain_config`, `allowed_tools`, `read_only`) from env vars or a `--config` JSON file, and `is_tool_allowed()` / `make_headers()` / `_should_send_tenant_header()` already implement the tool-filtering and `X-Tenant-ID` semantics this ADR generalizes. Agent Profiles are the multi-profile, SDK-and-CLI-facing evolution of that single-profile MCP config.

## Decision

Agent Profiles: a client-side catalog of named connection profiles that an agent (or operator) selects by name for each operation. Profiles are declarative JSON, resolved at connection time into either a local `connect` or a remote `connect_http` call, with tenant routing handled transparently.

### 1. Profile catalog files

Two catalog locations, merged at load time (project overrides user):

| Location | Scope | Version-controlled? | Purpose |
|----------|-------|---------------------|---------|
| `.ohm/profiles.json` | Project (repo-relative) | Yes (share with team) | Team-shared defaults: URLs, tenant IDs, domain configs, allowed tools |
| `~/.ohm/profiles.json` | User (home) | No (per-developer) | Personal overrides: tokens, default selection, local-only profiles |

Project-level catalogs are the sharing mechanism — a team commits the connection topology once, and every member gets the same profiles. User-level catalogs let an individual override tokens (which should never be committed) and set a personal default.

**Profile shape:**

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `ohm_url` | string | No | `http://host:port` of an `ohmd`. Absent → local DuckDB (`connect`). |
| `tenant_id` | string | No | ADR-015 tenant to route to. Absent → single-tenant / default. |
| `token` | string | No | Bearer token for HTTP auth. Overridable via `OHM_TOKEN_<PROFILE>` env var. |
| `agent_id` | string | No | `actor` to use for `created_by` attribution. Defaults to the catalog's top-level `default_agent_id` or the profile name. |
| `domain_config` | string | No | `SchemaConfig` name to activate (e.g. `topo`, `medical`). Server may override for tenant-scoped keys. |
| `allowed_tools` | list[string] | No | Restrict the SDK surface for this profile (e.g. `["graph.read", "graph.search"]`). Absent or `["*"]` → all tools. Reuses the semantics from `mcp/config.py:is_tool_allowed`. |
| `read_only` | bool | No | If `true`, block write operations through this profile. Default `false`. Reuses `mcp/config.py:WRITE_TOOLS` for the blocked set. |
| `token_type` | string | No | `agent` (sends `X-Tenant-ID` header to select tenant) or `customer` (key is already tenant-scoped server-side, header omitted). Default `agent`. Matches `mcp/config.py`. |
| `default` | bool | No | If `true`, this profile is selected when no `--profile` is given. Exactly one profile per merged catalog should be default. |

Example:

```json
{
  "default_agent_id": "metis",
  "profiles": {
    "local": {
      "agent_id": "metis",
      "read_only": false,
      "default": true
    },
    "devops": {
      "ohm_url": "http://127.0.0.1:8710",
      "tenant_id": "devops",
      "token": "${OHM_TOKEN_DEVOPS}",
      "agent_id": "metis",
      "domain_config": "default",
      "token_type": "agent"
    },
    "security": {
      "ohm_url": "http://127.0.0.1:8710",
      "tenant_id": "security",
      "token": "${OHM_TOKEN_SECURITY}",
      "agent_id": "metis-sec",
      "read_only": true,
      "token_type": "agent"
    },
    "remote-staging": {
      "ohm_url": "http://10.0.0.5:8710",
      "tenant_id": "ops",
      "token": "${OHM_TOKEN_STAGING}",
      "agent_id": "metis",
      "domain_config": "topo"
    }
  }
}
```

Tokens support `${ENV_VAR}` interpolation so the committed catalog carries no secrets — only the variable name. At load time, `${OHM_TOKEN_DEVOPS}` resolves from the environment; an unresolved variable leaves the field empty (and the SDK raises a clear error at connection time, not at catalog parse time).

### 2. Profile routing — `connect` vs `connect_http`

At connection time, the profile's `ohm_url` field selects the transport, resolving into the two existing SDK primitives (`src/ohm/framework/sdk.py`):

- **`ohm_url` present** → `connect_http(ohm_url, actor=agent_id, token=token, ...)` (`sdk.py:6092`). The SDK's HTTP client sends the request.
- **`ohm_url` absent** → `connect(db_path, actor=agent_id, ...)` (`sdk.py:5961`). The profile resolves a local DuckDB path from `~/.ohm/agents/{agent_id}/ohm.duckdb` (ADR-012) unless an explicit `db_path` is provided in the catalog.

Tenant routing (when `tenant_id` is present) depends on `token_type`, reusing the logic already implemented in `mcp/config.py`:

- **`agent`** (default): the HTTP client sends `X-Tenant-ID: <tenant_id>` on every request (`sdk.py:6161`). The server resolves the tenant from the header (ADR-015). Agent tokens can address any tenant.
- **`customer`**: the key is already scoped to a single tenant server-side (ADR-015, OHM-tss4.19). The `tenant_id` field is informational only and is not sent as a header — the server ignores it. This avoids leaking the tenant id in transit and matches how customer API keys work.

### 3. Profile selection — explicit, heuristic, or default

Three selection modes, in priority order:

1. **Explicit** — `--profile <name>` on the CLI, or `Graph.from_profile(name)` / `profile="name"` in the SDK. Always wins. Example: `ohm --profile security graph search "CVE-2026-*"`.
2. **Heuristic** (future, Phase 2) — infer the profile from context: the current repo, a marker file (`.ohm/profile`), or the path being operated on. Not implemented in Phase 1; the hook is reserved so a future `--profile auto` can be added without changing the catalog format.
3. **Default** — the profile with `"default": true` in the merged catalog. If no profile is marked default, the SDK falls back to `connect` with no arguments (current behavior — backward compatible).

### 4. CLI — `ohm profile` subcommand tree and global `--profile`

A new `ohm profile` subcommand tree manages the catalog:

| Command | Purpose |
|---------|---------|
| `ohm profile list` | Print all profiles (merged), marking the default with `*`. Shows resolved URL/tenant but masks tokens. |
| `ohm profile show <name>` | Print a single profile's full resolved configuration (tokens masked unless `--reveal`). Prints the source (project vs user catalog) of each resolved field so merges are debuggable. |
| `ohm profile use <name>` | Set the default profile by writing `"default": true` to the user-level catalog. |

A global `--profile <name>` flag is added to the top-level argument parser and threaded into every subcommand that opens a connection. `ohm --profile devops graph search "..."` is equivalent to selecting the `devops` profile for that one invocation. The flag is optional; without it, the default profile is used.

### 5. SDK — `Graph.from_profile(name)`

The canonical agent SDK (`ohm.framework.sdk.Graph`, `sdk.py:28`) gains a class method:

```python
from ohm.framework.sdk import Graph

# Explicit profile
with Graph.from_profile("devops") as g:
    results = g.search("CVE-2026-*")

# Default profile
with Graph.from_profile() as g:
    g.create_node("...", node_type="pattern")
```

`from_profile` loads and merges the catalogs, resolves `${ENV_VAR}` tokens, picks the transport (`connect` vs `connect_http`), applies `read_only` and `allowed_tools` restrictions, and returns a context manager. The returned object is the same `Graph` class — profiles are a connection factory, not a separate API surface. This keeps agent code identical regardless of transport.

`read_only=True` profiles cause write methods (`create_node`, `create_edge`, `observe`, etc.) to raise `PermissionDeniedError` (exit code 4) at call time, reusing the `WRITE_TOOLS` set from `mcp/config.py`. `allowed_tools` restricts which SDK methods are callable; an attempt to call a disallowed method raises `PermissionDeniedError` with the allowed list in the message.

### 6. Composition with multi-tenancy

Profiles are the **client-side complement** to OHM's server-side multi-tenancy (ADR-015), not a replacement:

- **ADR-015 (server-side)** decides how one `ohmd` isolates N tenants — separate DuckDB files, per-tenant SchemaConfig, LRU cache. The daemon owns isolation.
- **This ADR (client-side)** decides how one agent picks which tenant (or which daemon, or which local file) to talk to for a given operation. The agent owns selection.

The two compose cleanly:

- A profile with `ohm_url` + `tenant_id` + `token_type: agent` routes to a specific tenant on a multi-tenant daemon.
- A profile with `ohm_url` + `token_type: customer` routes to a single-tenant-scoped key on the same daemon.
- A profile with no `ohm_url` routes to a local DuckDB file (ADR-012) — no daemon, no tenant, no HTTP.
- A profile pointing at a remote single-tenant `ohmd` (no ADR-015) just omits `tenant_id`.

Nothing in the profile catalog changes server behavior. A profile is a description of how to reach a store; the store's own rules (boundary enforcement ADR-003, read scopes ADR-037, tenant isolation ADR-015) still apply.

## Mapping to existing concepts

| Existing concept | Relationship |
|------------------|-------------|
| `mcp/config.py` single-profile config | Profiles are the multi-profile generalization: same fields (`ohm_url`, `token`, `agent_id`, `tenant_id`, `token_type`, `domain_config`, `allowed_tools`, `read_only`), same `is_tool_allowed` / `WRITE_TOOLS` / `_should_send_tenant_header` semantics, promoted from one active config to a named catalog |
| ADR-015 multi-tenancy | Profiles are the client-side routing layer over server-side tenant isolation |
| ADR-012 per-agent local cache | A profile with no `ohm_url` is the local-cache connection — `~/.ohm/agents/{agent_id}/ohm.duckdb` |
| ADR-042 instance registry | The registry discovers *which* instances exist; profiles describe *how to connect* to them. `ohm profile add-from-registry <instance_id>` is a future convenience helper. |
| `connect` / `connect_http` (`framework/sdk.py:5961` / `6092`) | Profiles are a connection factory over these two primitives — no new transport |
| `OHM_URL` / `OHM_TOKEN` env vars | Still honored as the final fallback when no profile is selected and no catalog exists. Profiles add `${ENV_VAR}` interpolation so committed catalogs carry no secrets. |
| `X-Tenant-ID` header (ADR-015, `sdk.py:6161`) | Profiles automate sending this header based on `tenant_id` + `token_type` |
| `SchemaConfig` (ADR-006/007) | `domain_config` field activates a per-profile schema without code changes |
| ADR-003 / ADR-037 boundaries & read scopes | Server-side enforcement still applies to every profile-selected connection; `read_only` / `allowed_tools` are client-side guardrails on top |

## Consequences

**Positive:**
- A single agent can seamlessly work across dev/sec/ops tenants and across local/remote instances without code changes — switching is a `--profile` flag or a `from_profile(name)` call
- The profile catalog is declarative and version-controllable (`.ohm/profiles.json`), so a team shares one connection topology instead of tribal knowledge
- Tokens are interpolated from environment variables (`${OHM_TOKEN_DEVOPS}`), so the committed catalog carries no secrets — the same pattern as `.env` files
- No server-side changes required — profiles are pure client-side configuration resolved into existing `connect` / `connect_http` calls
- `read_only` and `allowed_tools` reuse the already-implemented `mcp/config.py` semantics, giving operators a client-side guardrail on top of server-side boundary enforcement (ADR-003/037) — useful for low-trust or junior-agent profiles

**Negative:**
- The token still lives *somewhere* — in the env var that the catalog interpolates, or (worst case) inline in a user-level catalog. The catalog format does not itself encrypt. Mitigations: `${ENV_VAR}` interpolation is the documented pattern; `ohm profile show --reveal` is the only command that prints tokens; user-level catalogs (`~/.ohm/profiles.json`) are not committed
- The profile catalog must be kept in sync across team members. A teammate who has not pulled the latest `.ohm/profiles.json` will be missing profiles. This is the same tradeoff as any shared config file (`.editorconfig`, `pyproject.toml`) and is acceptable
- Two catalog locations (project + user) introduce a merge order and a potential for confusion ("why is my `devops` profile pointing at staging?"). `ohm profile show <name>` prints the source of each resolved field to make merges debuggable

**Neutral:**
- Profiles do not replace server-side multi-tenancy (ADR-015); they compose with it. A profile routes to a tenant; the daemon still enforces isolation
- Profiles do not replace ADR-042's instance registry; they consume it. The registry answers "what exists?"; profiles answer "how do I connect to it?"
- The catalog format is intentionally transport-agnostic (local file vs HTTP vs future transports). Adding a new transport means adding a new discriminator field, not a new catalog format

## Alternatives considered

1. **Single config file with one active connection.** A flat config (one `ohm_url`, one `token`, one `tenant_id`) representing the currently active instance — effectively what `mcp/config.py` is today. Rejected as too rigid for multi-instance workflows: an agent working across three tenants would have to rewrite the config file three times per session, or hold three connections with no shared configuration. Profiles let all connections be described simultaneously and selected per-operation. (The single-profile MCP config remains the right shape for the MCP server, which only ever represents one connection; profiles are the SDK/CLI-facing generalization.)

2. **Environment-variable-only configuration.** `OHM_URL`, `OHM_TOKEN`, `OHM_TENANT_ID` — extend the existing single-instance env vars to cover multiple instances via suffixed names (`OHM_URL_DEVOPS`, `OHM_TOKEN_DEVOPS`, ...). Rejected as hard to manage: the number of env vars grows as `3 × N_profiles`, there is no declarative list/discover command, no `default` flag, no `allowed_tools` or `read_only` restrictions, and no way to commit a team-wide topology without a wrapper script. Environment variables remain the secret-injection mechanism (via `${ENV_VAR}` interpolation); profiles are the structure on top.

3. **Server-side profile routing (ohmd selects tenant based on token).** Let the daemon inspect the token and route to the correct tenant automatically, so the client only ever sends one token. Rejected because it does not handle the multi-daemon scenario — an agent talking to two separate `ohmd` processes (production and staging) still needs two endpoints and two tokens, and server-side routing cannot pick between daemons. It also reverses ADR-015's design, where the *client* (agent token) selects the tenant via `X-Tenant-ID` and the server is tenant-agnostic until the header arrives. Server-side routing is a useful optimization for customer-key tenants (where the key already encodes the tenant — already implemented via OHM-tss4.19), but it does not solve the multi-instance problem this ADR addresses.

## References

- `src/ohm/framework/sdk.py:28` — `Graph` class (canonical agent SDK)
- `src/ohm/framework/sdk.py:5961` — `connect()` (local DuckDB transport)
- `src/ohm/framework/sdk.py:6092` — `connect_http()` (HTTP transport to ohmd)
- `src/ohm/framework/sdk.py:6161` — `X-Tenant-ID` header injection in `HttpGraph`
- `src/ohm/mcp/config.py` — single-profile config this ADR generalizes (`is_tool_allowed`, `WRITE_TOOLS`, `_should_send_tenant_header`, `make_headers`)
- `src/ohm/cli/__init__.py` — `ohm profile` subcommand tree and global `--profile` flag (to be implemented)
- `.ohm/profiles.json` — project-level catalog (team-shared)
- `~/.ohm/profiles.json` — user-level catalog (personal overrides)
- ADR-015 — Multi-tenancy (server-side tenant isolation)
- ADR-012 — Per-agent local DuckDB cache (local profile transport)
- ADR-042 — Instance registry (discovery; profiles consume the registry)
- ADR-003 — Agent-owned edges with challenge semantics (boundary enforcement still applies)
- ADR-037 — Per-agent read scopes and temporal pinning (server-side read restrictions still apply)
- ADR-006/007 — Advisory schema and SchemaConfig (`domain_config` field)
