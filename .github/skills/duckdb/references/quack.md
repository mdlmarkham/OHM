# Quack Remote Protocol Reference

> **Status**: Beta, DuckDB v1.5.2, `core_nightly` repository.
> Protocol and function names subject to change. Stable in v2.0 (September 2026).
> Announced: May 12, 2026. Docs: https://duckdb.org/docs/current/quack/overview

Quack turns any DuckDB instance into an HTTP server. Other DuckDB instances (clients)
connect via `ATTACH 'quack:host'` or `quack_query(...)`. Both client and server
are full DuckDB — the protocol is peer-to-peer, not layered on Postgres or any DBMS.

---

## Install

```sql
-- Both server and client must install from core_nightly (not yet in stable repo)
FORCE INSTALL quack FROM core_nightly;
LOAD quack;
```

---

## Server-Side

### Start a server

```sql
-- Localhost-only (default, safest)
CALL quack_serve('quack:localhost');
-- Returns: listen_uri | url | auth_token (auto-generated if not set)

-- Explicit token (min 4 chars; >= 32 recommended)
CALL quack_serve('quack:localhost', token := 'MY_TOKEN_32_CHARS_OR_MORE_ABCDEF');

-- External bind (requires TLS reverse proxy — see Security below)
CALL quack_serve('quack:0.0.0.0:9494',
                 token := 'MY_TOKEN',
                 allow_other_hostname := true);
```

### Stop a server

```sql
CALL quack_stop('quack:localhost');
```

### Node identity (for fleet management)

```sql
CALL quack_identify(
    name     := 'analytics-node-1',
    provider := 'ec2',
    hostname := 'i-0abc123.eu-west-1.compute.internal',
    region   := 'eu-west-1',
    meta     := '{"role": "worker", "tier": "hot"}'
);
```

### Inspect identity

```sql
FROM whoami();
-- Returns: name, provider, hostname, region, uptime, ts_now, meta
```

### Logging

```sql
-- Enable Quack message log (both client and server)
CALL enable_logging('Quack');
FROM quack_query('quack:localhost', 'SELECT 42');
SELECT * FROM duckdb_logs_parsed('Quack');
-- join on (quack_connection_id, client_query_id) to correlate client↔server

-- HTTP transport log
CALL enable_logging('HTTP');
SELECT request.type, request.url, response.status FROM duckdb_logs_parsed('HTTP');
```

---

## Client-Side

### Authentication (recommended: scoped secret)

```sql
-- Create a secret scoped to the server URI — avoids inline tokens everywhere
CREATE SECRET (
    TYPE quack,
    TOKEN 'MY_TOKEN_32_CHARS_OR_MORE_ABCDEF',
    SCOPE 'quack:srv.example.com'
);

-- Now ATTACH without explicit token
ATTACH 'quack:srv.example.com' AS remote (TYPE quack);
```

### Attach (full catalog)

```sql
-- Localhost (plain HTTP auto-selected for local URIs)
ATTACH 'quack:localhost' AS remote (TYPE quack);

-- Remote (HTTPS auto-selected; disable for plain HTTP test environments only)
ATTACH 'quack:srv.example.com' AS remote (
    TYPE quack,
    TOKEN 'MY_TOKEN_32_CHARS_OR_MORE_ABCDEF'
);

-- Force disable SSL (for explicit HTTP — not recommended in production)
ATTACH 'quack:srv.example.com' AS remote (
    TOKEN 'MY_TOKEN',
    DISABLE_SSL true
);

DETACH remote;
```

### Stateless queries (`quack_query`)

```sql
-- Single query without persisting an attachment
FROM quack_query(
    'quack:localhost',
    'SELECT * FROM events LIMIT 100',
    token := 'MY_TOKEN'
);

-- Use with scoped secret (no inline token needed)
FROM quack_query('quack:srv.example.com', 'SELECT COUNT(*) FROM orders');
```

### Query patterns once attached

```sql
-- Treat remote tables like local ones
SELECT * FROM remote.events WHERE ts > now() - INTERVAL '1 hour';
INSERT INTO remote.staging SELECT * FROM local_batch;
BEGIN; UPDATE remote.orders SET status='shipped' WHERE id=42; COMMIT;

-- Ad-hoc SQL scoped to attachment
FROM remote.query('SELECT version()');

-- Check remote identity
FROM remote.query('FROM whoami()');
```

### URI format

| URI | Host | Port |
|---|---|---|
| `quack:localhost` | localhost | 9494 |
| `quack://localhost` | localhost | 9494 |
| `quack:myhost:9000` | myhost | 9000 |
| `quack:127.0.0.1` | 127.0.0.1 | 9494 |
| `quack:[::1]:1234` | ::1 (IPv6) | 1234 |

Default port: **9494**. Validate a URI: `SELECT quack_uri_parser('quack:host', ssl := false);`

---

## Python Helper (`DuckDBSession`)

```python
import os
from scripts.duckdb_helper import DuckDBSession

# SERVER side
with DuckDBSession("server.duckdb") as srv:
    result = srv.quack_serve(
        "quack:localhost",
        token_env="QUACK_TOKEN",   # read from env — never hardcode
    )
    print(result["listen_uri"], result["auth_token"])

# CLIENT side
with DuckDBSession(":memory:") as cli:
    # Option A: scoped secret (preferred)
    cli.quack_secret(token_env="QUACK_TOKEN", scope="quack:srv.example.com")
    cli.attach_quack("quack:srv.example.com", alias="remote")
    df = cli.query("SELECT * FROM remote.events LIMIT 100")

    # Option B: explicit token from env var
    cli.attach_quack("quack:localhost", alias="local_srv", token_env="QUACK_TOKEN")

    # Stateless query
    df = cli.quack_query("quack:localhost", "SELECT COUNT(*) FROM events",
                          token_env="QUACK_TOKEN")

    # Fleet identity
    cli.quack_identify(alias="remote", name="worker-1", provider="ec2", region="eu-west-1")
```

