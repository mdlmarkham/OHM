# DuckDB Advanced Analysis Patterns

Reference for sophisticated analytical queries. Read this file when the
SKILL.md examples are insufficient for your analytical use case.

---

## Time-Series Patterns

### Gap detection — find missing intervals
```sql
-- Detect gaps > 10 minutes in sensor readings
WITH ordered AS (
  SELECT ts, value, LEAD(ts) OVER (ORDER BY ts) AS next_ts
  FROM sensor_cache
)
SELECT ts AS gap_start, next_ts AS gap_end,
       (next_ts - ts) AS gap_duration
FROM ordered
WHERE (next_ts - ts) > INTERVAL '10 minutes'
ORDER BY gap_duration DESC;
```

### ASOF join — match to nearest prior event
```sql
-- Match each reading to the recipe step active at that time
SELECT s.ts, s.value, r.step_name, r.target_temp
FROM sensor_readings s
ASOF JOIN recipe_steps r
  ON (s.machine_id = r.machine_id AND s.ts >= r.start_ts);
```

### Resampling — downsample to uniform intervals
```sql
-- 5-minute average (time_bucket requires DuckDB 1.1+ or use date_trunc)
SELECT
  time_bucket(INTERVAL '5 minutes', ts) AS bucket,
  tag,
  AVG(value)   AS avg_val,
  MIN(value)   AS min_val,
  MAX(value)   AS max_val
FROM sensor_cache
GROUP BY 1, 2
ORDER BY 1, 2;
```

### Last-observation-carried-forward (LOCF)
```sql
-- Fill NULLs with the last non-null value per tag
SELECT ts, tag,
       LAST_VALUE(value IGNORE NULLS) OVER (
         PARTITION BY tag ORDER BY ts
         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ) AS value_filled
FROM sensor_cache;
```

### Sessionisation — group events into sessions
```sql
-- New session starts when gap > 30 minutes
WITH sessions AS (
  SELECT *,
    SUM(CASE WHEN ts - LAG(ts) OVER (PARTITION BY user_id ORDER BY ts) > INTERVAL '30 minutes'
             THEN 1 ELSE 0 END)
    OVER (PARTITION BY user_id ORDER BY ts) AS session_id
  FROM events
)
SELECT user_id, session_id,
       MIN(ts) AS session_start,
       MAX(ts) AS session_end,
       COUNT(*) AS event_count
FROM sessions
GROUP BY 1, 2;
```

---

## Window Function Patterns

### Rolling statistics
```sql
SELECT ts, value,
       AVG(value)    OVER w AS rolling_avg_7d,
       STDDEV(value) OVER w AS rolling_std_7d,
       (value - AVG(value) OVER w) / NULLIF(STDDEV(value) OVER w, 0) AS z_score
FROM sensor_cache
WINDOW w AS (PARTITION BY tag ORDER BY ts RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW);
```

### Percentile rank per group
```sql
SELECT product_id, revenue,
       PERCENT_RANK()   OVER (PARTITION BY category ORDER BY revenue) AS pct_rank,
       NTILE(4)         OVER (PARTITION BY category ORDER BY revenue) AS quartile
FROM sales;
```

### Year-over-year comparison
```sql
SELECT date_trunc('month', ts) AS month,
       SUM(value) AS total,
       LAG(SUM(value), 12) OVER (ORDER BY date_trunc('month', ts)) AS yoy_prev,
       ROUND(100.0 * (SUM(value) - LAG(SUM(value), 12) OVER (ORDER BY date_trunc('month', ts)))
             / NULLIF(LAG(SUM(value), 12) OVER (ORDER BY date_trunc('month', ts)), 0), 2) AS yoy_pct
FROM events
GROUP BY 1
ORDER BY 1;
```

---

## Pivot / Unpivot

```sql
-- Pivot: tag values to columns (DuckDB PIVOT extension)
PIVOT sensor_cache
ON tag_name
USING AVG(value)
GROUP BY date_trunc('hour', ts)
ORDER BY 1;

-- Dynamic pivot (column list from data)
-- Step 1: find distinct tags
CREATE TEMP TABLE tags AS SELECT DISTINCT tag_name FROM sensor_cache;
-- Step 2: build SQL string and execute via macro or Python

-- Unpivot: columns to rows
UNPIVOT wide_table
ON (col_a, col_b, col_c)
INTO NAME metric_name VALUE metric_value;
```

