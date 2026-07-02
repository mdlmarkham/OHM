# OHM Performance Audit — Pure-Python Loops (OHM-lqpk.6)

**Date:** 2026-07-02
**Scope:** `src/ohm/graph/queries/__init__.py` (~13k LoC) and `src/ohm/graph/methods.py` (~4.8k LoC)
**Method:** ripgrep for the patterns listed in the issue, then targeted inspection of each candidate. The full candidate list is in the bottom section; this report only covers candidates that survived a quick review (i.e., looked like they could be hot in production).

## TL;DR

| Tier | Path | Status |
|------|------|--------|
| Tier 1 (already known) | `monte_carlo_impact` / `monte_carlo_cascade` | Already in AGENTS.md as Rust candidates |
| Tier 1.5 (this audit) | `hd_membership_search` | Investigated in OHM-lqpk.2 — SQL path is **1.5× SLOWER** for current scale, no action |
| **Tier 1 (new)** | `batch_create_nodes` / `batch_create_edges` | **N round-trips** for batch operations; single multi-row INSERT could be 10-100× faster for 1000+ node batches. **Recommended child issue.** |
| Tier 2 | `find_islands` | 12ms for 2000 nodes / 5000 edges — not a hot path concern at current scale |
| Tier 2 | `query_path` BFS | Uses SQL-per-level already, not pure-Python edge iteration |
| Tier 2 | `query_stale_edges` / `apply_confidence_decay` | Bounded by stale-edge count |
| No issue | `find_bridges` | Hot path is inside networkx, not in our code |

The codebase is well-optimized. The **only new Tier-1 candidate** is `batch_create_nodes` / `batch_create_edges`; the rest are either already known, already investigated (OHM-lqpk.2), or bounded.

---

## Tier 1: New candidate worth shipping

### `batch_create_nodes` and `batch_create_edges` — N round-trips

**Location:**
- `src/ohm/graph/queries/__init__.py:2877` (`batch_create_nodes`)
- `src/ohm/graph/queries/__init__.py:2911` (`batch_create_edges`)

**The pattern:**
```python
def batch_create_nodes(conn, *, nodes, created_by):
    results = []
    for node_data in nodes:
        result = create_node(conn, label=node_data["label"], ...)
        results.append(result)
    return results
```

Each call to `create_node` does:
1. Validate inputs (`validate_identifier` etc.)
2. Check for existing node (one `SELECT`)
3. Compute embedding hash
4. Run the full `INSERT` (with `created_at`, `updated_at`, `created_by`, etc.)
5. Run a `UNIQUE`-constraint check
6. Call `_log_change` (one `INSERT` into `ohm_change_feed`)

For a batch of 1000 nodes, this is 1000 round-trips. The HTTP overhead alone is significant even before the SQL work.

**Why this matters:**
- The `/batch` endpoint is the documented way to write large graph states (see the writing protocol in AGENTS.md).
- `ohm_change_log` ingestion pipeline (scripts/ingestion/) uses batch operations.
- DuckLake sync (store.py) replicates the per-row pattern but with even more overhead.

**Suggested fix (not implemented in this spike):**
- Rewrite `batch_create_nodes` as a single `INSERT INTO ohm_nodes SELECT ... FROM (VALUES (...))` with all 1000 rows.
- Same for `batch_create_edges`.
- Validation can happen in a single `WHERE id IN (...)` pre-check.
- Change-feed entries can be a single multi-row `INSERT INTO ohm_change_feed`.
- Expected speedup: 10-100× for batches of 1000+ rows. Even a 5× speedup is worth shipping for a hot path.

**Should this be a child issue of OHM-lqpk.5 (profiling)?** The optimization is clear without profiling — the N round-trips are obviously wasteful. Recommend filing as a separate child issue of the OHM-lqpk epic (or as a follow-up to OHM-lqpk.4 if Rust is on the table for this).

---

## Tier 1.5: Already investigated (OHM-lqpk.2)

### `hd_membership_search` — per-row Hamming distance loop

**Location:** `src/ohm/graph/queries/__init__.py:6211`

**The pattern:** Reads all stored HD fingerprints, runs `hamming_similarity` in a Python loop per row.

**Status:** Investigated in OHM-lqpk.2 (closed 2026-07-02). Findings:
- DuckDB 1.5.2 has a broken `^` operator on integer types — workaround via De Morgan.
- Schema change required (BLOB → UTINYINT[]) to enable SQL path.
- Benchmark: SQL path is **50-60× SLOWER** at N=100-1000, **1.5× slower** at N=10k.
- **Decision: do not implement.** Python loop is faster for current OHM scale.

**No further action** unless OHM scales past 10k candidates or DuckDB fixes the `^` operator bug.

---

## Tier 2: Bounded, not currently hot

### `find_islands` — connected components with per-island loop

**Location:** `src/ohm/graph/methods.py:4111`

**The pattern:** Union-Find over edges, then a per-island loop to count internal edges. The per-island loop at line 4235 is `O(E)` per island — worst case `O(E × max_islands)`.

**Benchmark** (synthetic graphs, 3 runs each):
- 500 nodes, 1000 edges: median 3.6 ms
- 1000 nodes, 3000 edges: median 7.1 ms
- 2000 nodes, 5000 edges: median 12.0 ms

**Verdict:** Fast enough at current scale. The `max_islands=20` cap keeps the worst case bounded. If production graphs reach 50k+ edges, revisit — but for now, no action.

### `query_path` BFS — SQL-per-level traversal

**Location:** `src/ohm/graph/queries/__init__.py:189`

