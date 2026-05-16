# DuckLake Reference

DuckLake v1.0 (April 2026, production-ready, MIT license) is an open lakehouse
format: SQL catalog + Parquet data on object storage. ACID across multiple tables,
time travel, schema evolution, deletion vectors, Change Data Feed — with zero
metadata file sprawl (vs Iceberg/Delta).

---

## Catalog Options

| Catalog | Attach string | Best for |
|---|---|---|
| DuckDB file | `ducklake:metadata.ducklake` | Single-writer local / dev |
| SQLite | `ducklake:sqlite:metadata.sqlite` | Single-writer; portable |
| PostgreSQL | `ducklake:postgres:dbname=cat host=pg.example.com` | Multi-writer concurrent |

> **Concurrent writers**: Use Postgres catalog. DuckDB and SQLite catalogs
> support only one writer at a time (DuckLake handles SQLite by
> detach/reattach cycle internally).

---

## Setup: DuckDB file catalog + S3 data

```sql
INSTALL ducklake; LOAD ducklake;
INSTALL httpfs;   LOAD httpfs;
INSTALL aws;      LOAD aws;   -- auto-load AWS_ env vars

CREATE SECRET s3_creds (
  TYPE s3, REGION 'us-east-1'
  -- KEY_ID / SECRET if not using IAM role / env vars
);

ATTACH 'ducklake:metadata.ducklake' AS lake (
  DATA_PATH 's3://my-bucket/lake/'
);
USE lake;
```

## Setup: Postgres catalog + S3 data (multi-writer)

```sql
INSTALL ducklake; LOAD ducklake;
INSTALL postgres; LOAD postgres;
INSTALL httpfs;   LOAD httpfs;

-- Postgres must have a database named ducklake_catalog (or any name)
ATTACH 'ducklake:postgres:dbname=ducklake_catalog host=pg.example.com user=cat password=secret'
  AS lake (DATA_PATH 's3://my-bucket/lake/');
```

---

## DDL

```sql
-- Create table
CREATE TABLE lake.events (
  id         BIGINT PRIMARY KEY,
  ts         TIMESTAMPTZ DEFAULT now(),
  source     VARCHAR,
  payload    JSON
) PARTITION BY (DATE_TRUNC('month', ts));

-- Partitioned DuckLake tables write each partition as separate Parquet files
-- (reduces compaction overhead for time-series workloads)

-- Schema evolution — fully backward-compatible
ALTER TABLE lake.events ADD COLUMN user_id INTEGER;
ALTER TABLE lake.events RENAME COLUMN payload TO body;
ALTER TABLE lake.events DROP COLUMN source;     -- logical delete; data preserved

-- Create view
CREATE VIEW lake.recent_events AS
SELECT * FROM lake.events WHERE ts > now() - INTERVAL '7 days';
```

---

## DML: Insert, Update, Delete

```sql
-- Bulk insert
INSERT INTO lake.events SELECT * FROM staging;

-- Update (uses deletion vectors — efficient; no full file rewrite)
UPDATE lake.events SET user_id = 42 WHERE id = 1001;

-- Delete (deletion vector)
DELETE FROM lake.events WHERE ts < now() - INTERVAL '365 days';

-- MERGE / UPSERT
MERGE INTO lake.events AS target
USING staging AS source ON target.id = source.id
WHEN MATCHED     THEN UPDATE SET body = source.body, ts = source.ts
WHEN NOT MATCHED THEN INSERT VALUES (source.id, source.ts, source.body, source.user_id);
```

---

## Time Travel

```sql
-- Query at a specific snapshot version
SELECT * FROM lake.events AT (VERSION => 5);

-- Query all versions side by side
SELECT 'v3' AS ver, COUNT(*) FROM lake.events AT (VERSION => 3)
UNION ALL
SELECT 'v5', COUNT(*) FROM lake.events AT (VERSION => 5);

-- Attach as a read-only point-in-time view (all tables in the catalog)
ATTACH 'ducklake:metadata.ducklake' AS snap_jan
  (SNAPSHOT_TIME '2026-01-31 23:59:59', READ_ONLY);
SELECT * FROM snap_jan.events LIMIT 10;
DETACH snap_jan;
```

---

## Change Data Feed (CDC)

