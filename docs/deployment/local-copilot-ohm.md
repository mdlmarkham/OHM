# Deploying OHM for Local Agents: System Daemon + Per-Tenant MCP

This guide covers the recommended deployment topology for small teams using local AI agents such as GitHub Copilot, Cursor, Claude Code, or OpenCode:

- One `ohmd` daemon running at the system level.
- Multiple isolated tenants inside that daemon (e.g., `devops`, `dataops`).
- One `ohm-mcp` sidecar process per tenant.
- Each agent discovers the sidecars as separate MCP servers.

---

## Table of contents

1. [Topology](#topology)
2. [Greenfield deployment](#greenfield-deployment)
3. [Connecting to an existing tenant](#connecting-to-an-existing-tenant)
4. [Configuring local agents](#configuring-local-agents)
5. [Security notes](#security-notes)
6. [Troubleshooting](#troubleshooting)
7. [Reference configs](#reference-configs)

---

## Topology

```text
┌─────────────────────────────────────┐
│        system-level ohmd            │
│  (started with --multi-tenant)      │
│                                     │
│  ┌──────────────┐ ┌──────────────┐│
│  │ tenant: devops│ │ tenant: dataops│
│  │ devsecops.json│ │datapipelines.json│
│  └──────┬───────┘ └──────┬───────┘│
└─────────┼────────────────┼─────────┘
          │                │
    ohm-mcp-devops   ohm-mcp-dataops
    (local sidecar)  (local sidecar)
          │                │
    ┌─────┴────┐     ┌─────┴────┐
    │ Copilot  │     │ Copilot  │
    │ "OHM DevOps"   │ "OHM DataOps"  │
    └──────────┘     └──────────┘
```

Why this topology?

- **Centralized memory** across all projects and agents.
- **Tenant isolation** keeps DevSecOps and functional/data-pipeline data separate.
- **Natural agent UX**: each tenant appears as its own MCP toolset.
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

### 2. Start the system daemon

Create `/etc/systemd/system/ohmd.service`:

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

### 3. Provision tenants

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

### 4. Create per-tenant MCP configs

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

### 5. Test the sidecars

```bash
# These commands should return the tenant schemas
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ohm-mcp --config /etc/ohm/mcp-devops.json
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ohm-mcp --config /etc/ohm/mcp-dataops.json
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

Follow the same `mcp-{tenant}.json` format as in the greenfield section, using the existing tenant's domain and customer token.

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

### Cursor / Claude Code / OpenCode

Most MCP-compatible clients accept a similar JSON config. Use the same `ohm-mcp` command and a tenant-specific config file for each server.

---

## Security notes

- **Prefer customer API keys for agents.** A customer key is scoped to exactly one tenant. If a Copilot session is compromised, the blast radius is one tenant.
- **Admin-role agent tokens can access any tenant via `X-Tenant-ID`.** Only use these for provisioning and automation, not for everyday agent tools.
- **Non-admin agent tokens cannot use `X-Tenant-ID`.** The server silently ignores the header for non-admin agents and routes them to the core store. This was fixed in `OHM-tss4.19` to prevent cross-tenant data leaks.
- **Keep tokens out of repos.** Use environment variables, 1Password, or your OS keychain. The MCP config files should be readable only by the user:

```bash
chmod 600 /etc/ohm/mcp-*.json
```

- **Scope `allowed_tools`** per tenant. A read-only observability tenant does not need `ohm_create_node`.

---

## Troubleshooting

### MCP server fails to start

- Check that `ohm-mcp` is installed and in `PATH`.
- Verify the token is valid: `curl -H "Authorization: Bearer <token>" http://127.0.0.1:8710/health`.
- Verify the tenant exists: `GET /tenants` with an admin token.
- Check that `ohmd` was started with `--multi-tenant`.

### Results come from the wrong tenant

- If using an agent token: ensure the agent has `admin` role and `tenant_id` is in the MCP config.
- If using a customer token: the token itself is tenant-scoped. Remove `tenant_id` from the config or treat it as documentation only.

### Domain schema mismatch

- Check `GET /tenant/{tenant_id}/schema` and compare with the `domain_config` in the MCP config.
- The MCP server should fetch the live schema on startup, so mismatches are usually a stale cached prompt.

### Two sidecars conflict

- Ensure each config uses distinct log/temp paths if `ohm-mcp` writes any local files. With stdio transport and tenant-scoped tokens, sidecars are naturally stateless and should not conflict.

---

## Reference configs

See the full examples above for:

- `ohmd.service` systemd unit
- `mcp-devops.json`
- `mcp-dataops.json`
- Copilot `mcpServers` block

---

## Next steps

After deployment:

1. Set up ingestion (`OHM-x51a`) to feed repo artifacts into the right tenant.
2. Define skills/runbooks (`OHM-461f`) so agents know when and how to query OHM.
3. Configure Agent Profiles (`OHM-yzyk.3`) for agents that use both SDK and MCP.

For hosted or remote agents, see `OHM-yzyk.2` (hosted MCP gateway).
