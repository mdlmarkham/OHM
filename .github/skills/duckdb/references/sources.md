# DuckDB Source Connection Reference

Complete connection strings for every source type supported by DuckDB extensions.
Read this file when the main SKILL.md quick-start is insufficient for your source type.


---

## DuckLake (v1.0 — April 2026)

DuckLake stores metadata in a SQL catalog (DuckDB file / Postgres / SQLite)
and data as Parquet files on local storage or object storage.

```sql
INSTALL ducklake; LOAD ducklake;
```

### DuckDB file catalog (single-writer, simplest)
```sql
ATTACH 'ducklake:metadata.ducklake' AS lake (DATA_PATH 'data/');
-- S3 data path
ATTACH 'ducklake:metadata.ducklake' AS lake (DATA_PATH 's3://my-bucket/lake/');
```

### SQLite catalog (single-writer, portable)
```sql
INSTALL sqlite; LOAD sqlite;
ATTACH 'ducklake:sqlite:metadata.sqlite' AS lake (DATA_PATH 'data/');
```

### PostgreSQL catalog (multi-writer concurrent — recommended for teams)
```sql
INSTALL postgres; LOAD postgres;
-- Postgres must have a database for the catalog (any name works)
ATTACH 'ducklake:postgres:dbname=ducklake_catalog host=pg.example.com user=cat password=secret'
  AS lake (DATA_PATH 's3://my-bucket/lake/');
```

### S3 credentials for DuckLake data layer
```sql
INSTALL httpfs; LOAD httpfs;
INSTALL aws; LOAD aws;   -- auto-loads AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN

-- Or explicit secret
CREATE OR REPLACE SECRET lake_s3 (
  TYPE s3, REGION 'us-east-1',
  KEY_ID 'AKIA...', SECRET 'my-secret'
);
```

### Read-only point-in-time attach
```sql
-- By snapshot version
ATTACH 'ducklake:metadata.ducklake' AS snap_v5 (VERSION => 5, READ_ONLY);
-- By timestamp
ATTACH 'ducklake:metadata.ducklake' AS snap_jan
  (SNAPSHOT_TIME '2026-01-31 23:59:59', READ_ONLY);
```

### Inspect attached DuckLake
```sql
SELECT * FROM lake.snapshots() ORDER BY snapshot_id;         -- version history
SELECT * FROM lake.ducklake_files('my_table');               -- Parquet file inventory
SELECT * FROM information_schema.tables WHERE table_schema != 'information_schema';
SHOW ALL TABLES;
```

### Python helper
```python
db.attach_ducklake(
    catalog="postgres:dbname=ducklake_catalog host=pg.example.com",
    data_path="s3://my-bucket/lake/",
    alias="lake",                    # identifier-validated
    read_only=False,
    # Optional time-travel:
    # snapshot_version=5,
    # snapshot_time="2026-01-31 23:59:59",
)
```

---

## PostgreSQL

### Basic attachment
```sql
INSTALL postgres; LOAD postgres;
ATTACH 'host=db.example.com port=5432 dbname=prod user=reader password=s3cr3t'
  AS pg (TYPE postgres, READ_ONLY);
```

### SSL connection
```sql
ATTACH 'host=db.example.com dbname=prod user=reader password=s3cr3t sslmode=require'
  AS pg (TYPE postgres, READ_ONLY);
```

### Schema inspection after attach
```sql
SHOW ALL TABLES;                             -- all schemas + tables across all attached databases
SELECT * FROM pg.information_schema.tables;  -- Postgres catalog passthrough
DESCRIBE pg.public.orders;                   -- column types
```

### Filtering pushdown — always add WHERE / LIMIT
```sql
-- DuckDB pushes WHERE and LIMIT to Postgres; never do SELECT * without a filter on large tables
SELECT * FROM pg.public.events WHERE created_at > now() - INTERVAL '1 day' LIMIT 10000;
```

---

## Apache Iceberg

### REST catalog
```sql
INSTALL iceberg; LOAD iceberg;

CREATE SECRET iceberg_rest (
  TYPE iceberg,
  CATALOG_URL 'https://polaris.example.com',
  TOKEN      'my-bearer-token'
);

-- List tables
SHOW ALL TABLES IN iceberg_rest;

-- Scan
SELECT * FROM iceberg_scan('s3://warehouse/db/orders', allow_moved_paths=true);
```

### AWS Glue catalog
```sql
-- Requires httpfs + AWS credentials
INSTALL httpfs; LOAD httpfs;
SET s3_region='us-east-1';
SET s3_access_key_id='AKIAIOSFODNN7EXAMPLE';
SET s3_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY';

SELECT * FROM iceberg_scan('s3://my-warehouse/db/table/', version_name_format='v%s%s.metadata.json');
```

### Unity Catalog (Databricks)
```sql
CREATE SECRET unity (
  TYPE iceberg,
  CATALOG_URL 'https://<workspace>.azuredatabricks.net/api/2.1/unity-catalog/iceberg',
  TOKEN       'dapi...',
  AUTHORIZATION_TYPE 'OAUTH2'
);
```

### Time-travel queries
```sql
-- By snapshot ID
SELECT * FROM iceberg_scan('s3://warehouse/orders', snapshot_id=8765432100);

-- By timestamp
SELECT * FROM iceberg_scan('s3://warehouse/orders', version_name_format='%s-%s.metadata.json');
-- DuckDB 1.1+ supports AS OF syntax for Iceberg:
SELECT * FROM iceberg_scan('s3://warehouse/orders') AS OF TIMESTAMP '2025-01-15 00:00:00';
```