---

## Fuzzy Matching / String Similarity

```sql
-- Levenshtein distance (built-in)
SELECT a.name, b.name, levenshtein(a.name, b.name) AS dist
FROM customers a
CROSS JOIN customers b
WHERE levenshtein(a.name, b.name) BETWEEN 1 AND 3;

-- Jaro-Winkler similarity
SELECT jaro_winkler_similarity('kitten', 'sitting');

-- Soundex / phonetic match
SELECT soundex('Robert'), soundex('Rupert');
```

---

## Geospatial (spatial extension)

```sql
INSTALL spatial; LOAD spatial;

-- Distance between two points (returns metres)
SELECT ST_Distance(
  ST_Point(lon1, lat1)::GEOGRAPHY,
  ST_Point(lon2, lat2)::GEOGRAPHY
) AS distance_m
FROM locations;

-- Points within 10km of a reference
SELECT id, name
FROM locations
WHERE ST_DWithin(
  ST_Point(lon, lat)::GEOGRAPHY,
  ST_Point(-77.0369, 38.9072)::GEOGRAPHY,
  10000  -- metres
);

-- Hilbert curve encoding for spatial locality (used for columnar sorting)
SELECT hilbert_encode([lon::FLOAT, lat::FLOAT]) AS h_code
FROM locations;
```

---

## Sampling & Statistical Analysis

```sql
-- Reservoir sample — reproducible 1% random sample
SELECT * FROM sensor_cache USING SAMPLE 1%;

-- Seeded sample for reproducible results
SELECT * FROM sensor_cache USING SAMPLE reservoir(1000 ROWS) REPEATABLE(42);

-- Approximate count distinct (HyperLogLog)
SELECT APPROX_COUNT_DISTINCT(customer_id) FROM orders;

-- Histogram with equal-width buckets
SELECT histogram(value, 20) FROM sensor_cache;

-- Approximate quantiles (fast on large datasets)
SELECT APPROX_QUANTILE(value, [0.25, 0.5, 0.75, 0.95, 0.99]) AS quantiles
FROM sensor_cache;
```

---

## JSON Extraction

```sql
-- Extract nested fields
SELECT
  json_extract_string(payload, '$.customer.name')   AS customer,
  json_extract(payload, '$.items')                  AS items_json,
  json_array_length(json_extract(payload, '$.items')) AS item_count
FROM orders;

-- Unnest JSON array to rows
SELECT unnest(from_json(items_json, '["VARCHAR"]')) AS item
FROM orders;

-- Read NDJSON and flatten
SELECT * FROM read_json('/data/events.ndjson',
  columns={
    'event_id': 'VARCHAR',
    'ts': 'TIMESTAMP',
    'payload': 'JSON'
  }
);
```

---

## Performance Tuning Checklist

| Scenario | Recommendation |
|---|---|
| Slow Postgres scan | Add WHERE clause; check that DuckDB pushes it via `EXPLAIN` |
| Large join OOM | `PRAGMA memory_limit='16GB'; PRAGMA temp_directory='/fast-ssd/tmp';` |
| Repeated full scans | Materialise to local DuckDB table: `CREATE TABLE t AS SELECT ...` |
| Slow GROUP BY on strings | Use integer surrogate keys; or `PRAGMA threads=16` |
| Writing many small Parquet files | Use `PARTITION_BY` with limited cardinality; avoid >1K partitions |
| Reading many small Parquet files | Use glob + `union_by_name=true`; consider consolidating first |

### EXPLAIN output
```sql
-- Physical query plan
EXPLAIN SELECT * FROM pg.public.orders WHERE customer_id = 42;

-- Analyse + profiling (executes the query)
EXPLAIN ANALYSE SELECT * FROM my_table GROUP BY tag;
```

### Parallelism
```sql
PRAGMA threads = 16;              -- match to physical cores
PRAGMA worker_threads = 12;       -- background IO threads
PRAGMA enable_object_cache = true; -- cache Parquet footer metadata
```