Uses BFS with frontier set but **one SQL query per level** to fetch outgoing edges. Avoids the O(N) edge load of a pure-Python implementation. Comment in the function explains the design (DuckDB recursive CTEs blow up exponentially in dense graphs).

**Verdict:** Already optimized. No action.

### `query_stale_edges` and `apply_confidence_decay`

**Locations:**
- `src/ohm/graph/queries/__init__.py:2788` (per-row loop in `query_stale_edges`)
- `src/ohm/graph/queries/__init__.py:2796` (per-edge loop in `apply_confidence_decay`)

**The pattern:** Iterates over stale edges to compute decay. Bounded by the number of edges past the staleness threshold — typically a small fraction of total edges.

**Verdict:** Not currently hot. Could be SQL-only (the effective_confidence formula is a simple `confidence * 0.5 ^ (age_days / half_life)` expression).

### `find_bridges`

**Location:** `src/ohm/graph/methods.py:3045`

Uses networkx for `bridges()` and `articulation_points()`. The hot path is in networkx, not in our code. Our code is a thin SQL-to-networkx adapter.

**Verdict:** No action on our side. If bridge detection is hot, the fix is in networkx (or a hand-rolled Tarjan's SCC algorithm).

---

## Patterns searched

### 1. `for _ in range(N)` with N >= 100

| Location | N | Status |
|----------|---|--------|
| `methods.py:902, 907` | `simulations`, `depth` (configurable) | **Tier 1, known** — `monte_carlo_impact` |
| `queries/__init__.py:221` | `max_depth` (validated ≤ 50) | Tier 2, bounded — `query_path` BFS |
| `queries/__init__.py:3633, 3638` | `trials`, `max_depth` (configurable) | **Tier 1, known** — `monte_carlo_cascade` |
| `bayesian.py:480` | 6 | Not hot (fixed small loop) |
| `game_theory.py:320` | 2000 | Tier 2, already uses numpy |

### 2. Nested for loops over edges/nodes

| Location | Pattern | Status |
|----------|---------|--------|
| `methods.py:891-925` | Adjacency list build + BFS | **Tier 1, known** — `monte_carlo_impact` |
| `methods.py:4194, 4203, 4216, 4220, 4235` | Union-Find + per-island edge counting | Tier 2, fast (12ms @ 5k edges) |
| `queries/__init__.py:2896, 2930` | N round-trips to `create_node`/`create_edge` | **Tier 1, NEW** — `batch_create_*` |
| `queries/__init__.py:5161, 5390` | Per-row fragment processing | Tier 2, bounded by fragment count |
| `queries/__init__.py:6211` | Per-row Hamming | Tier 1.5 — already investigated |

### 3. `random.random()` calls (stochastic Python)

| Location | Context | Status |
|----------|---------|--------|
| `methods.py:919` | Edge-failure sample in `monte_carlo_impact` | **Tier 1, known** |
| `queries/__init__.py:3639` | Edge-failure sample in `monte_carlo_cascade` | **Tier 1, known** |

No new stochastic hot paths.

### 4. While loops with frontier/visited sets (BFS/DFS)

Three patterns found, all already known:
- `methods.py:903-925` (`monte_carlo_impact`)
- `queries/__init__.py:218-253` (`query_path` BFS, already SQL-per-level)
- `queries/__init__.py:3635-3660` (`monte_carlo_cascade`)

### 5. Per-row dict construction over query results

| Location | Bounded by | Status |
|----------|-----------|--------|
| `queries/__init__.py:2115, 2788, 3384, 5161, 5390, 6211, 6254, 6808, 6851, 8259, 12910, 13196` | Various — most are LIMIT-bounded | Not currently hot |
| `queries/__init__.py:4172, 4184, 4214, 4236` | Embedding/Hamming search | Tier 1.5 — `hd_membership_search`, already investigated |

No new per-row hot paths.

---

## Recommendations

1. **File a new child issue** under OHM-lqpk epic for `batch_create_nodes` / `batch_create_edges` rewrite. The N round-trips are a clear Tier-1 win for any ingestion path.

2. **No further action** on `hd_membership_search` until OHM scales past 10k candidates or DuckDB fixes the `^` operator.

3. **No further action** on `find_islands`, `query_path`, `query_stale_edges`, `apply_confidence_decay`, or `find_bridges` — all bounded or delegated to libraries.

4. **Re-run this audit** when OHM scales past 10k nodes/edges or when the OHM-lqpk.5 profiling spike lands real production data.

## Methodology

```bash
# 1. Find for-range patterns
rg -n "for _ in range\(" src/ohm/graph/queries/__init__.py src/ohm/graph/methods.py

# 2. Find for-in-rows patterns
rg -n "for\s+\w+\s+in\s+.*\b(rows|results|edges|nodes)\b" src/ohm/graph/queries/__init__.py

# 3. Find frontier/visited patterns
rg -n "visited\s*=\s*set\(\)|frontier\s*=" src/ohm/graph/queries/__init__.py src/ohm/graph/methods.py

# 4. Find random.random calls
rg -n "random\.(random|uniform|randint)" src/ohm/graph/queries/__init__.py src/ohm/graph/methods.py

# 5. Find nested for loops
rg -n "for\s+\w+\s+in\s+edges" src/ohm/graph/methods.py
```

Total patterns found: ~30 candidates. After inspection, **1 new Tier-1 candidate** (`batch_create_nodes/edges`), 2 already-investigated (`monte_carlo_*`, `hd_membership_search`), 4 Tier-2 bounded, and the rest are SQL-only or already optimized.
