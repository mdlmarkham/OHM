#!/usr/bin/env python3
"""scripts/cleanup_test_artifacts.py — Clean up live test artifacts.

Per docs/test-reports/2026-06-30_live_daemon_test_report.md, eight
``metis_test_*`` nodes + associated edges/observations were left in the
production graph after the live daemon integration test. This script
soft-deletes them, cascading to edges and observations.

Usage:
    python scripts/cleanup_test_artifacts.py --dry-run
    python scripts/cleanup_test_artifacts.py --db-path /var/lib/ohm/ohm.duckdb
    python scripts/cleanup_test_artifacts.py --pattern 'metis_test_%'
    python scripts/cleanup_test_artifacts.py --ids node1,node2,node3

By default:
- pattern matches against ``id`` and ``label`` columns of ``ohm_nodes``
- only nodes whose ``deleted_at IS NULL`` are affected
- dry-run prints what would be deleted without modifying the DB
- uses the same soft-delete cascade as the HTTP API (delete_node query fn)

Tested against the same in-memory DuckDB schema used by the test suite.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ohm.queries import delete_node  # noqa: E402
from ohm.schema import initialize_schema  # noqa: E402


def _open_duckdb(db_path: str):
    """Open DuckDB at db_path. Defaults to the production ohm.duckdb."""
    import duckdb

    if db_path == ":memory:":
        return duckdb.connect(":memory:")
    return duckdb.connect(db_path)


def _list_candidate_ids(conn, pattern: str) -> list[str]:
    """Find node ids matching the pattern (id LIKE or label LIKE), excluding
    already-soft-deleted rows."""
    rows = conn.execute(
        """
        SELECT id FROM ohm_nodes
        WHERE deleted_at IS NULL
          AND (id LIKE ? OR label LIKE ?)
        ORDER BY id
        """,
        [pattern, pattern],
    ).fetchall()
    return [r[0] for r in rows]


def main(argv: list[str] | None = None) -> int:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.split("\n\n")[0])
    parser.add_argument(
        "--db-path",
        default="/var/lib/ohm/ohm.duckdb",
        help="DuckDB path (default: %(default)s; use ':memory:' for tests)",
    )
    parser.add_argument(
        "--pattern",
        default="metis_test_%",
        help="SQL LIKE pattern (default: %(default)s)",
    )
    parser.add_argument(
        "--ids",
        default=None,
        help="Comma-separated explicit node ids to delete (overrides --pattern)",
    )
    parser.add_argument(
        "--deleted-by",
        default="ops_cleanup",
        help="Agent name for the soft-delete attribution (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without modifying the DB",
    )
    parser.add_argument(
        "--init-schema",
        action="store_true",
        help="Initialize schema before deleting (for tests with :memory:)",
    )
    args = parser.parse_args(argv)

    conn = _open_duckdb(args.db_path)
    try:
        if args.init_schema:
            initialize_schema(conn)

        if args.ids:
            candidate_ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        else:
            candidate_ids = _list_candidate_ids(conn, args.pattern)

        if not candidate_ids:
            print(f"No nodes match pattern '{args.pattern}'. Nothing to do.")
            return 0

        print(f"Found {len(candidate_ids)} candidate node(s):")
        for nid in candidate_ids:
            print(f"  - {nid}")

        if args.dry_run:
            print("\nDRY-RUN: no changes made. Re-run without --dry-run to apply.")
            return 0

        results = {"deleted_nodes": [], "edges_removed": 0, "observations_removed": 0}
        for nid in candidate_ids:
            try:
                r = delete_node(conn, node_id=nid, deleted_by=args.deleted_by)
                results["deleted_nodes"].append(nid)
                results["edges_removed"] += r.get("edges_removed", 0)
                results["observations_removed"] += r.get("observations_removed", 0)
            except Exception as e:
                print(f"  FAILED to delete {nid}: {e}", file=sys.stderr)
                results.setdefault("failed", []).append({"id": nid, "error": str(e)})

        print("\nCleanup summary:")
        print(json.dumps(results, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
