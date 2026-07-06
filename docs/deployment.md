# OHM Deployment Guide

Choose the deployment model that matches your scale and connectivity needs.

## Deployment scenarios

| Scenario | Agents | Daemon | Network | Best for |
|----------|--------|--------|---------|----------|
| [Single project](single-project-ohm.md) | 1 | None | None (library mode) | A solo developer or researcher with one agent |
| [Local daemon](local-copilot-ohm.md) | 2–10 | One `ohmd --multi-tenant` | localhost only | A small team with multiple agents or tenants |
| [Remote daemon](remote-copilot-ohm.md) | 2–50+ | One `ohmd` behind TLS proxy | HTTPS | Agents on multiple machines, CI/CD, or cloud |
| [Per-agent cache](#per-agent-local-duckdb) | Any | Optional | Sync via DuckLake | Zero-latency local reads with shared knowledge |

Each scenario builds on the one before it. Start with **single project** and add components as needed.

---

## 1. Single project — library mode

The simplest deployment: one agent, one local DuckDB, no daemon, no HTTP.

```python
from ohm.store import OhmStore

store = OhmStore.for_agent(
    agent_name="metis",
    ducklake_path="/var/lib/ohm/ohm_lake.ducklake",  # optional sync
)
store.write_node(id="concept-x", label="X", type="concept")
result = store.sync_heartbeat()  # push/pull from DuckLake if configured
```

See [Single-Project Deployment](single-project-ohm.md) for the full guide.

---

## 2. Local daemon — multi-tenant

One `ohmd` daemon on the local machine, multiple tenants, one MCP sidecar per tenant.

```text
┌─────────────────────────────────────┐
│        system-level ohmd            │
│  (--multi-tenant, port 8710)        │
│  ┌──────────────┐ ┌──────────────┐  │
│  │ tenant: devops│ │ tenant: dataops│ │
│  └──────┬───────┘ └──────┬───────┘  │
└─────────┼────────────────┼──────────┘
     ohm-mcp-devops    ohm-mcp-dataops
          │                │
     Copilot "OHM DevOps"   Copilot "OHM DataOps"
```

- Linux/macOS: [Local Agent Deployment](local-copilot-ohm.md)
- Windows: [Windows Local Agent Deployment](windows-copilot-ohm.md)

---

## 3. Remote daemon — hosted gateway

Agents on different machines connect to a shared `ohmd` over HTTPS. Use a reverse proxy (Caddy, nginx) for TLS termination and access control.

```text
┌──────────────────────┐
│  ohmd behind Caddy   │
│  ohm.example.com     │
│  (TLS, auth, rate    │
│   limiting)          │
└──────────┬───────────┘
           │ HTTPS
    ┌──────┼──────┐
    │      │      │
  Agent 1  Agent 2  CI/CD
  (SDK)    (MCP)   (CLI)
```

See [Remote Daemon Deployment](remote-copilot-ohm.md) for the full guide, or read on for the production Caddy/nginx configuration.

---

## Production reverse proxy

ohmd serves plain HTTP on `127.0.0.1:8710`. For production, run it behind a reverse proxy that handles TLS and access control.

### Caddy (recommended)

```bash
# Debian/Ubuntu
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# macOS
brew install caddy
```

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
sudo systemctl enable --now caddy
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name ohm.example.com;

    ssl_certificate     /etc/letsencrypt/live/ohm.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ohm.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8710;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## ohmd Configuration

### Token Setup

```bash
ohmd --init-token metis
ohmd --init-token clio
```

Writes tokens to `~/.ohm/ohmd.json`. Copy tokens to agents securely.

### Production Config

`~/.ohm/ohmd.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8710,
  "db_path": "/var/lib/ohm/ohm.duckdb",
  "tokens": {
    "abc123...": "metis",
    "def456...": "clio"
  },
  "roles": {
    "metis": "read-write",
    "clio": "read-write"
  }
}
```

### systemd Service

`/etc/systemd/system/ohmd.service`:

```ini
[Unit]
Description=OHM Knowledge Graph Daemon
After=network.target

[Service]
Type=simple
User=ohm
Group=ohm
ExecStart=/usr/local/bin/ohmd
Restart=on-failure
RestartSec=5
Environment=OHM_DB_PATH=/var/lib/ohm/ohm.duckdb
Environment=OHM_HOST=127.0.0.1
Environment=OHM_PORT=8710

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
sudo systemctl enable --now ohmd
```

---

## Security Checklist

- [ ] ohmd binds to `127.0.0.1` only (not `0.0.0.0`)
- [ ] TLS terminates at reverse proxy (Caddy/nginx)
- [ ] Tokens configured for all agents
- [ ] Roles set (read-only for observers)
- [ ] `--no-auth` flag NOT used in production
- [ ] Database file permissions: `600`
- [ ] Config file permissions: `600`
- [ ] Firewall allows only 443 (HTTPS), not 8710

---

## Per-Agent Local DuckDB

Each agent can run its own local DuckDB for zero-latency reads/writes, syncing to a shared DuckLake on heartbeat. This works in any scenario — with or without a daemon.

### Setup

```python
from ohm.store import OhmStore

store = OhmStore.for_agent(
    agent_name="metis",
    ducklake_path="/var/lib/ohm/ohm_lake.ducklake",
)

store.write_node(id="concept-x", label="X", type="concept", ...)
node = store.get_node("concept-x")

result = store.sync_heartbeat()
# → {"pushed": 3, "pulled": 7, "last_sync": "..."}
```

### Architecture

```text
Agent (local DuckDB)  ←→  DuckLake (shared Parquet)  ←→  Agent (local DuckDB)

Each agent:
  - Owns ~/.ohm/agents/{name}/ohm.duckdb
  - Reads/writes locally (microsecond latency)
  - Syncs to DuckLake on heartbeat (push + pull)
  - No daemon required for local operations

ohmd:
  - Still useful for HTTP-only clients
  - Provides /listen, /suggest, /deep endpoints
  - Optional for agents using OhmStore.for_agent()
```

### Conflict Resolution

- Last-write-wins by `updated_at` timestamp
- Conflicts are rare in knowledge graphs (agents write different perspectives, not competing updates)
- Challenge edges handle disagreements without conflict

See [ADR-012](adr/0012-per-agent-local-cache.md) for full details.

---

## Scenario comparison

| Feature | Single project | Local daemon | Remote daemon |
|---------|---------------|--------------|---------------|
| Daemon required | No | Yes (localhost) | Yes (TLS) |
| Multi-tenant | No | Yes | Yes |
| MCP access | No | Yes (stdio/SSE) | Yes (SSE/HTTPS) |
| SDK access | Yes (local) | Yes (local + HTTP) | Yes (HTTPS) |
| Cross-agent sync | DuckLake only | DuckLake + daemon | DuckLake + daemon |
| Setup complexity | Minimal | Medium | Medium + TLS |
| Best for | Solo dev, research | Small team, local agents | Team, CI/CD, cloud |