---

## Delta Lake

### Local Delta table
```sql
INSTALL delta; LOAD delta;
SELECT * FROM delta_scan('/data/delta/orders/');
```

### Delta on S3
```sql
INSTALL httpfs; LOAD httpfs;
SET s3_region='us-east-1';
-- ... set credentials ...
SELECT * FROM delta_scan('s3://bucket/delta/orders/');
```

### Azure ADLS Gen2
```sql
SET azure_storage_connection_string='DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...';
SELECT * FROM delta_scan('abfss://container@account.dfs.core.windows.net/delta/orders/');
```

### Delta write-back (via deltalake Python library)
DuckDB's delta extension is currently **read-only**. Use the Python `deltalake` library for writes:

```python
from deltalake.writer import write_deltalake
import duckdb

con = duckdb.connect("cache.duckdb")
df = con.execute("SELECT * FROM my_aggregated_table").fetchdf()
write_deltalake(
    "/data/delta/output/",
    df,
    mode="append",            # or "overwrite"
    partition_by=["year", "month"],
)
```

---

## Parquet

### Local files
```sql
-- Single file
SELECT * FROM read_parquet('/data/orders.parquet');

-- Glob pattern
SELECT * FROM read_parquet('/data/orders/*.parquet');

-- Hive-partitioned directory (auto-detects partition columns)
SELECT * FROM read_parquet('/data/orders/**/*.parquet', hive_partitioning=true);
```

### S3 / GCS / ADLS
```sql
INSTALL httpfs; LOAD httpfs;
SET s3_region='us-east-1';
-- Credentials via env or SET commands (see httpfs section below)
SELECT * FROM read_parquet('s3://bucket/prefix/*.parquet', hive_partitioning=true);
SELECT * FROM read_parquet('gs://bucket/prefix/*.parquet');   -- GCS
SELECT * FROM read_parquet('az://container/prefix/*.parquet'); -- ADLS
```

### Schema inspection before full scan
```sql
SELECT * FROM parquet_schema('/data/orders.parquet');   -- column names + types
SELECT * FROM parquet_metadata('/data/orders.parquet'); -- row groups, compression, statistics
```

---

## CSV / JSON

```sql
-- CSV with auto-detect
SELECT * FROM read_csv('/data/export.csv', auto_detect=true);

-- CSV with explicit schema
SELECT * FROM read_csv('/data/export.csv',
  columns={'id': 'INTEGER', 'ts': 'TIMESTAMP', 'value': 'DOUBLE'},
  dateformat='%Y-%m-%d',
  delim=';',
  header=true
);

-- NDJSON (newline-delimited JSON)
SELECT * FROM read_json('/data/events.ndjson', auto_detect=true);

-- Nested JSON
SELECT json_extract_string(body, '$.customer.name') AS customer
FROM read_json('/data/orders.json');
```

---

## SQLite

```sql
INSTALL sqlite; LOAD sqlite;
ATTACH '/data/local.db' AS sqlite (TYPE sqlite);
SELECT * FROM sqlite.main.products;
```

---

## MySQL / MariaDB

```sql
INSTALL mysql; LOAD mysql;
ATTACH 'host=mysql.example.com port=3306 database=sales user=reader password=secret'
  AS mysql_db (TYPE mysql);
SELECT * FROM mysql_db.sales.orders LIMIT 1000;
```

---

## httpfs — S3 / GCS / Azure Credential Patterns

### AWS credentials (multiple methods — pick one)

```sql
-- Method 1: SET commands (session-scoped, no disk storage)
SET s3_region='us-east-1';
SET s3_access_key_id='AKIA...';
SET s3_secret_access_key='secret';

-- Method 2: Instance profile / IAM role (no keys needed)
SET s3_region='us-east-1';
-- DuckDB will use the EC2/ECS metadata service automatically when no keys are set.

-- Method 3: CREATE SECRET (persistent in the DuckDB file, encrypted)
CREATE OR REPLACE SECRET aws_creds (
  TYPE s3,
  REGION 'us-east-1',
  KEY_ID 'AKIA...',
  SECRET 'secret'
);
```

### GCS

```sql
CREATE SECRET gcs_creds (
  TYPE gcs,
  KEY_ID 'service-account@project.iam.gserviceaccount.com',
  SECRET '<base64-encoded-private-key>'
);
SELECT * FROM read_parquet('gs://my-bucket/data/*.parquet');
```

### Azure ADLS / Blob Storage

```sql
CREATE SECRET azure_creds (
  TYPE azure,
  CONNECTION_STRING 'DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net'
);
SELECT * FROM read_parquet('az://mycontainer/data/*.parquet');
```

---

## Hive Metastore (via Iceberg or direct Parquet scan)

There is no native Hive Metastore extension in DuckDB as of v1.1. Options:

1. **Export table location from Hive**, then use `read_parquet` with the HDFS/S3 path.
2. **Use Iceberg REST proxy** in front of HMS (e.g., Project Nessie, Apache Polaris).
3. **Use Spark** to materialise to Parquet, then DuckDB reads the Parquet output.

---

## ATTACH management

```sql
-- List attached databases
SELECT * FROM duckdb_databases();

-- Detach when done (releases connection)
DETACH pg;
DETACH iceberg_rest;

-- Re-use a named secret across sessions
SELECT * FROM duckdb_secrets();
DROP SECRET aws_creds;
```
