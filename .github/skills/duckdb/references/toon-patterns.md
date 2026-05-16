# TOON Patterns for DuckDB Result Sets

TOON (Token-Oriented Object Notation v3.0) serialises DuckDB query results for
LLM consumption with ~40% fewer tokens than JSON for uniform arrays.

Spec: https://github.com/toon-format/toon  
Python helper: `scripts/duckdb_helper.py` → `df_to_toon()` / `DuckDBSession.to_toon()`

---

## Format Quick Reference

### Tabular array (uniform columns — most DuckDB results)
```
tablename[N]{col1,col2,col3}:
  val1,val2,val3
  val4,val5,val6
```
- `N` = declared row count (models use this for validation)
- `{col1,...}` = field header, declared once
- One data row per line, comma-separated
- Quote values containing commas, newlines, or double-quotes: `"val,with,comma"`

### Single-row result (scalar/metadata)
```
tablename:
  col1: val1
  col2: val2
```

### Nested / heterogeneous data (use JSON compact)
```json
{"key": {"nested": "value"}, "arr": [1,2,3]}
```
TOON does not improve token cost for deeply nested non-uniform structures.

---

## Decision Matrix

| Result shape | Format | Reason |
|---|---|---|
| Uniform array, ≥2 rows | TOON tabular | 40–60% token savings |
| Single row | TOON key-value | Slightly better than JSON |
| Scalar (COUNT, SUM) | Inline text | No serialisation overhead |
| Nested / semi-uniform | JSON compact | TOON overhead may exceed savings |
| >500 rows | Paginate or summarise first | LLM context window |
| Schema / column list | TOON key-value list | Structured, compact |

---

## Pagination for Large Results

When a query returns more rows than the LLM context budget allows, paginate:

```python
from scripts.duckdb_helper import DuckDBSession, df_to_toon

PAGE_SIZE = 200

with DuckDBSession("cache.duckdb") as db:
    total = db.scalar("SELECT COUNT(*) FROM sensor_cache")
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    for page in range(pages):
        df = db.query(f"""
          SELECT * FROM sensor_cache
          ORDER BY ts
          LIMIT {PAGE_SIZE} OFFSET {page * PAGE_SIZE}
        """)
        toon = df_to_toon(df, f"sensor_cache_page{page+1}of{pages}")
        # yield toon to LLM, wait for acknowledgement, then send next page
        yield toon
```

Include a preamble to the LLM:
```
Data follows in {pages} pages of {PAGE_SIZE} rows each.
Confirm receipt of each page before I send the next.
```

---

## Schema Introspection in TOON

Emit table schemas as compact TOON for context-setting:

```python
def schema_to_toon(db: DuckDBSession, table: str) -> str:
    df = db.query(f"DESCRIBE {table}")
    return df_to_toon(df[['column_name', 'column_type', 'null']], f"{table}_schema")
```

Output:
```
orders_schema[5]{column_name,column_type,null}:
  id,INTEGER,NO
  customer_id,INTEGER,YES
  total,DOUBLE,YES
  created_at,TIMESTAMP WITH TIME ZONE,NO
  status,VARCHAR,YES
```

---

## Multi-Table Context in One Prompt

Combine schemas + sample data in a single TOON block:

```python
prompt_parts = []

# Schema context
for tbl in ["orders", "customers", "products"]:
    prompt_parts.append(f"## Schema: {tbl}")
    prompt_parts.append("```toon")
    prompt_parts.append(db.query_toon(f"DESCRIBE {tbl}", f"{tbl}_schema"))
    prompt_parts.append("```")

# Sample data
prompt_parts.append("## Sample Data: orders (last 5)")
prompt_parts.append("```toon")
prompt_parts.append(db.query_toon(
    "SELECT * FROM orders ORDER BY created_at DESC LIMIT 5",
    "orders_sample"
))
prompt_parts.append("```")

full_prompt = "\n".join(prompt_parts)
```

---

## TOON Formatting Rules (Python Implementation)

The `df_to_toon` function in `duckdb_helper.py` implements these rules:

| Rule | Detail |
|---|---|
| `null` Python/pandas `None` | Rendered as literal `null` |
| Booleans | `true` / `false` (lowercase) |
| Floats | Use pandas default repr; no forced rounding |
| Timestamps | ISO 8601 string from pandas |
| String with comma | Wrapped in `"..."` |
| String with `"` | Inner quotes doubled: `""` |
| Row count | Declared in `[N]` header |
| Truncation | Appended comment: `# WARNING: result truncated to N rows` |

---

## Receiving TOON Output from an LLM

When asking an LLM to generate TOON (e.g., generate a SQL result schema):

```
Respond ONLY in TOON format. Use this structure for arrays:
  name[N]{col1,col2,col3}:
    val1,val2,val3

Wrap in ```toon ... ``` code block.
Do not include any prose outside the code block.
```

Parse TOON responses back to Python dicts/DataFrames using the
`@toon-format/toon` TypeScript SDK, or implement a minimal parser:

```python
def parse_toon_table(toon_str: str) -> list[dict]:
    """Minimal TOON tabular parser (uniform arrays only)."""
    import csv, io
    lines = [l for l in toon_str.strip().splitlines() if not l.strip().startswith("#")]
    # Header line: name[N]{col1,col2,...}:
    header_line = lines[0]
    fields_part = header_line[header_line.index("{")+1 : header_line.index("}")]
    cols = [c.strip() for c in fields_part.split(",")]
    rows = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        reader = csv.reader(io.StringIO(line))
        values = next(reader)
        rows.append(dict(zip(cols, values)))
    return rows
```