---

## DuckDB 1.3+ — USING KEY Recursive CTEs (Graph & Convergence)

Standard `WITH RECURSIVE` accumulates every intermediate result in a union
table (append-only). On dense graphs this causes exponential blowup.
`USING KEY` (DuckDB 1.3+) treats the union table as a keyed dictionary —
existing rows are updated in-place rather than duplicated.

**When to use USING KEY:**
- Graph algorithms where a node may be reached via multiple paths (Dijkstra, BFS)
- Fixed-point / convergence computations (PageRank, connected components)
- Any recursion where you want "keep the best seen so far" semantics

```sql
-- Pattern: USING KEY (key_col) — payload cols may be updated in later iterations
WITH RECURSIVE result(node, dist, path) USING KEY (node) AS (
    -- Base case (non-recursive)
    SELECT 1 AS node, 0.0 AS dist, [1] AS path
  UNION
    -- Recursive step — UNION (not UNION ALL) with key semantics
    SELECT e.dst,
           r.dist + e.weight,
           list_append(r.path, e.dst)
    FROM result r
    JOIN edges e ON e.src = r.node
    LEFT JOIN result cur ON cur.node = e.dst   -- access recurring (union) table
    WHERE r.dist + e.weight < COALESCE(cur.dist, 1e18)
)
SELECT * FROM result ORDER BY dist;
```

**USING KEY rules:**
- Only one `USING KEY (...)` clause per recursive CTE
- The key column(s) must uniquely identify each row
- Use `LEFT JOIN <cte_name> cur ON cur.key = ...` to read the current state of the recurring table
- Terminate with a `WHERE` guard (`depth < N`, convergence check) to prevent infinite loops
- Requires DuckDB 1.3+ — vanilla `WITH RECURSIVE` still works for simple tree traversals

---

## DuckDB 1.4/1.5 Performance Features

```sql
-- Cache Parquet footer metadata across queries (1.4+)
-- Dramatically reduces re-read overhead on repeated scans of the same files
PRAGMA enable_object_cache = true;

-- Multiple read-only attaches to the same .duckdb file (1.4+)
-- Previously only one process could attach; now N readers are allowed
ATTACH 'cache.duckdb' AS r1 (READ_ONLY);
ATTACH 'cache.duckdb' AS r2 (READ_ONLY);

-- CTE materialisation control (1.3+)
-- Force materialise (evaluate once, store result):
WITH expensive AS MATERIALIZED (SELECT ... FROM large_table)
SELECT * FROM expensive e1 JOIN expensive e2 ...;

-- Force inline (no materialisation, re-evaluate at each reference):
WITH simple AS NOT MATERIALIZED (SELECT 1+1 AS x)
SELECT * FROM simple;

-- Column pruning for MATERIALIZED CTEs (1.5+)
-- DuckDB now only reads columns actually referenced after the CTE
WITH full_row AS MATERIALIZED (SELECT * FROM wide_table)
SELECT id, name FROM full_row;  -- only id, name columns are scanned

-- read_duckdb() — glob and read DuckDB files like Parquet (1.4+)
SELECT * FROM read_duckdb('/archive/*.duckdb', table_name='events');
```

---

## Query Efficiency Checklist

| Problem | Fix |
|---|---|
| Slow Postgres scan | Add WHERE + LIMIT; verify DuckDB pushes them via `EXPLAIN` |
| Dense graph recursion | Replace `WITH RECURSIVE` with `USING KEY` (DuckDB 1.3+) |
| Repeated Parquet re-reads | `PRAGMA enable_object_cache=true;` |
| Large Parquet header round-trips | Consolidate small files; use `ducklake_compact()` or DuckLake |
| Slow GROUP BY on strings | Use integer surrogate keys; increase `PRAGMA threads` |
| OOM on large join | `PRAGMA memory_limit='16GB'; PRAGMA temp_directory='/fast-ssd/tmp';` |
| HNSW index not firing | Use top-k pattern: `ORDER BY dist LIMIT k` in inner CTE |
| Many small writes to DuckLake | Batch inserts; use `ducklake_compact()` after bulk load |

