# OHM Crash Recovery — Remaining Fixes

## What the FatalException crash taught us:

### 1. FatalException is uncatchable (P0 - RESOLVED in prior commit)
- DuckDB's `FatalException` calls `abort()` at C level — Python cannot catch it
- The PK violation on soft-deleted rows triggers this
- Fix: Check for soft-deleted rows before every INSERT (commit a36689d)
- This is working correctly for HTTP API calls

### 2. DuckLake initial sync crashes on restart (P0 - PARTIALLY FIXED)
- When daemon restarts with a fresh DB, `_initial_sync_table` does plain INSERT
- If DuckLake mirror has rows that match (from prior runs), INSERT violates PK
- This is what crashed the daemon repeatedly after the DB was recreated
- Fix in commit 06c7c9c: DELETE + INSERT pattern, filter deleted_at IS NULL
- **BUT**: The crash happened because the local DB got corrupted first,
  then on restart the initial sync tried to INSERT rows that were already
  in the mirror from the corrupted DB's prior push.

### 3. DuckLake push syncs soft-deleted rows (P1 - FIXED, then RE-FIXED in OHM-822)
- `_incremental_sync_table` was pushing ALL rows including soft-deleted ones
- Initial fix: Filter with `WHERE deleted_at IS NULL` — excluded soft-deleted
  rows from the changed-row query entirely
- **Problem**: This also prevented soft-delete tombstones from propagating to
  the DuckLake mirror. The old active version stayed in the mirror with
  `deleted_at = NULL`, so a rebuild from DuckLake resurrected the node.
- **OHM-822 fix**: Remove the `deleted_at IS NULL` filter from the changed-row
  query. Find ALL changed rows (including soft-deleted), DELETE them from the
  mirror, then re-INSERT only active rows. Soft-deleted rows are removed from
  the mirror entirely — no stale active version remains to be resurrected.

### 4. Daemon crash corrupts local DB irreversibly (P0 - ADDRESSED in OHM-822)
- Once FatalException fires, DuckDB marks the connection as invalidated
- All subsequent queries on that connection fail
- The daemon process gets SIGABRT and dies
- On restart, DuckDB may refuse to open the DB file at all
- **OHM-822**: All three rebuild paths now filter `WHERE deleted_at IS NULL`
  from the DuckLake mirror, ensuring soft-deleted rows are not resurrected
  during recovery:
  - `rebuild_from_ducklake.py` — added `WHERE deleted_at IS NULL` to all
    INSERT...SELECT statements (nodes, edges, observations)
  - `store.py` `_recover_from_ducklake()` — added `WHERE deleted_at IS NULL`
    to the INSERT...SELECT from the DuckLake mirror
  - `store.py` `_auto_restore_if_empty()` — same fix
- Auto-recovery is already implemented via `_try_ducklake_recovery` in db.py
  and `_auto_restore_if_empty` in store.py; the fix ensures they don't
  resurrect soft-deleted data.
- **Also fixed**: `node_cols`/`edge_cols` bug in `db.py`
  `_try_ducklake_recovery()` — both were set from the edges query's
  `description`, causing node inserts to use edge column names. Now each
  is captured immediately after its respective query.
- Still requires manual intervention if the DB file is corrupted
  (rebuild_from_ducklake.py), but the rebuild is now correct.

### 5. Embedding generation crashes daemon (P1 - FIXED)
- Processing all 161+ nodes at once caused OOM or timeout
- Fix: batch_size parameter, delay_ms parameter, paginated results
- Client calls repeatedly until remaining=0

### 6. DB rebuild from DuckLake has column mapping issues (P1 - DOCUMENTED)
- DuckLake mirror stores all columns as VARCHAR
- Local schema has typed columns (FLOAT, TIMESTAMP, JSON)
- Column names differ between mirror and local (e.g., condition vs content)
- rebuild_from_ducklake.py handles this mapping

### 7. Daemon startup should attempt auto-recovery from DuckLake (P2 - ADDRESSED in OHM-822)
- Auto-recovery is already implemented via `_try_ducklake_recovery` (db.py)
  and `_auto_restore_if_empty` (store.py).
- **OHM-822**: Fixed the rebuild paths to filter `WHERE deleted_at IS NULL`
  from the DuckLake mirror, preventing soft-deleted nodes from being
  resurrected during auto-recovery.
- Also fixed `node_cols`/`edge_cols` capture bug in `_try_ducklake_recovery`
  (both were set from the edges query description).
- The recovery event is logged to `ohm_change_feed` with operation
  `RECOVERY` (see `_try_ducklake_recovery` in db.py).

## Proposed Implementation for #4 and #7:

In `store.py`, modify `_connect_with_wal_recovery` to also handle FatalException
during initial connection, and add a `rebuild_from_ducklake` method that's called
automatically when the DB is corrupted.

In `server.py`, add a try/except around the DB connection in startup:
1. Try to open the DB normally
2. If FatalException, delete the DB file and WAL
3. Reconnect and initialize schema
4. Pull from DuckLake mirror
5. Log the recovery