#!/usr/bin/env python3
"""scripts/backfill_challenge_reasons.py — Backfill null-reason challenges (OHM-e0t1).

Per ADR-018 and OHM-e0t1, every open CHALLENGED_BY edge must have
a non-empty challenge_reason. The 45 historically-null reasons on the
live daemon can be inferred from each challenge's target edge:
type + layer + confidence gap all point to a specific rationale
("weak CAUSES claim", "overconfident PREDICTS", etc.).

This script wraps ohm.graph.challenges.backfill_challenge_reasons()
with a CLI and operator-friendly output. It does NOT call any LLM
or remote service — all inference is rule-based and runs in-process
against the local DuckDB.

Usage:
    # Dry run: see what would change without writing
    python scripts/backfill_challenge_reasons.py --dry-run

    # Apply
    python scripts/backfill_challenge_reasons.py

    # Apply against a specific DB
    python scripts/backfill_challenge_reasons.py --db-path /var/lib/ohm/ohm.duckdb

    # JSON output for piping
    python scripts/backfill_challenge_reasons.py --dry-run --format json

The script is idempotent — re-running it after a successful backfill
will find zero null-reason challenges and return scanned=0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ohm.graph.challenges import backfill_challenge_reasons  # noqa: E402
from ohm.schema import initialize_schema  # noqa: E402


def _open_duckdb(db_path: str):
    """Open DuckDB at db_path. Defaults to the production ohm.duckdb."""
    import duckdb

    if db_path == ":memory:":
        return duckdb.connect(":memory:")
    return duckdb.connect(db_path, read_only=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill null-reason CHALLENGED_BY edges (OHM-e0t1).")
    parser.add_argument(
        "--db-path",
        default="/var/lib/ohm/ohm.duckdb",
        help="Path to the OHM DuckDB file (default: /var/lib/ohm/ohm.duckdb).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan + infer but don't write. Default: False (apply).",
    )
    parser.add_argument(
        "--agent",
        default="ohmd_backfill",
        help="Agent name to record in ohm_change_feed for audit trail.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args(argv)

    conn = _open_duckdb(args.db_path)
    try:
        # Defensive: ensure the base OHM schema is present. Idempotent —
        # the CREATE TABLE IF NOT EXISTS statements are no-ops if the
        # schema is already there.
        if not args.dry_run:
            initialize_schema(conn)

        result = backfill_challenge_reasons(conn, dry_run=args.dry_run, agent=args.agent)

        if args.format == "json":
            print(json.dumps(result, indent=2, default=str))
        else:
            mode = "DRY RUN" if args.dry_run else "APPLY"
            print(f"=== Challenge Reason Backfill ({mode}) ===")
            print(f"Null-reason challenges scanned: {result['scanned']}")
            print(f"Backfilled (written):           {result['backfilled']}")
            print(f"Proposed (would-write):          {len(result['proposed'])}")
            print(f"Errors:                          {len(result['errors'])}")
            if result["proposed"]:
                print("")
                print("Proposed updates:")
                for p in result["proposed"][:25]:
                    print(f"  {p['challenge_id'][:12]}.. target={p['target_edge_id'][:12]}.. reason={p['reason'][:60]}...")
                if len(result["proposed"]) > 25:
                    print(f"  ... and {len(result['proposed']) - 25} more (use --format json to see all)")
            if result["errors"]:
                print("")
                print("Errors:")
                for e in result["errors"]:
                    print(f"  {e}")
                return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