```sql
-- All row-level changes between snapshot 3 and 7
FROM lake.table_changes('events', 3, 7);
-- Returns: snapshot_id, rowid, change_type ('insert'|'delete'|'update_pre'|'update_post'), ...data cols

-- Incremental pipeline: process only new changes since last run
-- Store last processed snapshot_id in a watermark table
SELECT MAX(snapshot_id) AS last_snap FROM watermarks WHERE table_name = 'events';
FROM lake.table_changes('events', :last_snap + 1, :current_snap);
```

---

## Snapshot & File Management

```sql
-- List all snapshots
SELECT * FROM lake.snapshots() ORDER BY snapshot_id;

-- Inspect Parquet files for a table
SELECT * FROM lake.ducklake_files('events') ORDER BY file_size DESC;

-- Compact small Parquet files (merge into fewer, larger files)
CALL lake.ducklake_compact('events');

-- Compact all tables
CALL lake.ducklake_compact_all();

-- Expire old snapshots (keep last 30 days of history)
CALL lake.ducklake_expire_snapshots(
  table_name => 'events',
  older_than  => now() - INTERVAL '30 days'
);
```

---

## ACID Multi-Table Transactions

```sql
-- Atomic insert into two tables (both succeed or both roll back)
BEGIN;
INSERT INTO lake.orders VALUES (101, now(), 'pending');
INSERT INTO lake.order_items VALUES (101, 'widget', 3, 9.99);
COMMIT;

-- Roll back if validation fails
BEGIN;
INSERT INTO lake.events SELECT * FROM new_batch;
-- Validate
SELECT COUNT(*) FROM lake.events AT (VERSION => (SELECT MAX(snapshot_id) FROM lake.snapshots()));
ROLLBACK;  -- or COMMIT
```

---

## Python Integration

```python
import os
from scripts.duckdb_helper import DuckDBSession

with DuckDBSession("local_cache.duckdb") as db:
    # Attach DuckLake (Python helper validates alias and path)
    db.attach_ducklake(
        catalog="postgres:dbname=ducklake_catalog host=pg.example.com",
        data_path="s3://my-bucket/lake/",
        alias="lake",
    )

    # Time-travel query via helper
    df = db.ducklake_time_travel("", table="events", alias="lake", version=3)

    # List snapshots as TOON
    snaps = db.ducklake_snapshots("lake")
    print(db.to_toon(snaps, "lake_snapshots"))

    # Compact
    db.ducklake_compact("events", alias="lake")
```

---

## Migration from Iceberg / Delta

```sql
-- Read existing Iceberg table, write into DuckLake
INSTALL iceberg; LOAD iceberg;
INSERT INTO lake.orders
SELECT * FROM iceberg_scan('s3://old-warehouse/orders/');

-- Read Delta table, write into DuckLake
INSTALL delta; LOAD delta;
INSERT INTO lake.shipments
SELECT * FROM delta_scan('s3://old-bucket/delta/shipments/');

-- Validate row counts
SELECT
  (SELECT COUNT(*) FROM lake.orders) AS ducklake_rows,
  (SELECT COUNT(*) FROM iceberg_scan('s3://old-warehouse/orders/')) AS iceberg_rows;
```

---

## DuckLake vs Iceberg vs Delta — Decision Guide

| Criterion | DuckLake | Iceberg | Delta Lake |
|---|---|---|---|
| Metadata location | SQL database | S3 files | S3 files (`_delta_log/`) |
| Cold query planning | ~2ms (SQL index lookup) | ~200ms (S3 manifest scan) | ~100ms |
| Concurrent writers | Postgres catalog | External catalog needed | S3 lock-based |
| Time travel | VERSION / SNAPSHOT_TIME | Snapshot ID / timestamp | Version / timestamp |
| Small files problem | Data inlining + SQL metadata | Compaction required | Compaction required |
| Engine compatibility | DuckDB-first; spec is open | Spark, Flink, Trino, etc. | Spark, Databricks native |
| Write support in DuckDB | Full (INSERT/UPDATE/DELETE) | Full | Read-only (write via Python `deltalake`) |
| Production readiness | v1.0 (April 2026) | Mature | Mature |

**Choose DuckLake when**: DuckDB is the primary engine; you need ACID multi-table;
you want minimal infrastructure; you value fast metadata operations.

**Choose Iceberg when**: Multi-engine access (Spark + Flink + Trino); existing
Unity Catalog or AWS Glue investment.

**Choose Delta when**: Databricks is the primary platform.
