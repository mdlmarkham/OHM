# Vectors, Graphs & Hierarchies in DuckDB

Reference for VSS/HNSW vector search, graph algorithms (DuckDB 1.3+
`USING KEY` CTEs), and LTREE-style hierarchy patterns.

---

## Part 1 — Vector Similarity Search (VSS / HNSW)

### Extension & index tuning

```sql
INSTALL vss; LOAD vss;

-- Enable persistent index for disk-backed databases
SET GLOBAL hnsw_enable_experimental_persistence = true;

-- HNSW build parameters (set before CREATE INDEX)
SET hnsw_ef_construction = 200;   -- default 128; higher = better recall, slower build
SET hnsw_m = 16;                  -- default 16; max connections per node

-- Build AFTER bulk load (much faster than insert-then-index)
CREATE INDEX emb_hnsw ON embeddings USING HNSW (embedding)
  WITH (metric = 'cosine');       -- or 'l2sq', 'ip'
```

### Distance functions

| DuckDB function | Math | Use case |
|---|---|---|
| `array_cosine_distance(a, b)` | `1 - cosine_similarity` | Normalized text embeddings |
| `array_distance(a, b)` | Euclidean (L2) | Image/audio embeddings |
| `array_negative_inner_product(a, b)` | `-dot_product(a,b)` | Inner-product ANN |

All three are accelerated by HNSW index when used in a top-k ORDER BY pattern:
```sql
SELECT id, array_cosine_distance(embedding, $1::FLOAT[384]) AS dist
FROM embeddings ORDER BY dist LIMIT 10;
```

### Hybrid search (vector + BM25 full-text)

```sql
INSTALL fts; LOAD fts;
PRAGMA create_fts_index('embeddings', 'id', 'content');

-- Reciprocal Rank Fusion (RRF) — blends semantic and lexical relevance
WITH vec_ranked AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY array_cosine_distance(embedding, $1::FLOAT[384])) AS rank_v
  FROM embeddings LIMIT 100
),
fts_ranked AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY fts_main_embeddings.match_bm25(id, $2) DESC NULLS LAST) AS rank_f
  FROM embeddings
  WHERE fts_main_embeddings.match_bm25(id, $2) IS NOT NULL LIMIT 100
)
SELECT COALESCE(v.id, f.id) AS id,
       0.7/NULLIF(v.rank_v,0) + 0.3/NULLIF(f.rank_f,0) AS rrf_score
FROM vec_ranked v FULL OUTER JOIN fts_ranked f USING (id)
ORDER BY rrf_score DESC LIMIT 10;
```

### Filtered vector search pattern

```sql
-- WRONG: WHERE before ORDER BY — HNSW index cannot fire
SELECT * FROM embeddings WHERE category = 'maintenance'
ORDER BY array_cosine_distance(embedding, $1::FLOAT[384]) LIMIT 5;

-- CORRECT: over-fetch in inner query, filter in outer
WITH candidates AS (
  SELECT *, array_cosine_distance(embedding, $1::FLOAT[384]) AS dist
  FROM embeddings ORDER BY dist LIMIT 100   -- index fires here
)
SELECT * FROM candidates
WHERE json_extract_string(metadata, '$.category') = 'maintenance'
ORDER BY dist LIMIT 5;
```

### RAG pipeline with DuckLake storage

```python
from scripts.duckdb_helper import DuckDBSession
from scripts.vector_helper import VectorStore
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2")

# Ingest documents into DuckLake, embeddings into VSS store
with DuckDBSession("cache.duckdb") as db:
    db.attach_ducklake(catalog="metadata.ducklake", data_path="lake/", alias="lake")
    docs_df = db.query("SELECT id, content FROM lake.documents WHERE indexed_at IS NULL")

with VectorStore("rag.duckdb", dim=384) as vs:
    rows = [{"id": row.id,
             "embedding": model.encode(row.content, normalize_embeddings=True).tolist(),
             "content": row.content}
            for _, row in docs_df.iterrows()]
    vs.upsert_batch(rows)
    vs.build_index()   # call AFTER bulk load

    # Query
    q_emb = model.encode("pump seal maintenance", normalize_embeddings=True)
    results = vs.search(q_emb, top_k=5)
    context = "\n".join(results["content"].tolist())
    # Feed context into LLM prompt
```

### Embedding dimensions guide

| Model | Dimension | Use case |
|---|---|---|
| `all-MiniLM-L6-v2` | 384 | Fast; good for English semantic search |
| `all-mpnet-base-v2` | 768 | Higher quality; balanced speed |
| OpenAI `text-embedding-3-small` | 1536 | Cloud API; strong quality |
| OpenAI `text-embedding-3-large` | 3072 | Best quality; slower and costlier |
| `nomic-embed-text-v1.5` | 768 | Open source; strong |

---

## Part 2 — Graph Data Structures (DuckDB 1.3+ USING KEY)

### Schema best practices