### Security enforcement in helpers

| Issue | What the helper does |
|---|---|
| Single-quote in URI | `_validate_quack_uri()` raises `ValueError` |
| SQL control chars in URI | Same validator rejects them |
| Empty / short token | `_validate_quack_token()` raises if <4 chars; warns if <32 |
| Single-quote in token | `_validate_quack_token()` raises `ValueError` |
| External server without TLS | `quack_serve(allow_other_hostname=True, require_tls_confirm=True)` raises until cleared |
| Token in debug logs | ATTACH prefix in `_REDACT_PREFIXES` — statement is redacted |
| Token hardcoded | All helpers accept `token_env=` (env var) not plain `token=` |

---

## Security

### Token requirements

- Minimum 4 chars (DuckDB enforces); **32+ chars recommended** for production
- Use a cryptographically random token: `openssl rand -hex 32`
- Never commit tokens to version control; always read from env vars or secrets managers
- Rotate tokens by stopping the server, changing the token, and restarting

### TLS / Encryption

Quack uses plain HTTP by default. For production external deployments:

1. Bind to localhost: `quack:localhost` (default)
2. Put nginx / Caddy / Traefik in front with TLS termination
3. Let the proxy forward to `http://localhost:9494`
4. Clients connect to `quack:public-host` (auto-selects HTTPS for non-local URIs)

```nginx
# Minimal nginx snippet
server {
    listen 443 ssl;
    server_name quack.example.com;
    location / {
        proxy_pass http://127.0.0.1:9494;
    }
}
```

### Authorization

As of v1.5.2, Quack uses **single shared token authentication** — any client with the
token has full access to the server's visible database. Row-level security, per-user
tokens, and OAuth are not yet implemented. Plan accordingly:

- Use separate server instances for different trust boundaries
- Keep the server's DuckDB session scoped to only the data you want to expose
- Monitor access via `CALL enable_logging('Quack')`

---

## Architecture Patterns

### Multi-writer (replace SQLite single-writer limitation)

```
[Writer A] ──quack──► [DuckDB Server] ◄──quack── [Writer B]
                              │
                        server.duckdb (single file, serialised writes)
```

Server handles write serialisation. Replaces: SQLite catalog for DuckLake, or
workaround patterns like pg_duckdb for shared DuckDB access.

### Edge-to-center aggregation

```
[Edge Node A] → DuckDB in-process → ATTACH 'quack:central' → INSERT INTO central.events
[Edge Node B] → DuckDB in-process → ATTACH 'quack:central' → INSERT INTO central.events
[Central]     → DuckDB serving Quack → aggregates all writes → DuckLake S3 export
```

### Browser-to-server (DuckDB-Wasm + Quack)

DuckDB-Wasm natively speaks Quack. No REST API layer needed:
```javascript
// Browser: DuckDB-Wasm client
const db = await DuckDB.instantiate();
await db.attach('quack:analytics.example.com', { token: await getToken() });
const result = await db.query("SELECT * FROM shared.dashboard_data");
```

### Quack vs DuckLake — decision matrix

| | Quack | DuckLake |
|---|---|---|
| **Scale** | ≤few TB (DuckDB native file) | Petabyte+ (object storage) |
| **Concurrency** | Server serialises writes (~thousands/sec) | Postgres catalog (parallel) |
| **Setup complexity** | Extension only (both sides) | Catalog DB + object storage |
| **Engine support** | DuckDB-only | Open spec (any engine) |
| **Time travel** | No | Yes (`AT (VERSION => N)`) |
| **CDC / Change feed** | No | Yes (`table_changes()`) |
| **Status** | Beta (v1.5.2) → stable v2.0 | v1.0 production |

**Use Quack when**: DuckDB-only stack, ≤few TB, need concurrent writes without
external catalog infrastructure, or want browser-to-server queries.

**Use DuckLake when**: petabyte scale, multi-engine access, time travel / CDC needed,
or want an open format without vendor lock-in.

**Combine both**: Run a Quack server that serves a DuckLake-backed database.
Clients write via Quack; data is stored as DuckLake Parquet on S3.

```sql
-- SERVER: attach DuckLake, then serve it over Quack
ATTACH 'ducklake:postgres:dbname=catalog host=pg.example.com'
  AS lake (DATA_PATH 's3://my-bucket/lake/');
USE lake;
CALL quack_serve('quack:localhost', token := 'MY_TOKEN');
-- Clients now ATTACH 'quack:server' and query/write lake.* tables
```

---

## Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| `HTTP Error: Connection refused` | Server not running | `CALL quack_serve(...)` first |
| `Authentication failed` | Wrong token | Check token env var; restart server with new token |
| `allow_other_hostname not set` | External bind without flag | Add `allow_other_hostname := true` + TLS proxy |
| `Extension not found` | Not installed from nightly | `FORCE INSTALL quack FROM core_nightly;` |
| `Protocol mismatch` | Client/server version skew | Ensure same DuckDB version on both sides |
| SSL errors on remote | HTTP endpoint without SSL | Add `DISABLE_SSL true` (non-production only) |
