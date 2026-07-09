# Deploying OHM for Local Agents: System Daemon + Per-Tenant MCP

This guide covers the recommended deployment topology for small teams using local AI agents such as GitHub Copilot, Cursor, Claude Code, or OpenCode.

The guide supports two shapes:

1. **Multiple sidecars** — one `ohm-mcp` process per tenant, each appearing as a separate MCP server in the agent.
2. **Single sidecar with profiles** — one `ohm-mcp` process that carries multiple named tenant profiles and switches between them at runtime via `ohm_select_profile`.

Both shapes sit on top of the same system-level `ohmd --multi-tenant` daemon. The sidecar shape is easier to reason about; the profile shape reduces the number of child processes the agent spawns.

---

## Table of contents

1. [Topology](#topology)
2. [Greenfield deployment](#greenfield-deployment)
3. [Connecting to an existing tenant](#connecting-to-an-existing-tenant)
4. [Configuring local agents](#configuring-local-agents)
5. [Agent profiles: one sidecar, many tenants](#agent-profiles-one-sidecar-many-tenants)
6. [Instance registry and monitoring](#instance-registry-and-monitoring)
7. [Security notes](#security-notes)
8. [Troubleshooting](#troubleshooting)
9. [Reference configs](#reference-configs)
10. [Next steps](#next-steps)

---

## Topology

### Shape A: one sidecar per tenant

```text
┌─────────────────────────────────────┐
│        system-level ohmd            │
│  (started with --multi-tenant)      │
│                                     │
│  ┌──────────────┐ ┌──────────────┐ │
│  │ tenant: devops│ │tenant: dataops│ │
│  │ devsecops.json│ │datapipelines.json│ │
│  └──────┬───────┘ └──────┬───────┘ │
└─────────┼────────────────┼───────────┘
          │                │
    ohm-mcp-devops   ohm-mcp-dataops
    (local sidecar)  (local sidecar)
          │                │
    ┌─────┴────┐     ┌─────┴────┐
    │ Copilot  │     │ Copilot  │
    │"OHM DevOps"│   │"OHM DataOps"│
    └──────────┘     └──────────┘
```

### Shape B: one sidecar with profiles

```text
┌─────────────────────────────────────┐
│        system-level ohmd            │
│  (started with --multi-tenant)      │
│                                     │
│  ┌──────────────┐ ┌──────────────┐ │
│  │ tenant: devops│ │tenant: dataops│ │
│  │ devsecops.json│ │datapipelines.json│ │
│  └──────┬───────┘ └──────┬───────┘ │
└─────────┼────────────────┼───────────┘
          │                │
         ┌┴────────────────┴┐
         │   ohm-mcp         │
         │  profiles=[devops │
         │           dataops]│
         └────────┬──────────┘
                  │
              ┌───┴───┐
              │ Copilot │
              │ "OHM"   │
              └─────────┘
```

Why this topology?

- **Centralized memory** across all projects and agents.
- **Tenant isolation** keeps DevSecOps and functional/data-pipeline data separate.
- **Natural agent UX**: each tenant can appear as its own MCP toolset, or as one toolset with profile switching.
- **No per-project setup**: agents connect to the system daemon regardless of which repo is open.

---

## Greenfield deployment

### 1. Install OHM

```bash
pip install ohm
```

Or use the container image:

```bash
docker run -p 127.0.0.1:8710:8710 -v /var/lib/ohm:/var/lib/ohm ghcr.io/mdlmarkham/ohm:latest --multi-tenant
```

### 2. Run `ohm standup` (recommended)

The fastest way to stand up a multi-tenant daemon with MCP sidecars is the `ohm standup` CLI (ADR-022):

```bash
sudo ohm standup --mode greenfield \
  --multi-tenant \
  --template devsecops \
  --tenant devops \
  --agent-id copilot-vscode \
  --service-mode systemd
```

This will:

1. Write a default `/etc/ohm/ohmd.json` with an admin agent.
2. Install and start the `ohmd` systemd service.
3. Provision the `devops` tenant with the `devsecops` domain template.
4. Generate a customer API key for the tenant.
5. Emit `/etc/ohm/mcp-devops.json` (or a single profile config if you pass `--single-profile`).
6. Install and start the sidecar service.
7. Verify the tenant schema, minimum viable graph, and MCP `tools/list`.

Repeat for additional tenants:

```bash
sudo ohm standup --mode greenfield \
  --multi-tenant \
  --template datapipelines \
  --tenant dataops \
  --agent-id copilot-vscode \
  --service-mode systemd
```

For a quick foreground test on a non-conflicting port:

```bash
OHM_CONFIG=/tmp/ohm-test/ohmd.json \
OHM_DB_PATH=/tmp/ohm-test/ohm.duckdb \
ohm standup --mode greenfield \
  --url http://127.0.0.1:18710 \
  --service-mode foreground \
  --template devsecops \
  --tenant devops \
  --agent-id copilot-vscode
```

### 3. Manual daemon setup (alternative)

If you prefer to set up the daemon by hand, create `/etc/systemd/system/ohmd.service`:

```ini
[Unit]
Description=OHM daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/ohmd --multi-tenant --config /etc/ohm/ohmd.json
Restart=always
User=ohm
Group=ohm

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ohmd
```

### 4. Provision tenants

Use an admin agent token to create tenants:

```bash
curl -X POST http://127.0.0.1:8710/tenant/provision \
  -H "Authorization: Bearer ${OHM_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"devops","domain":"devsecops","tier":"professional"}'

curl -X POST http://127.0.0.1:8710/tenant/provision \
  -H "Authorization: Bearer ${OHM_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"dataops","domain":"datapipelines","tier":"professional"}'
```

Save the returned customer API keys securely.

### 5. Create MCP configs

See [Reference configs](#reference-configs) for the two shapes.

### 6. Test the sidecars

```bash
# Shape A: separate sidecars
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ohm-mcp --config /etc/ohm/mcp-devops.json
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ohm-mcp --config /etc/ohm/mcp-dataops.json

# Shape B: one sidecar with profiles
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ohm-mcp --config /etc/ohm/mcp-ohm.json
```

---

## Connecting to an existing tenant

If the tenants already exist, you only need a customer API key and an MCP config.

### 1. Discover tenants

```bash
curl -H "Authorization: Bearer ${OHM_ADMIN_TOKEN}" \
  http://127.0.0.1:8710/tenants
```

### 2. Get or rotate a customer key

If the original key was lost, the simplest recovery is to re-provision or use an admin token with `X-Tenant-ID` while you rotate credentials. There is no current endpoint to reveal an existing customer key by design.

### 3. Write an MCP config

Follow the same config format as in the greenfield section, using the existing tenant's domain and customer token.

### 4. Verify

```bash
curl -H "Authorization: Bearer ohm-cu-dataops-..." \
  -H "X-Tenant-ID: dataops" \
  http://127.0.0.1:8710/tenant/dataops/schema
```

Note: customer tokens are already tenant-scoped. The `X-Tenant-ID` header is redundant here but harmless. It is required only when using an admin-role agent token.

---

## Configuring local agents

### GitHub Copilot (VS Code)

#### Shape A: one server per tenant

Add to your VS Code settings or `.vscode/mcp.json`:

```json
{
  "mcpServers": {
    "ohm-devops": {
      "command": "ohm-mcp",
      "args": ["--config", "/etc/ohm/mcp-devops.json"]
    },
    "ohm-dataops": {
      "command": "ohm-mcp",
      "args": ["--config", "/etc/ohm/mcp-dataops.json"]
    }
  }
}
```

Copilot will expose tools like `ohm-devops/ohm_search` and `ohm-dataops/ohm_search` depending on transport and MCP naming.

#### Shape B: single server with profiles

```json
{
  "mcpServers": {
    "ohm": {
      "command": "ohm-mcp",
      "args": ["--config", "/etc/ohm/mcp-ohm.json"]
    }
  }
}
```

The agent can call `ohm_list_profiles` to discover tenants and `ohm_select_profile` to switch between them. This is useful when the agent wants to operate in one tenant at a time without spawning multiple stdio servers.

### Cursor / Claude Code / OpenCode

Most MCP-compatible clients accept a similar JSON config. Use the same `ohm-mcp` command and either a tenant-specific config file for each server or a single profile config.

---

## Agent profiles: one sidecar, many tenants

Agent profiles (ADR-043 / `OHM-yzyk.3`) let a single `ohm-mcp` sidecar carry multiple tenant profiles. Each profile contains its own backend URL, token, tenant, agent ID, `allowed_tools`, and `read_only` flag.

### When to use profiles

- You want one MCP server entry in the agent, but access to multiple tenants.
- The agent's runtime can switch tenants explicitly rather than by name prefix.
- You want to reduce the number of long-lived stdio child processes.

### When to use separate sidecars instead

- You want different tools to appear simultaneously in the agent's tool list.
- You want separate log/temp paths or process-level isolation per tenant.
- You are not confident the agent will call `ohm_select_profile` correctly.

### Available profile tools

The local sidecar exposes two extra tools when profiles are configured:

- `ohm_list_profiles` — returns `{"profiles": [...], "active": "..."}`.
- `ohm_select_profile(name)` — switches the active profile for the next tool call.

These tools are **local-only** and are not exposed by the FastMCP gateway, because the gateway resolves profiles from the HTTP request rather than from sidecar state.

### Example conversation

```text
Agent: list my OHM profiles
Tool:  ohm_list_profiles → {"profiles": ["devops", "dataops"], "active": "devops"}

Agent: switch to the dataops profile
Tool:  ohm_select_profile {"name": "dataops"}

Agent: search dataops for pipeline failures
Tool:  ohm_search {"query": "pipeline failure"}
```

### Backwards compatibility

A legacy flat config without a `profiles` array is treated as an implicit `default` profile. You do not have to rewrite existing `mcp-*.json` files unless you want multi-tenant support.

---

## Instance registry and monitoring

When you run more than one `ohm-mcp` sidecar, it becomes useful to know which sidecars are alive, which tenant they serve, and whether they can reach the daemon. The OHM instance registry (ADR-042 / `OHM-yzyk.5`) tracks this.

### What the registry stores

Each `ohm-mcp` sidecar can register itself with the daemon on startup:

```json
{
  "instance_id": "copilot-vscode-devops-a1b2",
  "agent_id": "copilot-vscode",
  "tenant_id": "devops",
  "transport": "stdio",
  "profile": "devops",
  "last_seen": "2026-07-08T19:00:00Z"
}
```

The registry is exposed at `GET /registry/instances` (admin token required).

### Monitoring checklist

- Daemon health: `GET /health`.
- Tenant health: `GET /tenant/{tenant_id}/health`.
- Registry: `GET /registry/instances`.
- Sidecar logs: wherever `ohm-mcp` writes its log file (set in the MCP config).

### Cleanup

Stale instances are pruned automatically after a configured timeout, or you can force a heartbeat with `POST /registry/heartbeat`.

For full details see `docs/adr/0043-agent-profiles-tenants.md` and `docs/adr/0042-instance-registry-monitoring.md`.

---

## Security notes

- **Prefer customer API keys for agents.** A customer key is scoped to exactly one tenant. If a Copilot session is compromised, the blast radius is one tenant.
- **Admin-role agent tokens can access any tenant via `X-Tenant-ID`.** Only use these for provisioning and automation, not for everyday agent tools.
- **Non-admin agent tokens cannot use `X-Tenant-ID`.** The server silently ignores the header for non-admin agents and routes them to the core store. This was fixed in `OHM-tss4.19` to prevent cross-tenant data leaks.
- **Keep tokens out of repos.** Use environment variables, 1Password, or your OS keychain. The MCP config files should be readable only by the user:

```bash
chmod 600 /etc/ohm/mcp-*.json
```

- **Scope `allowed_tools`** per profile or per tenant config. A read-only observability tenant does not need `ohm_create_node`.
- **Use `read_only: true`** for any profile that should only read or listen. Write tools will be rejected before any HTTP request leaves the sidecar.

---

## Troubleshooting

### MCP server fails to start

- Check that `ohm-mcp` is installed and in `PATH`.
- Verify the token is valid: `curl -H "Authorization: Bearer <token>" http://127.0.0.1:8710/health`.
- Verify the tenant exists: `GET /tenants` with an admin token.
- Check that `ohmd` was started with `--multi-tenant`.

### Results come from the wrong tenant

- **Shape A (separate sidecars):** ensure each sidecar config uses the correct customer token.
- **Shape B (profiles):** check `ohm_list_profiles` and call `ohm_select_profile` to switch.
- If using an agent token: ensure the agent has `admin` role and `tenant_id` is in the MCP config.
- If using a customer token: the token itself is tenant-scoped. Remove `tenant_id` from the config or treat it as documentation only.

### Domain schema mismatch

- Check `GET /tenant/{tenant_id}/schema` and compare with the `domain_config` in the MCP config.
- The MCP server should fetch the live schema on startup, so mismatches are usually a stale cached prompt.

### Two sidecars conflict

- Ensure each config uses distinct log/temp paths if `ohm-mcp` writes any local files. With stdio transport and tenant-scoped tokens, sidecars are naturally stateless and should not conflict.

### `ohm_select_profile` is missing

- `ohm_list_profiles` and `ohm_select_profile` are only available when the config contains more than one profile, or explicitly in the local sidecar. They are intentionally skipped by the FastMCP gateway.

---

## Reference configs

### Per-tenant sidecar configs

`/etc/ohm/mcp-devops.json`:

```json
{
  "transport": "stdio",
  "ohm_url": "http://127.0.0.1:8710",
  "tenant_id": "devops",
  "token": "ohm-cu-devops-...",
  "agent_id": "copilot-vscode",
  "domain_config": "devsecops.json",
  "allowed_tools": [
    "ohm_search",
    "ohm_get_node",
    "ohm_neighborhood",
    "ohm_observe",
    "ohm_create_node"
  ],
  "read_only": false
}
```

`/etc/ohm/mcp-dataops.json`:

```json
{
  "transport": "stdio",
  "ohm_url": "http://127.0.0.1:8710",
  "tenant_id": "dataops",
  "token": "ohm-cu-dataops-...",
  "agent_id": "copilot-vscode",
  "domain_config": "datapipelines.json",
  "allowed_tools": ["*"],
  "read_only": false
}
```

### Single sidecar with profiles

`/etc/ohm/mcp-ohm.json`:

```json
{
  "transport": "stdio",
  "agent_id": "copilot-vscode",
  "active_profile": "devops",
  "profiles": [
    {
      "name": "devops",
      "ohm_url": "http://127.0.0.1:8710",
      "tenant_id": "devops",
      "token": "ohm-cu-devops-...",
      "domain_config": "devsecops.json",
      "allowed_tools": ["*"],
      "read_only": false
    },
    {
      "name": "dataops",
      "ohm_url": "http://127.0.0.1:8710",
      "tenant_id": "dataops",
      "token": "ohm-cu-dataops-...",
      "domain_config": "datapipelines.json",
      "allowed_tools": [
        "ohm_stats",
        "ohm_search",
        "ohm_get_node",
        "ohm_neighborhood",
        "ohm_listen"
      ],
      "read_only": true
    }
  ]
}
```

### VS Code: one server per tenant

```json
{
  "mcpServers": {
    "ohm-devops": {
      "command": "ohm-mcp",
      "args": ["--config", "/etc/ohm/mcp-devops.json"]
    },
    "ohm-dataops": {
      "command": "ohm-mcp",
      "args": ["--config", "/etc/ohm/mcp-dataops.json"]
    }
  }
}
```

### VS Code: single server with profiles

```json
{
  "mcpServers": {
    "ohm": {
      "command": "ohm-mcp",
      "args": ["--config", "/etc/ohm/mcp-ohm.json"]
    }
  }
}
```

---

## Next steps

After deployment:

1. Set up ingestion (`OHM-x51a`) to feed repo artifacts into the right tenant.
2. Define skills/runbooks (`OHM-461f`) so agents know when and how to query OHM.
3. Set up the instance registry (`OHM-yzyk.5`) so you can monitor sidecar health.
4. For off-machine or SaaS agents, deploy the hosted FastMCP gateway (`OHM-yzyk.2` / ADR-028):

   ```bash
   ohm-gateway --config /etc/ohm/gateway.json
   ```

For other deployment scenarios:
- **Simpler**: [Single-Project Deployment](single-project-ohm.md) — one agent, no daemon.
- **Personal/embedded**: [Single-Agent Local DuckDB](per-agent-ohm.md) — no HTTP, direct SDK.
- **Remote**: [Remote Daemon Deployment](remote-copilot-ohm.md) — HTTPS/TLS shared daemon.
- **Design rationale**: [ADR-028 — hosted MCP gateway](../adr/ADR-028-hosted-mcp-gateway.md) and [ADR-043 — agent profiles and tenants](../adr/0043-agent-profiles-tenants.md).