```sql
-- Nodes
CREATE TABLE nodes (
  id      BIGINT PRIMARY KEY,
  label   VARCHAR,
  props   JSON       -- arbitrary properties
);

-- Directed weighted edges
CREATE TABLE edges (
  src    BIGINT REFERENCES nodes(id),
  dst    BIGINT REFERENCES nodes(id),
  weight DOUBLE DEFAULT 1.0,
  label  VARCHAR
);

-- Indexes for forward and backward traversal
CREATE INDEX edges_src ON edges(src);
CREATE INDEX edges_dst ON edges(dst);
```

### Shortest path (Dijkstra, USING KEY)

```sql
-- DuckDB 1.3+: USING KEY makes the union table a dictionary keyed by node
-- → overwrites stale (higher-cost) paths instead of accumulating them
-- → dramatically smaller union table vs. vanilla UNION ALL on dense graphs

WITH RECURSIVE dijkstra(node, dist, path) USING KEY (node) AS (
    SELECT 1::BIGINT, 0.0::DOUBLE, [1::BIGINT]   -- start node
  UNION
    SELECT e.dst,
           d.dist + e.weight,
           list_append(d.path, e.dst)
    FROM dijkstra d
    JOIN edges e ON e.src = d.node
    LEFT JOIN dijkstra cur ON cur.node = e.dst
    WHERE d.dist + e.weight < COALESCE(cur.dist, 1e18)
)
SELECT node, dist, path FROM dijkstra ORDER BY dist;
```

### BFS (level-by-level exploration)

```sql
WITH RECURSIVE bfs(node, depth) USING KEY (node) AS (
    SELECT 1::BIGINT, 0
  UNION
    SELECT e.dst, b.depth + 1
    FROM bfs b
    JOIN edges e ON e.src = b.node
    WHERE b.depth < 10   -- hard depth limit prevents infinite loops on cycles
)
SELECT n.id, n.label, b.depth
FROM bfs b JOIN nodes n ON n.id = b.node
ORDER BY b.depth, b.node;
```

### PageRank (iterative, USING KEY)

```sql
WITH RECURSIVE
  out_degree AS (SELECT src, COUNT(*) AS deg FROM edges GROUP BY src),
  pagerank(node, pr) USING KEY (node) AS (
    -- Initialize
    SELECT id, 1.0 / (SELECT COUNT(*) FROM nodes) FROM nodes
  UNION
    -- Update: damping factor 0.85
    SELECT e.dst,
           0.15 / (SELECT COUNT(*) FROM nodes)
           + 0.85 * SUM(p.pr / NULLIF(od.deg, 1))
    FROM pagerank p
    JOIN edges e ON e.src = p.node
    JOIN out_degree od ON od.src = p.node
    GROUP BY e.dst
)
SELECT node, ROUND(pr, 6) AS pagerank
FROM pagerank ORDER BY pagerank DESC LIMIT 20;
```

### Connected components (Union-Find style)

```sql
-- Undirected: add both directions in edge table (or use UNION in query)
WITH RECURSIVE cc(node, component) USING KEY (node) AS (
    SELECT id, id FROM nodes
  UNION
    SELECT e.dst, LEAST(c.component,
      (SELECT component FROM cc WHERE node = e.src))
    FROM cc c
    JOIN edges e ON e.src = c.node OR e.dst = c.node
    WHERE LEAST(c.component,
      (SELECT component FROM cc WHERE node = e.dst)) < c.component
)
SELECT component, COUNT(*) AS size, LIST(node ORDER BY node) AS members
FROM cc GROUP BY component ORDER BY size DESC;
```

### Cycle detection

```sql
-- Find all cycles reachable from node 1 (path contains the revisited node)
WITH RECURSIVE paths(node, path, has_cycle) AS (
    SELECT 1::BIGINT, [1::BIGINT], false
  UNION ALL
    SELECT e.dst,
           list_append(p.path, e.dst),
           list_contains(p.path, e.dst)
    FROM paths p
    JOIN edges e ON e.src = p.node
    WHERE NOT p.has_cycle AND list_count(p.path) < 20
)
SELECT path FROM paths WHERE has_cycle;
```

---

## Part 3 — Hierarchies (LTREE-style patterns)

### A — Materialized Path (best for large read-heavy trees)

LTREE equivalent in DuckDB using `VARCHAR` with dot-separated labels.

```sql
CREATE TABLE categories (
  id      INTEGER PRIMARY KEY,
  name    VARCHAR NOT NULL,
  path    VARCHAR NOT NULL UNIQUE,   -- 'root.industrial.motors.bearings'
  depth   INTEGER GENERATED ALWAYS AS (len(string_split(path, '.')) - 1) VIRTUAL
);
CREATE INDEX cat_path_prefix ON categories(path);

-- All descendants of a path
SELECT * FROM categories WHERE path LIKE 'root.industrial.%';

-- Immediate children only
SELECT * FROM categories
WHERE path LIKE 'root.industrial.%'
  AND array_length(string_split(path, '.'))
      = array_length(string_split('root.industrial', '.')) + 1;

-- Full ancestor chain of a leaf (split path → rows)
SELECT unnest(string_split(path, '.')) AS label,
       generate_subscripts(string_split(path, '.'), 1) - 1 AS depth
FROM categories WHERE id = 99;

-- Subtree aggregate (sum all values under a path)
SELECT SUM(value) FROM fact_table f
JOIN categories c ON c.id = f.category_id
WHERE c.path LIKE 'root.industrial.%';
```

