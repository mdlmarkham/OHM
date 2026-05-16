---
name: duckdb
description: >
  Use DuckDB as a local analytical engine, lakehouse gateway, and networked
  server. Pulls data from PostgreSQL, DuckLake, Iceberg, Delta Lake, Parquet,
  CSV, and JSON; caches locally; performs OLAP, VSS/HNSW vector search for RAG,
  graph BFS/shortest-path via USING KEY CTEs, hierarchy queries, and DuckLake
  lakehouse management. Quack (v1.5.2 beta) enables multi-writer client-server
  deployments and remote computation. Token-efficient TOON output for LLMs.
  Covers DuckDB 1.5+ features including USING KEY, multi-attach, Quack.
license: MIT
compatibility: >
  Python 3.9+, duckdb>=1.5.2. DuckLake v1.0 (production-ready, April 2026).
  Quack: FORCE INSTALL quack FROM core_nightly (beta, stable in v2.0 Sep 2026).
  VSS: INSTALL vss (experimental; HNSW, FLOAT[] only). Optional: numpy/
  sentence-transformers for embedding generation. Postgres catalog for DuckLake
  requires Postgres 12+. Extensions auto-install via INSTALL/LOAD.
metadata:
  author: TitanClawOps
  version: "3.0"
  toon-version: "3.0"
allowed-tools: Bash Read Write
---

# DuckDB v3 — Lakehouse · Analytics · Vectors · Graphs · Hierarchies · Quack

DuckDB 1.5+ is an embedded OLAP engine with DuckLake lakehouse, Quack client-server protocol, VSS/HNSW vector search for RAG, `USING KEY` graph CTEs, and LTREE-style hierarchies.

## Quick Orientation

