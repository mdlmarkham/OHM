# Remote Daemon Deployment: Hosted OHM for Distributed Agents

When agents run on different machines, in CI/CD pipelines, or in the cloud, they need to reach OHM over HTTPS. This guide covers running `ohmd` behind a reverse proxy so that remote agents, IDE integrations, and automation can all connect securely.

This is the right choice when:
- Agents run on multiple machines (laptop + server + CI).
- Copilot or Cursor connects to a shared OHM instance over the network.
- You need TLS termination, rate limiting, and access control.
- CI/CD pipelines write observations from automated workflows.

---

## Table of contents

1. [Topology](#topology)
2. [Server setup](#server-setup)
3. [Authentication and authorization](#authentication-and-authorization)
4. [MCP over HTTPS](#mcp-over-https)
5. [SDK access from remote agents](#sdk-access-from-remote-agents)
6. [CI/CD integration](#cicd-integration)
7. [Security hardening](#security-hardening)
8. [Troubleshooting](#troubleshooting)

---

## Topology

```text
┌──────────────────────────────┐
│   ohmd (127.0.0.1:8710)      │
│   behind Caddy/nginx (TLS)   │
│   ohm.example.com:443        │
└──────────────┬───────────────┘
               │ HTTPS
        ┌──────┼──────┐
        │      │      │
    ┌───┘  ┌──┘   ┌──┘
    │      │      │
  Agent 1  Agent 2  CI/CD
  (SDK)    (MCP)   (CLI)
  Laptop   Desktop  GitHub Actions
```

Why this topology?

- **Shared memory**: all agents and pipelines write to the same knowledge graph.
- **Multi-tenant isolation**: each context (devops, dataops, research) gets its own tenant.
- **TLS and auth**: the reverse proxy handles encryption; ohmd handles tokens and scoping.
- **Flexible access**: SDK, CLI, and MCP all reach the same data.

---

## Server setup

### 1. Install OHM

```bash
pip install ohm
```

Or use the container image:

```bash
docker run -d --name ohmd \
  -p 127.0.0.1:8710:8710 \
  -v /var/lib/ohm:/var/lib/ohm \
  ghcr.io/mdlmarkham/ohm:latest --multi-tenant
```

### 2. Configure ohmd

`/etc/ohm/ohmd.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8710,
  "db_path": "/var/lib/ohm/ohm.duckdb",
  "multi_tenant": true
}
```

### 3. Start ohmd with systemd

`/etc/systemd/system/ohmd.service`:

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
WorkingDirectory=/var/lib/ohm

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/ohm
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /bin/false ohm
sudo mkdir -p /var/lib/ohm
sudo chown ohm:ohm /var/lib/ohm
sudo systemctl daemon-reload
sudo systemctl enable --now ohmd
```

### 4. Add TLS with Caddy

`/etc/caddy/Caddyfile`:

```caddyfile
ohm.example.com {
    reverse_proxy 127.0.0.1:8710

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        -Server
    }
}
```

```bash
sudo systemctl reload caddy
```

Caddy auto-provisions Let's Encrypt certificates.

### 5. Verify

```bash
curl -sf https://ohm.example.com/health
```

---

## Authentication and authorization

### Agent tokens

Generate one token per agent:

```bash
ohmd --init-token metis
ohmd --init-token clio
```

Tokens are written to `~/.ohm/ohmd.json`. Distribute them to agents over a secure channel (1Password, Vault, environment variables).

### Roles

```json
{
  "roles": {
    "metis": "read-write",
    "clio": "read-write",
    "socrates": "read-only"
  }
}
```

Read-only agents can query but not write. Use this for observers, dashboards, and audit tools.

### Customer API keys (multi-tenant)

For multi-tenant deployments, create a customer key per tenant:

```bash
curl -X POST https://ohm.example.com/admin/tenant/devops/key \
  -H "Authorization: Bearer ${OHM_ADMIN_TOKEN}"
```

Customer keys are scoped to one tenant and do not require `X-Tenant-ID`.

### Token distribution

| Method | Best for |
|--------|----------|
| Environment variable | CI/CD, containers |
| 1Password CLI | Developer laptops |
| Vault / AWS Secrets | Server-side agents |
| `~/.ohm/ohmd.json` (local only) | Single-machine daemon |

---

## MCP over HTTPS

Remote agents (Copilot, Cursor, Claude Code) connect to OHM via MCP over HTTPS.

### SSE transport (recommended for remote)

The MCP sidecar connects to `ohmd` using SSE (Server-Sent Events) over HTTPS.

MCP config for a remote agent:

```json
{
  "ohm_url": "https://ohm.example.com",
  "token": "ohm-cu…",
  "agent_id": "copilot-vscode",
  "tenant_id": "devops",
  "domain_config": "devsecops.json",
  "transport": "sse",
  "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_observe", "ohm_create_node"],
  "read_only": false
}
```

### VS Code / Copilot configuration

`.vscode/mcp.json` or VS Code settings:

```json
{
  "mcpServers": {
    "OHM DevOps": {
      "command": "ohm-mcp",
      "args": ["--config", "/path/to/mcp-devops.json"]
    }
  }
}
```

For remote deployments, the `ohm_url` in the config points to `https://ohm.example.com` instead of `http://127.0.0.1:8710`.

### Cursor / Claude Code / OpenCode

Same MCP config pattern. Place the config in the appropriate location:

- Cursor: `~/.cursor/mcp.json`
- Claude Code: `~/.claude/mcp.json`
- OpenCode: project-level `.opencode/mcp.json`

---

## SDK access from remote agents

Python agents on remote machines use the HTTP SDK:

```python
from ohm.sdk import connect_http

g = connect_http(
    url="https://ohm.example.com",
    actor="metis",
    token=os.environ["OHM_TOKEN"]
)

# Full graph API
g.create_node(id="concept-x", label="X", type="concept")
node = g.get_node("concept-x")
neighbors = g.neighborhood("concept-x", depth=2)

# Multi-tenant: use customer key, not admin key
g = connect_http(
    url="https://ohm.example.com",
    actor="copilot-devops",
    token=os.environ["OHM_DEVOPS_TOKEN"]
)
# Token is scoped to devops tenant — no tenant_id needed
```

### Local cache for remote agents

Remote agents can also use a local DuckDB cache with DuckLake sync for zero-latency reads:

```python
from ohm.store import OhmStore

# Local reads/writes, sync to remote DuckLake
store = OhmStore.for_agent(
    agent_name="metis",
    ducklake_path="s3://ohm-lake.ducklake"
)

# Read locally (microsecond latency)
node = store.get_node("concept-x")

# Sync periodically
store.sync_heartbeat()
```

This combines the remote daemon's multi-tenant access with local-read performance.

---

## CI/CD integration

GitHub Actions, GitLab CI, and other pipelines can write observations to OHM using the CLI or SDK.

### GitHub Actions example

```yaml
- name: Record deployment observation
  env:
    OHM_TOKEN: ${{ secrets.OHM_TOKEN }}
  run: |
    pip install ohm
    ohm observe concept-deploy-prod \
      --type measurement \
      --value 1.0 \
      --source github-actions \
      --url https://ohm.example.com \
      --token $OHM_TOKEN
```

### SDK example

```python
import os
from ohm.sdk import connect_http

g = connect_http(
    url=os.environ["OHM_URL"],
    actor="github-actions",
    token=os.environ["OHM_TOKEN"]
)

# Record a deployment outcome
g.observe(
    node_id="concept-deploy-prod",
    obs_type="measurement",
    value=1.0,
    source="github-actions",
    source_url=f"https://github.com/org/repo/actions/runs/{os.environ['GITHUB_RUN_ID']}"
)
```

### Token scoping for CI/CD

Create a read-write token scoped to the relevant tenant:

```bash
curl -X POST https://ohm.example.com/admin/tenant/devops/key \
  -H "Authorization: Bearer ${OHM_ADMIN_TOKEN}"
```

Store it as a repository secret (`OHM_TOKEN`) in GitHub/GitLab.

---

## Security hardening

### Network

- [ ] ohmd binds to `127.0.0.1` only (never `0.0.0.0`)
- [ ] TLS terminates at Caddy/nginx (ohmd never sees HTTPS traffic)
- [ ] Firewall allows only 443 (HTTPS) — port 8710 is not exposed
- [ ] Rate limiting at the reverse proxy level (Caddy: `rate_limit` directive)

### Authentication

- [ ] Every agent has its own token
- [ ] CI/CD tokens are scoped to a single tenant (customer API key)
- [ ] Admin tokens are used only for provisioning and management
- [ ] Tokens are stored in secrets management (1Password, Vault, GitHub Secrets) — never in code

### Data

- [ ] Database file permissions: `600` (owner read/write only)
- [ ] Config file permissions: `600` (contains plaintext tokens)
- [ ] Encryption at rest enabled (see [ADR-017](../adr/0017-encryption-at-rest.md))
- [ ] `--no-auth` flag NOT used in production

### Monitoring

- [ ] Prometheus `/metrics` endpoint scraped by your monitoring system
- [ ] `/health` endpoint monitored for uptime alerts
- [ ] DuckLake sync lag tracked (push/pull counts from `sync_heartbeat()`)

---

## Troubleshooting

### Agent cannot connect to ohmd

- Check DNS resolution: `dig ohm.example.com`
- Check TLS certificate: `curl -vI https://ohm.example.com/health`
- Check firewall: only port 443 should be open
- Check ohmd is running: `systemctl status ohmd`
- Check token: `curl -H "Authorization: Bearer <token>" https://ohm.example.com/health`

### MCP sidecar fails to connect

- Verify `ohm_url` uses `https://` (not `http://`) for remote deployments.
- Verify the token is valid and scoped to the correct tenant.
- Check that the reverse proxy forwards `Authorization` and `X-Tenant-ID` headers.

### Cross-tenant data leak

- Customer API keys are scoped to one tenant. If you see data from another tenant, verify the token type:
  - `ohm-cu…` = customer key (tenant-scoped)
  - `ohm-ad…` = admin key (cross-tenant with `X-Tenant-ID`)
- After `OHM-tss4.19`, admin tokens use `X-Tenant-ID` and customer tokens ignore it.

### DuckLake sync failures

- Check S3 credentials and permissions.
- Verify the DuckLake path is correct and writable.
- Check network connectivity from the agent's machine to the S3 endpoint.
- If using a local DuckLake path, ensure the path is shared (NFS, S3, etc.) or use an S3-compatible endpoint.

### Performance: slow remote queries

- Use `OhmStore.for_agent()` with DuckLake sync for zero-latency local reads.
- Increase the sync interval if writes are infrequent.
- Use `/neighborhood?depth=1` instead of `depth=3` for faster queries.
- Enable Prometheus metrics and watch query latency histograms.


---

## Other deployment scenarios

- **Simplest**: [Single-Project Deployment](single-project-ohm.md) — one agent, no daemon, library mode.
- **Local**: [Local Agent Deployment](local-copilot-ohm.md) / [Windows](windows-copilot-ohm.md) — system daemon on localhost with per-tenant MCP.
- **Overview**: [Deployment Guide](deployment.md) — scenario comparison and decision tree.