**Maintenance:**
```sql
-- Move subtree: update all paths under the moved node
UPDATE categories
SET path = 'root.operations' || SUBSTR(path, LENGTH('root.industrial') + 1)
WHERE path LIKE 'root.industrial.%';

-- Insert child (auto-compute path)
INSERT INTO categories (id, name, path)
SELECT 101, 'Seals',
       (SELECT path FROM categories WHERE id = 55) || '.Seals';
```

### B — Adjacency List + Recursive CTE (best for write-heavy trees)

```sql
CREATE TABLE tree (
  id        INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES tree(id),
  name      VARCHAR,
  value     DOUBLE DEFAULT 0
);
CREATE INDEX tree_parent ON tree(parent_id);

-- Subtree with depth, full path string, accumulated value
WITH RECURSIVE subtree(id, parent_id, name, value, depth, path_str) AS (
    SELECT id, parent_id, name, value, 0, name
    FROM tree WHERE id = ?               -- root of subtree
  UNION ALL
    SELECT c.id, c.parent_id, c.name, c.value, s.depth + 1,
           s.path_str || '.' || c.name
    FROM tree c JOIN subtree s ON c.parent_id = s.id
)
SELECT *, SUM(value) OVER (PARTITION BY 1) AS total_value FROM subtree ORDER BY path_str;

-- Bill of Materials (BOM) rollup: sum value through all levels
WITH RECURSIVE bom(node, total_value) USING KEY (node) AS (
    SELECT id, value FROM tree WHERE parent_id IS NULL  -- roots
  UNION
    SELECT c.id, c.value + b.total_value
    FROM tree c JOIN bom b ON b.node = c.parent_id
)
SELECT t.name, b.total_value FROM bom b JOIN tree t ON t.id = b.node ORDER BY b.total_value DESC;
```

### C — Closure Table (best for arbitrary ancestor/descendant queries)

```sql
CREATE TABLE tree_closure (
  ancestor   INTEGER NOT NULL REFERENCES tree(id),
  descendant INTEGER NOT NULL REFERENCES tree(id),
  depth      INTEGER NOT NULL,
  PRIMARY KEY (ancestor, descendant)
);
CREATE INDEX closure_desc ON tree_closure(descendant);

-- All ancestors of node N (ordered root → N)
SELECT t.* FROM tree t
JOIN tree_closure tc ON tc.ancestor = t.id
WHERE tc.descendant = ?
ORDER BY tc.depth DESC;

-- All descendants of node N
SELECT t.*, tc.depth FROM tree t
JOIN tree_closure tc ON tc.descendant = t.id
WHERE tc.ancestor = ?
ORDER BY tc.depth;

-- Is A an ancestor of B?
SELECT EXISTS (
  SELECT 1 FROM tree_closure WHERE ancestor = :A AND descendant = :B
) AS is_ancestor;
```

### D — ISA-95 Equipment Hierarchy (industrial use case)

```sql
-- ISA-95 hierarchy: Enterprise → Site → Area → Work Centre → Work Unit
CREATE TABLE equipment (
  id          BIGINT PRIMARY KEY,
  parent_id   BIGINT REFERENCES equipment(id),
  level       VARCHAR CHECK (level IN ('enterprise','site','area','work_centre','work_unit')),
  tag         VARCHAR UNIQUE,    -- e.g. 'SITE1.AREA2.WC3.PUMP-042'
  description VARCHAR
);

-- All equipment under a site with KPI rollup
WITH RECURSIVE eq_tree AS (
    SELECT id, tag, level, 0 AS depth FROM equipment WHERE tag = 'SITE1'
  UNION ALL
    SELECT e.id, e.tag, e.level, t.depth + 1
    FROM equipment e JOIN eq_tree t ON e.parent_id = t.id
)
SELECT eq.level, eq.tag,
       AVG(s.value) AS avg_oee,
       COUNT(DISTINCT s.ts::DATE) AS days_with_data
FROM eq_tree eq
JOIN sensor_cache s ON s.equipment_id = eq.id
WHERE s.tag = 'OEE'
GROUP BY eq.level, eq.tag
ORDER BY eq.level, eq.tag;
```

---

## Performance Notes

| Pattern | Read | Write | Depth limit | Notes |
|---|---|---|---|---|
| Materialized path | ✅ Fast (LIKE prefix) | ⚠️ Slow (update subtree) | Unlimited | Best for large stable trees |
| Adjacency + recursive CTE | ⚠️ O(depth) joins | ✅ Fast (single row) | ~50 safe | `WITH RECURSIVE` + depth guard |
| Closure table | ✅ O(1) ancestor check | ⚠️ O(N) insert | Unlimited | Best for many ancestor queries |
| USING KEY (graph) | ✅ Fast (upsert semantics) | N/A | Set `WHERE depth < N` | Requires DuckDB 1.3+ |
