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

### 3. DuckLake push syncs soft-deleted rows (P1 - FIXED)
- `_incremental_sync_table` was pushing ALL rows including soft-deleted ones
- Fix: Filter with `WHERE deleted_at IS NULL`

### 4. Daemon crash corrupts local DB irreversibly (P0 - NEEDS FIX)
- Once FatalException fires, DuckDB marks the connection as invalidated
- All subsequent queries on that connection fail
- The daemon process gets SIGABRT and dies
- On restart, DuckDB may refuse to open the DB file at all
- **Fix needed**: In server.py startup, catch FatalException during
  DB open, delete the corrupted DB, and rebuild from DuckLake
- Currently requires manual intervention (rebuild_from_ducklake.py)

### 5. Embedding generation crashes daemon (P1 - FIXED)
- Processing all 161+ nodes at once caused OOM or timeout
- Fix: batch_size parameter, delay_ms parameter, paginated results
- Client calls repeatedly until remaining=0

### 6. DB rebuild from DuckLake has column mapping issues (P1 - DOCUMENTED)
- DuckLake mirror stores all columns as VARCHAR
- Local schema has typed columns (FLOAT, TIMESTAMP, JSON)
- Column names differ between mirror and local (e.g., condition vs content)
- rebuild_from_ducklake.py handles this mapping

### 7. Daemon startup should attempt auto-recovery from DuckLake (P2 - NOT YET IMPLEMENTED)
- If DB open fails with FatalException, delete corrupted DB
- Rebuild from DuckLake mirror
- Log the recovery event
- This would prevent the manual intervention cycle

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