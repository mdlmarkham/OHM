# DuckLake Operational Guide

OHM uses DuckLake as a persistent shared backend. This document covers configuration, sync, recovery, and operational patterns.

## Architecture

```
ohmd (daemon)
  ├── DuckDB (local database)     ← queries run here
  └── DuckLake (mirror)           ← persistent truth
        ├── ohm_lake.ducklake     ← catalog (metadata, snapshots)
        └── ohm_lake_data/        ← Parquet data files
              └── main/
```

**DuckDB is the working store.** All queries execute against DuckDB. DuckLake mirrors the data for durability, time travel, and multi-agent awareness.

**DuckLake is the canonical truth.** When DuckDB's WAL is corrupted, DuckLake snapshots provide recovery. The change feed records every write.

## Configuration

### Environment Variables

| Variable | Required | Purpose | Default |
|----------|----------|---------|---------|
| `OHM_DUCKLAKE_PATH` | Yes | Catalog file path | None (DuckLake disabled) |
| `OHM_DUCKLAKE_DATA` | No | Parquet data directory | `<catalog_path>_data/` |

### ohmd.json

```json
{
  "ducklake": {
    "path": "/var/lib/ohm/ohm_lake.ducklake",
    "data_path": "/var/lib/ohm/ohm_lake_data"
  }
}
```

### File Paths (production)

```
/var/lib/ohm/
├── ohm.duckdb              ← DuckDB working database
├── ohm.duckdb.wal          ← Write-ahead log
├── ohm_lake.ducklake       ← DuckLake catalog
├── ohm_lake.ducklake.wal   ← DuckLake catalog WAL
└── ohm_lake_data/          ← Parquet data files
    └── main/
```

## Mirror Tables

DuckLake mirror tables use **VARCHAR for all columns** — DuckLake does not support PRIMARY KEY or UNIQUE constraints. Uniqueness is enforced in application code (ohmd upsert logic).

| DuckDB Table | DuckLake Mirror | Notes |
|--------------|------------------|-------|
| `ohm_nodes` | `ohm_lake.ohm_nodes` | All node fields as VARCHAR |
| `ohm_edges` | `ohm_lake.ohm_edges` | All edge fields as VARCHAR |
| `ohm_observations` | `ohm_lake.ohm_observations` | All observation fields as VARCHAR |
| `ohm_change_feed` | `ohm_lake.ohm_change_feed` | Change audit trail |

The type widening (FLOAT → VARCHAR, TIMESTAMP → VARCHAR) is intentional. DuckLake stores to Parquet which is strongly typed at the file level, but the catalog requires consistent types across snapshots. VARCHAR avoids migration issues.

## Sync: push_to_ducklake / pull_from_ducklake

### SDK Method

```python
with ohm.connect_remote("http://127.0.0.1:8710", actor="metis",
                       token=os.environ["OHM_TOKEN"]) as g:
    result = g.sync_heartbeat()
    # Returns: {"pushed_count": N, "pulled_count": M, "last_sync": "timestamp"}
```

### How It Works

1. **Push:** Local DuckDB rows that don't exist in DuckLake are inserted into mirror tables
2. **Pull:** DuckLake rows that don't exist locally are upserted into DuckDB
3. **Conflict:** Last-write-wins with attribution (L1/L2 shared). No conflict possible for L3/L4 (agent-owned)
4. **Agent last_sync:** Updated after sync completes

### When to Sync

- **Heartbeat-based** (recommended): Every 5-15 minutes per agent
- **On write** (optional): Immediate consistency, higher overhead
- **Batch** (future): Accumulate changes, sync on schedule

## Time Travel

### HTTP Endpoints

```bash
# List all snapshots
curl http://127.0.0.1:8710/admin/snapshots

# Query graph at snapshot version N
curl "http://127.0.0.1:8710/graph/at?version=5"

# Diff between two versions
curl "http://127.0.0.1:8710/graph/changes?from_version=5&to_version=44"
```

### Snapshot Structure

Each snapshot records:
- `snapshot_id` — sequential integer
- `snapshot_time` — when the snapshot was taken
- `schema_version` — schema at that point
- `changes` — what changed (tables created, rows inserted, inlined inserts)

### Typical Snapshot Lifecycle

Snapshots 0-4: Schema creation (tables)
Snapshots 5+: Data writes (inserts, deletes, updates)
Latest snapshot: Current state

## WAL Corruption Recovery

If DuckDB's WAL file is corrupted (crash, disk full, power loss):

### Stage 1: DuckLake Snapshot Fallback (OHM-kdk.4)

1. Detect WAL corruption on `duckdb.connect()` (IOException with "WAL" in message)
2. Attach DuckLake catalog to a temporary in-memory connection
3. Find latest snapshot with data
4. Export nodes/edges from snapshot
5. Move corrupted DB to `.corrupted` backup
6. Create fresh DuckDB with OHM schema
7. Import nodes/edges from snapshot
8. Log recovery to `ohm_change_feed`

**Result:** Data preserved from last DuckLake sync. Uncommitted writes between last sync and crash are lost.

### Stage 2: WAL Deletion (OHM-b5a)

If DuckLake is not configured or has no snapshots:

1. Delete the `.wal` file
2. Reconnect to DuckDB

**Result:** WAL-contained (uncommitted) writes are lost. Main DB file is intact.

### Known Gap

If DuckLake extension is not installed and WAL is corrupted, DuckDB may segfault before Python recovery can execute. **Workaround:** ensure DuckLake extension is installed, or manually delete the WAL file and restart ohmd.

```bash
# Manual recovery if ohmd won't start
rm /var/lib/ohm/ohm.duckdb.wal
systemctl restart ohmd
```

## Monitoring

```bash
# Check DuckLake is attached
curl http://127.0.0.1:8710/status | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Version: {d[\"version\"]}')
print(f'Nodes: {d[\"node_count\"]}, Edges: {d[\"edge_count\"]}')
"

# Snapshot count and freshness
curl http://127.0.0.1:8710/admin/snapshots | python3 -c "
import sys, json
d = json.load(sys.stdin)
snaps = d['snapshots']
print(f'Snapshots: {len(snaps)}')
print(f'Latest: {snaps[-1][\"snapshot_time\"]}')
"

# Disk usage
du -sh /var/lib/ohm/ohm_lake.ducklake /var/lib/ohm/ohm_lake_data/
```

## Future: Per-Agent Local DuckDB Caches

The current architecture has one DuckDB owned by ohmd. The planned evolution:

1. Each agent runs a local DuckDB for working memory
2. On heartbeat, agents sync local → DuckLake (push) and DuckLake → local (pull)
3. ohmd becomes a coordination layer, not the sole data owner
4. Quack protocol enables direct multi-writer access to DuckLake

This is Phase 4 (partially complete). The sync infrastructure (`push_to_ducklake`, `pull_from_ducklake`, `sync_heartbeat`) is implemented but per-agent caches don't exist yet.