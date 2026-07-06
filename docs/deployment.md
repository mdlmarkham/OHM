# OHM Deployment Guide

How to run ohmd in production with TLS, authentication, and systemd.

## Quick Start: Caddy Reverse Proxy

ohmd serves plain HTTP on `127.0.0.1:8710`. For production, run it behind
[Caddy](https://caddyserver.com/) — a zero-config reverse proxy that
auto-provisions Let's Encrypt TLS certificates.

### 1. Install Caddy

```bash
# Debian/Ubuntu
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# macOS
brew install caddy
```

### 2. Caddyfile

Create `/etc/caddy/Caddyfile`:

```caddyfile
ohm.example.com {
    reverse_proxy 127.0.0.1:8710

    # Optional: rate limit at the reverse proxy level
    # rate_limit {
    #     zone dynamic {
    #         key {remote_host}
    #         events 100
    #         window 1m
    #     }
    # }

    # Optional: IP allowlist for admin endpoints
    # @admin path /admin*
    # handle @admin {
    #     @allowed remote_ip 10.0.0.0/8 172.16.0.0/12
    #     respond @allowed 403
    # }

    header {
        # Security headers
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        -Server
    }
}
```

### 3. Start Caddy

```bash
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

Caddy auto-provisions TLS certificates from Let's Encrypt. No manual
certificate management needed.

## ohmd Configuration

### Token Setup

Generate tokens for each agent:

```bash
ohmd --init-token metis
ohmd --init-token clio
ohmd --init-token socrates
```

This writes tokens to `~/.ohm/ohmd.json`. Copy tokens to agents securely.

### Production Config

`~/.ohm/ohmd.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8710,
  "db_path": "/var/lib/ohm/ohm.duckdb",
  "tokens": {
    "abc123...": "metis",
    "def456...": "clio",
    "ghi789...": "socrates"
  },
  "roles": {
    "metis": "read-write",
    "clio": "read-write",
    "socrates": "read-only"
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
sudo systemctl enable --now ohmd
```

## Alternative: nginx

If you prefer nginx:

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

## Security Checklist

- [ ] ohmd binds to `127.0.0.1` only (not `0.0.0.0`)
- [ ] TLS terminates at reverse proxy (Caddy/nginx)
- [ ] Tokens configured for all agents
- [ ] Roles set (read-only for observers)
- [ ] `--no-auth` flag NOT used in production
- [ ] Database file permissions: `600` (owner read/write only)
- [ ] Config file permissions: `600` (contains plaintext tokens)
- [ ] Firewall allows only 443 (HTTPS), not 8710 (ohmd direct)
- [ ] systemd service runs as unprivileged `ohm` user

## Per-Agent Local DuckDB

Each agent can run its own local DuckDB for zero-latency reads/writes,
syncing to the shared DuckLake on heartbeat. This eliminates the
single-writer bottleneck of the centralized daemon.

### Setup

```python
from ohm.store import OhmStore
from ohm.schema import SchemaConfig

# Each agent creates its own store
store = OhmStore.for_agent(
    agent_name="metis",
    ducklake_path="/var/lib/ohm/ohm_lake.ducklake",
)

# Read/write locally (zero latency, no HTTP)
store.write_node(id="concept-x", label="X", type="concept", ...)
node = store.get_node("concept-x")

# Sync with other agents on heartbeat
result = store.sync_heartbeat()
# → {"pushed": 3, "pulled": 7, "last_sync": "..."}
```

### Architecture

```
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


## Local agents with MCP

For small teams using GitHub Copilot, Cursor, Claude Code, or OpenCode locally, see [Deploying OHM for Local Agents: System Daemon + Per-Tenant MCP](local-copilot-ohm.md).

### Windows

For the same topology on Windows, see [Deploying OHM on Windows for Local Agents](windows-copilot-ohm.md).