| Goal | Pattern |
|---|---|
| Attach Postgres / Iceberg / Delta | [Source Connections](#sources) |
| DuckLake lakehouse (ACID, time travel) | [DuckLake Pattern](#ducklake) |
| Vector search / RAG embeddings | [VSS / RAG Pattern](#vss) |
| Graph BFS, shortest path | [Graph Pattern](#graph) |
| Org chart, BOM, path hierarchies | [Hierarchy Pattern](#hierarchy) |
| Incremental cache refresh | [Caching Strategy](#cache) |
| Write back to Postgres / Parquet | [Write-Back Pattern](#writeback) |
| Token-efficient LLM output | [TOON Output Pattern](#toon) |
| Multi-writer / remote access to DuckDB | [Quack Pattern](#quack) |

---

## 30-Second Quick Start

> **Credentials**: always read from environment variables — never hardcode.

```python
import os
from scripts.duckdb_helper import DuckDBSession

with DuckDBSession("cache.duckdb") as db:
    db.attach_postgres(
        host="db.example.com", dbname="prod",
        user="reader", password=os.environ["PG_PASSWORD"],
    )
    df = db.query(
        "SELECT * FROM pg.public.orders WHERE created_at > now() - INTERVAL '7 days'"
    )
    print(db.to_toon(df, "recent_orders"))
```

---

## Source Connections {#sources}

### Postgres
```python
db.attach_postgres(host="db", dbname="prod", user="r",
                   password=os.environ["PG_PASSWORD"], alias="pg")
df = db.query("SELECT * FROM pg.public.orders LIMIT 1000")
```

### Iceberg & Delta
```python
db.load_extensions("iceberg", "httpfs")
db.configure_s3(region="us-east-1")   # reads AWS_ env vars if no keys supplied
df = db.query("SELECT * FROM iceberg_scan('s3://warehouse/prod/orders/')")
df = db.query("SELECT * FROM delta_scan('s3://bucket/delta/shipments/')")
```

### Parquet / CSV / JSON
```sql
SELECT * FROM read_parquet('s3://bucket/prefix/**/*.parquet', hive_partitioning=true);
SELECT * FROM read_csv('/data/export.csv', auto_detect=true);
SELECT * FROM read_json('/data/events.ndjson', auto_detect=true);
```

Full connection strings for all sources → [`references/sources.md`](references/sources.md)

---

## DuckLake Pattern {#ducklake}

DuckLake v1.0 (April 2026, production-ready) is an open lakehouse format:
SQL catalog (DuckDB file / Postgres / SQLite) + Parquet files on object storage.
It provides ACID multi-table transactions, time travel, schema evolution,
deletion vectors, and Change Data Feed — with no metadata file sprawl.

### Attach
```python
db.attach_ducklake(
    catalog="metadata.ducklake",           # local DuckDB file catalog (simplest)
    data_path="s3://my-bucket/lake/",      # Parquet data on S3
    alias="lake",
)
# Postgres catalog (recommended for concurrent writers)
db.attach_ducklake(
    catalog="postgres:dbname=ducklake_catalog host=pg.example.com",
    data_path="s3://my-bucket/lake/",
    alias="lake",
)
```

### Create, insert, update
```sql
USE lake;
CREATE TABLE lake.events (id BIGINT, ts TIMESTAMPTZ, payload JSON);
INSERT INTO lake.events VALUES (1, now(), '{"type":"click"}');
UPDATE lake.events SET payload = '{"type":"view"}' WHERE id = 1;
```

### Time travel
```sql
-- By snapshot version
SELECT * FROM lake.events AT (VERSION => 3);

-- By timestamp (attach at a point in time)
ATTACH 'ducklake:metadata.ducklake' AS snap
  (SNAPSHOT_TIME '2026-01-15 00:00:00');
SELECT * FROM snap.events;
```

### Compaction & maintenance
```sql
CALL lake.ducklake_compact('events');          -- merge small Parquet files
SELECT * FROM lake.snapshots();               -- list all snapshots
SELECT * FROM lake.ducklake_files('events');  -- inspect Parquet file inventory
```

Full DuckLake reference → [`references/ducklake.md`](references/ducklake.md)

---

## VSS / RAG Pattern {#vss}

DuckDB's `vss` extension provides HNSW (Hierarchical Navigable Small Worlds)
approximate nearest-neighbor search over fixed-size `FLOAT[]` arrays.

### Setup
```python
db.load_extensions("vss")
db.execute("SET hnsw_enable_experimental_persistence = true;")
```

### Store embeddings & build index
```sql
CREATE TABLE embeddings (
    id          VARCHAR PRIMARY KEY,
    content     VARCHAR,
    embedding   FLOAT[384]    -- dimension must match your model (e.g. all-MiniLM-L6-v2)
);

-- Build HNSW index AFTER bulk load (much faster than insert-then-index)
CREATE INDEX emb_hnsw ON embeddings USING HNSW (embedding)
  WITH (metric = 'cosine');
```

### Similarity search (RAG retrieval)
```sql
-- Top-5 nearest neighbours to a query vector (? = Python list → FLOAT[384])
WITH top_k AS (
  SELECT id, content,
         array_cosine_distance(embedding, ?::FLOAT[384]) AS distance
  FROM embeddings
  ORDER BY distance
  LIMIT 5
)
SELECT * FROM top_k ORDER BY distance;
```

### Hybrid search (vector + metadata filter)
```sql
-- Semantic filter THEN metadata filter (index only fires on inner query)
WITH candidates AS (
  SELECT id, content,
         array_cosine_distance(embedding, ?::FLOAT[384]) AS distance
  FROM embeddings
  ORDER BY distance
  LIMIT 50                   -- over-fetch for post-filter
)
SELECT c.id, c.content, c.distance
FROM candidates c
JOIN documents d USING (id)
WHERE d.category = 'maintenance'
  AND d.ts > now() - INTERVAL '90 days'
ORDER BY c.distance
LIMIT 5;
```

Full VSS reference, distance metrics, hybrid search, index tuning, `VectorStore` Python class → [`references/vectors-graphs-hierarchies.md`](references/vectors-graphs-hierarchies.md)

---

## Graph Pattern {#graph}

DuckDB 1.3+ adds `USING KEY` recursive CTEs — dramatically more efficient for
graph algorithms (shortest path, distance-vector routing) than vanilla `WITH RECURSIVE`.

### Shortest path (Dijkstra via USING KEY)
```sql
-- DuckDB 1.3+ USING KEY: updates existing rows instead of appending duplicates
-- → orders-of-magnitude smaller union table vs. vanilla UNION ALL
WITH RECURSIVE dijkstra(node, dist, path) USING KEY (node) AS (
    SELECT 1 AS node, 0.0 AS dist, [1] AS path
  UNION
    SELECT e.dst,
           d.dist + e.weight,
           list_append(d.path, e.dst)
    FROM dijkstra d
    JOIN edges e ON e.src = d.node
    LEFT JOIN dijkstra cur ON cur.node = e.dst   -- access recurring table
    WHERE d.dist + e.weight < COALESCE(cur.dist, 1e18)
)
SELECT node, dist, path FROM dijkstra ORDER BY dist;
```

### BFS / Neighbor lookup
```sql
-- BFS all reachable (USING KEY — depth-limited, cycle-safe)
WITH RECURSIVE bfs(node, depth) USING KEY (node) AS (
    SELECT 1, 0
  UNION
    SELECT e.dst, b.depth + 1 FROM bfs b JOIN edges e ON e.src = b.node WHERE b.depth < 10
)
SELECT * FROM bfs ORDER BY depth;

-- 1-hop neighbors
SELECT n.label, e.weight FROM edges e JOIN nodes n ON n.id = e.dst WHERE e.src = ?;
```

Full graph patterns (PageRank, components, cycle detection) → [`references/vectors-graphs-hierarchies.md`](references/vectors-graphs-hierarchies.md)

---

## Hierarchy Pattern {#hierarchy}

DuckDB has no native LTREE type, but three proven patterns cover all use cases.

### Pattern A — Materialized Path (LTREE equivalent, best for reads)
```sql
CREATE TABLE categories (
    id      INTEGER PRIMARY KEY,
    name    VARCHAR,
    path    VARCHAR NOT NULL,   -- e.g. 'root.industrial.motors.bearings'
    depth   INTEGER GENERATED ALWAYS AS (len(string_split(path, '.')) - 1) VIRTUAL
);
CREATE INDEX cat_path ON categories(path);

-- All descendants of a node
SELECT * FROM categories WHERE path LIKE 'root.industrial.%';

-- Immediate children
SELECT * FROM categories
WHERE path LIKE 'root.industrial.%'
  AND depth = (SELECT depth FROM categories WHERE path = 'root.industrial') + 1;

```

### Pattern B — Adjacency List + Recursive CTE (best for writes)
```sql
CREATE TABLE tree (id INTEGER PRIMARY KEY, parent_id INTEGER, name VARCHAR);

WITH RECURSIVE subtree AS (
    SELECT id, parent_id, name, 0 AS depth, CAST(name AS VARCHAR) AS full_path
    FROM tree WHERE id = ?          -- root of subtree
  UNION ALL
    SELECT c.id, c.parent_id, c.name, s.depth + 1,
           s.full_path || '.' || c.name
    FROM tree c JOIN subtree s ON c.parent_id = s.id
)
SELECT * FROM subtree ORDER BY full_path;
```

Full hierarchy patterns (closure table, BOM rollup, org chart, ISA-95 equipment tree) → [`references/vectors-graphs-hierarchies.md`](references/vectors-graphs-hierarchies.md)

---

## Caching Strategy {#cache}

```python
from scripts.duckdb_helper import DuckDBSession

with DuckDBSession("cache.duckdb") as db:
    db.attach_postgres(host="db", dbname="prod", user="r",
                       password=os.environ["PG_PASSWORD"])
    rows = db.refresh_table(
        source_query="SELECT * FROM pg.public.sensor_readings",
        table="sensor_cache",   # identifier-validated before SQL
        watermark_col="ts",     # identifier-validated; value passed as ?
    )
    print(f"Appended {rows} new rows")
```

> **Security**: `table` and `watermark_col` are validated against
> `[A-Za-z_][A-Za-z0-9_]*`. Untrusted strings raise `ValueError` before SQL.

```sql
-- Cache health
SELECT table_name, estimated_size, column_count FROM duckdb_tables()
ORDER BY estimated_size DESC;
CHECKPOINT;   -- flush WAL
```

---

## Analysis Patterns

```sql
-- Running total + pct share
SELECT date_trunc('day', ts) AS day, SUM(value) AS daily_total,
       SUM(SUM(value)) OVER (ORDER BY date_trunc('day', ts)) AS running_total,
       ROUND(100.0 * SUM(value) / SUM(SUM(value)) OVER (), 2) AS pct
FROM sensor_cache GROUP BY 1 ORDER BY 1;

-- ASOF join: sensor reading → nearest recipe step
SELECT s.ts, s.value, r.step_name FROM sensor_cache s
ASOF JOIN recipe_steps r ON (s.machine_id = r.machine_id AND s.ts >= r.start_ts);

-- Pivot rows → columns; profile column stats
PIVOT sensor_cache ON tag_name USING AVG(value) GROUP BY date_trunc('hour', ts);
SUMMARIZE sensor_cache;    -- min/max/null/distinct per column
```

Advanced patterns (gaps, sessionisation, fuzzy match, spatial, YoY, LOCF) → [`references/analysis-patterns.md`](references/analysis-patterns.md)

---

## Quack Pattern {#quack}

> **⚠ Beta (v1.5.2)**: Quack ships in `core_nightly`. Protocol and function names
> subject to change. Stable in DuckDB v2.0 (September 2026).

Quack is DuckDB's native HTTP-based client-server protocol — two DuckDB instances
talking to each other, enabling concurrent multi-writer access, remote computation, and
edge-to-central aggregation. **Both client and server are full DuckDB instances.**

```python
# SERVER — everything the session sees becomes reachable over quack:
db.quack_serve("quack:localhost", token_env="QUACK_TOKEN")
# External (requires TLS reverse proxy — enforce with require_tls_confirm=False):
db.quack_serve("quack:0.0.0.0:9494", token_env="QUACK_TOKEN",
               allow_other_hostname=True, require_tls_confirm=False)

# CLIENT — attach remote as full catalog:
db.attach_quack("quack:srv.example.com", alias="remote", token_env="QUACK_TOKEN")
df = db.query("SELECT * FROM remote.events LIMIT 100")  # runs on server

# Stateless query (no attach):
df = db.quack_query("quack:localhost", "SELECT * FROM events", token_env="QUACK_TOKEN")

# Recommended: scoped secret so ATTACH needs no inline token
db.quack_secret(token_env="QUACK_TOKEN", scope="quack:srv.example.com")
db.attach_quack("quack:srv.example.com", alias="remote")
```

**Quack vs DuckLake** — choose based on scale and compatibility needs:

| Criterion | Quack | DuckLake |
|---|---|---|
| Storage | DuckDB native file (≤few TB) | Object storage Parquet (PB+) |
| Concurrency | Server serialises writes | Postgres catalog handles |
| Setup | Extension on both ends only | Catalog DB + object storage |
| Engine | DuckDB-only | Open spec — any engine |
| Status | Beta (stable Sep 2026) | v1.0 production |
Full Quack reference → [`references/quack.md`](references/quack.md)

---

## Write-Back Pattern {#writeback}

```python
# Postgres — dry_run=True default; schema/table/column names are identifier-validated
db.writeback_postgres(source_table_or_query="summary_table",
    target_schema="public", target_table="summary",
    pg_alias="pg_rw", conflict_key="id", dry_run=False)

# Parquet — atomic (temp dir + rename); path null-byte checked; compression allowlisted
db.writeback_parquet(source_table_or_query="summary_table",
    output_path="/output/summary.parquet",
    partition_by=["year", "month"], dry_run=False)
```

CLI (`--no-dry-run` to commit; `--pg-password-env` preferred over `--pg-password`):
```bash
PG_PASSWORD=s python scripts/duckdb_writeback.py \
  --source cache.duckdb --query "SELECT * FROM agg" \
  --target postgres --pg-host db --pg-db prod --pg-user writer \
  --pg-password-env PG_PASSWORD --pg-table summary --no-dry-run
```

---

## TOON Output Pattern {#toon}

```python
df = db.query("SELECT tag, AVG(value) avg, MAX(value) peak FROM sensor_cache GROUP BY tag")
print(db.to_toon(df, "sensor_summary"))
# → sensor_summary[3]{tag,avg,peak}:
#     TEMP_01,72.4,98.1  ...
```

**Rules:** uniform arrays → TOON tabular · single row → key-value · nested → JSON compact · >500 rows → paginate first. TOON pagination, schema-as-TOON → [`references/toon-patterns.md`](references/toon-patterns.md)

---

## Extension Reference

| Source/Feature | Extension | Notes |
|---|---|---|
| PostgreSQL | `postgres` | Read/write; DSN escaping in `attach_postgres()` |
| DuckLake | `ducklake` | v1.0 production; also needs `httpfs` for S3 data |
| **Quack** | `quack` | **Beta** `core_nightly`; DuckDB client-server HTTP protocol |
| Iceberg | `iceberg` | REST, Glue, Unity catalogs |
| Delta Lake | `delta` | Read-only in DuckDB; write via `deltalake` Python lib |
| S3 / GCS / Azure | `httpfs` | Credentials via `CREATE SECRET` (not `SET`) |
| VSS / HNSW | `vss` | Experimental; `FLOAT[]` only; persistent index opt-in |
| Lance | `lance` | ML-friendly columnar format; efficient for embedding workloads |
| Vortex | `vortex` | High-performance columnar format (Spiral Analytics) |
| SQLite | `sqlite` | Single-writer; DuckLake detaches/reattaches automatically |
| MySQL / MariaDB | `mysql` | Read/write ATTACH |
| Unity Catalog | `unity_catalog` | Databricks Unity Catalog |
| Spatial | `spatial` | ST_Distance, ST_DWithin, Hilbert encoding |
| Full-text search | `fts` | Inverted index; complement to VSS for hybrid search |
| AWS credentials | `aws` | Auto-loads AWS env-var credentials |
| Avro | `avro` | Read Apache Avro files |

```sql
SELECT * FROM duckdb_extensions() WHERE loaded = true;
```

---

## Security Model

| Concern | Mitigation |
|---|---|
| SQL injection via identifiers | `_require_identifier()` — `[A-Za-z_][A-Za-z0-9_]*` allowlist |
| DSN credential injection | `_dsn_escape()` — libpq quoting on all DSN values |
| PRAGMA injection | `memory_limit` regex-validated; `threads` cast to `int` |
| Path traversal | Null-byte check; caller applies `Path.resolve()` for root confinement |
| Extension injection | `ensure_extension()` checks `KNOWN_EXTENSIONS` frozenset |
| Credential log leakage | `ATTACH`/`CREATE SECRET` statements redacted from DEBUG output |
| Quack URI injection | `_validate_quack_uri()` — allowlist regex; rejects SQL control chars |
| Quack token injection | `_validate_quack_token()` — rejects `'`, null bytes; warns if <32 chars |
| Quack external plaintext | `require_tls_confirm` guard on `allow_other_hostname=True` |

Source SQL arguments (`source_query`, `source_sql`) are not parsed — treat as parameterized SQL bodies from trusted code only.

---

## Reference Files

- [`references/quack.md`](references/quack.md) — Quack: server setup, client patterns, auth, TLS, fleet management, logging
- [`references/ducklake.md`](references/ducklake.md) — DuckLake: catalog options, S3 setup, time travel, CDC, compaction, concurrency, migration from Iceberg/Delta
- [`references/vectors-graphs-hierarchies.md`](references/vectors-graphs-hierarchies.md) — VSS/HNSW tuning, hybrid search, PageRank, connected components, cycle detection, BOM rollup, ISA-95 equipment trees
- [`references/sources.md`](references/sources.md) — Full connection strings: Postgres, Iceberg, Delta, S3/GCS/Azure, SQLite, MySQL, Hive Metastore
- [`references/analysis-patterns.md`](references/analysis-patterns.md) — Time-series, ASOF joins, pivots, spatial, fuzzy match, sampling
- [`references/toon-patterns.md`](references/toon-patterns.md) — TOON serialisation rules, pagination, multi-table context
- [`scripts/duckdb_helper.py`](scripts/duckdb_helper.py) — `DuckDBSession`: connections, TOON, refresh, write-back, DuckLake attach, VSS helpers
- [`scripts/vector_helper.py`](scripts/vector_helper.py) — `VectorStore`: embedding upsert, HNSW index management, RAG search, hybrid search
- [`scripts/duckdb_writeback.py`](scripts/duckdb_writeback.py) — CLI write-back with blast-radius checks

## Error Handling & Tuning

| Error | Cause | Fix |
|---|---|---|
| `Extension not found` | Not installed | `INSTALL <ext>; LOAD <ext>;` |
| `Connection refused` | Wrong host/port | Verify DSN; check firewall |
| `Catalog error` | Wrong schema | `SHOW ALL TABLES;` |
| `Out of memory` | Unbounded scan | Add WHERE; tune below |
| `Lock conflict` on .duckdb | Two writers | Single-writer or Postgres catalog |
| DuckLake `snapshot not found` | VERSION out of range | `SELECT * FROM lake.snapshots();` |
| HNSW `index out of memory` | Too large for RAM | Reduce `ef_construction`; pre-filter |
| `USING KEY` infinite loop | No termination | Add `WHERE depth < N` |
| Quack `connection refused` | Server not running | Verify `quack_serve` active; default port 9494 |
| Quack `authentication failed` | Wrong token | Verify token env var matches server |
| Quack `allow_other_hostname` guard | require_tls_confirm not cleared | Set `require_tls_confirm=False` after adding TLS proxy |

```sql
PRAGMA memory_limit='8GB'; PRAGMA threads=8;
PRAGMA temp_directory='/tmp/duckdb_spill';
PRAGMA enable_object_cache=true;  -- cache Parquet footer metadata (1.4+)
```
