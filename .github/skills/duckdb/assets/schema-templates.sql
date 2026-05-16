-- DuckDB Schema Templates v2.0
-- Sections: time-series cache, vector/embeddings, graph, hierarchy,
--           DuckLake setup, watermark registry, cache health views.
-- All templates use IF NOT EXISTS for idempotent execution.

-- =============================================================================
-- 1. TIME-SERIES CACHE  (OPC-UA / MQTT narrow schema)
-- =============================================================================
CREATE TABLE IF NOT EXISTS ts_cache (
    ts          TIMESTAMPTZ NOT NULL,
    tag         VARCHAR     NOT NULL,
    value       DOUBLE,
    quality     SMALLINT    DEFAULT 192,   -- 192 = OPC-UA Good
    source      VARCHAR,                   -- 'pg', 'iceberg', 'ducklake', etc.
    ingested_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ts_cache_tag_ts ON ts_cache (tag, ts);

-- Pre-aggregated hourly rollup
CREATE TABLE IF NOT EXISTS ts_hourly (
    hour        TIMESTAMPTZ NOT NULL,
    tag         VARCHAR     NOT NULL,
    avg_val     DOUBLE,
    min_val     DOUBLE,
    max_val     DOUBLE,
    std_val     DOUBLE,
    count_rows  INTEGER,
    PRIMARY KEY (hour, tag)
);

-- =============================================================================
-- 2. VECTOR / EMBEDDING STORE  (RAG)
-- =============================================================================
-- Dimension guide:
--   384  -> all-MiniLM-L6-v2
--   768  -> all-mpnet-base-v2, nomic-embed-text-v1.5
--   1536 -> OpenAI text-embedding-3-small
--   3072 -> OpenAI text-embedding-3-large
-- Change 384 below to match your model.
CREATE TABLE IF NOT EXISTS embeddings (
    id          VARCHAR PRIMARY KEY,
    embedding   FLOAT[384],
    content     VARCHAR,
    source_doc  VARCHAR,
    chunk_idx   INTEGER,
    metadata    JSON,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Build HNSW AFTER bulk load:
--   INSTALL vss; LOAD vss;
--   SET GLOBAL hnsw_enable_experimental_persistence = true;  -- disk DBs only
--   CREATE INDEX emb_hnsw ON embeddings USING HNSW (embedding) WITH (metric='cosine');
--   Metrics: 'cosine' (normalized text), 'l2sq' (Euclidean), 'ip' (inner product)

-- FTS for hybrid search:
--   INSTALL fts; LOAD fts;
--   PRAGMA create_fts_index('embeddings', 'id', 'content');

-- =============================================================================
-- 3. GRAPH  (directed, weighted)
-- =============================================================================
CREATE TABLE IF NOT EXISTS nodes (
    id      BIGINT PRIMARY KEY,
    label   VARCHAR NOT NULL,
    type    VARCHAR,
    props   JSON
);
CREATE TABLE IF NOT EXISTS edges (
    src     BIGINT NOT NULL REFERENCES nodes(id),
    dst     BIGINT NOT NULL REFERENCES nodes(id),
    weight  DOUBLE DEFAULT 1.0,
    label   VARCHAR,
    props   JSON
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);

-- BFS example (DuckDB 1.3+ USING KEY):
-- WITH RECURSIVE bfs(node, depth) USING KEY (node) AS (
--     SELECT 1::BIGINT, 0
--   UNION
--     SELECT e.dst, b.depth + 1
--     FROM bfs b JOIN edges e ON e.src = b.node
--     WHERE b.depth < 10
-- )
-- SELECT n.label, b.depth FROM bfs b JOIN nodes n ON n.id = b.node ORDER BY b.depth;

-- =============================================================================
-- 4. HIERARCHY  (three patterns — choose one or combine)
-- =============================================================================

-- 4A. Adjacency list (simplest; best for writes)
CREATE TABLE IF NOT EXISTS tree (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES tree(id),
    name        VARCHAR NOT NULL,
    value       DOUBLE DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS tree_parent ON tree(parent_id);

-- 4B. Materialized path  (LTREE-equivalent; fastest prefix queries)
CREATE TABLE IF NOT EXISTS categories (
    id      INTEGER PRIMARY KEY,
    name    VARCHAR NOT NULL,
    path    VARCHAR NOT NULL UNIQUE    -- 'root.industrial.motors.bearings'
);
CREATE INDEX IF NOT EXISTS cat_path ON categories(path);
-- Descendants:   WHERE path LIKE 'root.industrial.%'
-- Ancestors:     unnest(string_split(path, '.'))

-- 4C. Closure table  (O(1) ancestor check)
CREATE TABLE IF NOT EXISTS tree_closure (
    ancestor    INTEGER NOT NULL REFERENCES tree(id),
    descendant  INTEGER NOT NULL REFERENCES tree(id),
    depth       INTEGER NOT NULL,
    PRIMARY KEY (ancestor, descendant)
);
CREATE INDEX IF NOT EXISTS closure_desc ON tree_closure(descendant);
-- Is A ancestor of B?   SELECT EXISTS(SELECT 1 FROM tree_closure WHERE ancestor=:A AND descendant=:B);

-- ISA-95 equipment hierarchy
CREATE TABLE IF NOT EXISTS equipment (
    id          BIGINT PRIMARY KEY,
    parent_id   BIGINT REFERENCES equipment(id),
    level       VARCHAR CHECK (level IN ('enterprise','site','area','work_centre','work_unit')),
    tag         VARCHAR UNIQUE,
    description VARCHAR,
    props       JSON
);
CREATE INDEX IF NOT EXISTS equipment_parent ON equipment(parent_id);
CREATE INDEX IF NOT EXISTS equipment_tag    ON equipment(tag);

-- =============================================================================
-- 5. DUCKLAKE SETUP  (run in DuckDB shell with ducklake extension)
-- =============================================================================
-- INSTALL ducklake; LOAD ducklake;
-- INSTALL httpfs;   LOAD httpfs;
-- INSTALL aws;      LOAD aws;      -- auto-reads AWS_ env vars
--
-- Local catalog + S3 data:
--   ATTACH 'ducklake:metadata.ducklake' AS lake (DATA_PATH 's3://my-bucket/lake/');
--
-- Postgres catalog + S3 (multi-writer):
--   ATTACH 'ducklake:postgres:dbname=ducklake_catalog host=pg.example.com'
--     AS lake (DATA_PATH 's3://my-bucket/lake/');
--
-- Create tables in DuckLake:
--   USE lake;
--   CREATE TABLE IF NOT EXISTS lake.events (
--       id BIGINT PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), payload JSON
--   ) PARTITION BY (DATE_TRUNC('month', ts));
--
-- Time travel:   SELECT * FROM lake.events AT (VERSION => 3);
-- Compact:       CALL lake.ducklake_compact('events');
-- Snapshots:     SELECT * FROM lake.snapshots();

-- =============================================================================
-- 6. WATERMARK REGISTRY  (incremental refresh tracking)
-- =============================================================================
CREATE TABLE IF NOT EXISTS refresh_watermarks (
    cache_table   VARCHAR PRIMARY KEY,
    source_label  VARCHAR,
    watermark_col VARCHAR DEFAULT 'ts',
    last_ts       TIMESTAMPTZ DEFAULT '1970-01-01'::TIMESTAMPTZ,
    last_run      TIMESTAMPTZ DEFAULT now(),
    rows_loaded   BIGINT DEFAULT 0
);
-- Update after refresh:
-- INSERT INTO refresh_watermarks VALUES ('ts_cache','pg.public.sensor_data','ts',?,now(),?)
--   ON CONFLICT (cache_table) DO UPDATE
--   SET last_ts=EXCLUDED.last_ts, last_run=EXCLUDED.last_run, rows_loaded=EXCLUDED.rows_loaded;

-- =============================================================================
-- 7. CACHE HEALTH VIEWS
-- =============================================================================
CREATE VIEW IF NOT EXISTS cache_health AS
SELECT t.table_name,
       t.estimated_size,
       t.column_count,
       w.last_ts,
       w.last_run,
       w.rows_loaded,
       (now() - w.last_run)::VARCHAR AS age,
       CASE WHEN (now() - w.last_run) > INTERVAL '1 hour' THEN 'STALE' ELSE 'OK' END AS status
FROM duckdb_tables() t
LEFT JOIN refresh_watermarks w ON w.cache_table = t.table_name
ORDER BY t.table_name;

CREATE VIEW IF NOT EXISTS vector_store_health AS
SELECT t.table_name,
       t.estimated_size,
       i.index_name,
       CASE WHEN i.index_name IS NOT NULL THEN 'HNSW indexed'
            ELSE 'brute-force scan (run build_hnsw_index())' END AS index_status
FROM duckdb_tables() t
LEFT JOIN duckdb_indexes() i ON i.table_name = t.table_name AND i.index_name LIKE '%hnsw%'
ORDER BY t.table_name;
