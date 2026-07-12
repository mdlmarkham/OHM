#!/usr/bin/env python3
"""Sample ETL script — migrate legacy TOPO topo_reports into OHM-TOPO.

Demonstrates the column transformation documented in
docs/migrating-from-legacy-topo.md and consumes the machine-readable
mapping from docs/migrating-from-legacy-topo-mapping.yaml.

Usage:
    # Dry run (default) — prints transformed rows without inserting
    python scripts/migrate_topo_reports.py --legacy-db /path/to/legacy.duckdb

    # Apply — inserts into OHM database
    python scripts/migrate_topo_reports.py --legacy-db /path/to/legacy.duckdb --apply

    # With lookup overrides
    python scripts/migrate_topo_reports.py --legacy-db /path/to/legacy.duckdb \\
        --lookups '{"node": {"Plant A": "node_001"}, "plan": {"John": "plan_001"}}'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import yaml


def load_mapping(yaml_path: Path) -> dict:
    """Load the machine-readable column mapping."""
    with open(yaml_path) as f:
        return yaml.safe_load(f)


def transform_json_or_wrap(value: str | None, wrap_key: str = "action_items") -> str | None:
    """If value is valid JSON, return it. Otherwise wrap in {wrap_key: [value]}."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
        return json.dumps(parsed)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({wrap_key: [value]})


def transform_json_passthrough(value: str | None) -> str | None:
    """If value is valid JSON, return as-is. If double-encoded, parse and re-encode."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, str):
            return json.dumps(json.loads(parsed))
        return json.dumps(parsed)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"raw_value": value})


def resolve_lookup(value: str | None, lookup_table: dict, fallback: str | None = None) -> str | None:
    """Resolve a free-text value via a lookup table."""
    if value is None:
        return None
    return lookup_table.get(value, fallback)


def apply_mapping(row: dict, mapping: dict, lookups: dict) -> dict:
    """Apply the column mapping to a single legacy row."""
    result = {}
    metadata_extras = {}

    for m in mapping["mappings"]:
        legacy_col = m["legacy"]
        ohm_col = m["ohm"]
        transform = m["transform"]

        raw_value = row.get(legacy_col) if legacy_col else None

        if transform == "direct":
            result[ohm_col] = raw_value
        elif transform == "rename":
            result[ohm_col] = raw_value
        elif transform == "null":
            result[ohm_col] = None
        elif transform == "lookup":
            lookup_type = m.get("lookup_type", "")
            lookup_table = lookups.get(lookup_type, {})
            resolved = resolve_lookup(raw_value, lookup_table, m.get("fallback"))
            if resolved is None and raw_value and m.get("metadata_key"):
                metadata_extras[m["metadata_key"]] = raw_value
            result[ohm_col] = resolved
        elif transform == "json_passthrough":
            fallback_col = m.get("fallback_column")
            fallback_val = row.get(fallback_col) if fallback_col else None
            if raw_value is not None:
                result[ohm_col] = transform_json_passthrough(raw_value)
            elif fallback_val is not None:
                fb_transform = m.get("fallback_transform", "direct")
                if fb_transform == "json_or_wrap":
                    result[ohm_col] = transform_json_or_wrap(fallback_val)
                else:
                    result[ohm_col] = transform_json_passthrough(fallback_val)
            else:
                result[ohm_col] = None
        elif transform == "json_or_wrap":
            wrap_key = m.get("wrap_key", "action_items")
            result[ohm_col] = transform_json_or_wrap(raw_value, wrap_key)
        else:
            result[ohm_col] = raw_value

    if metadata_extras:
        existing_meta = json.loads(result.get("metadata") or "{}")
        existing_meta.update(metadata_extras)
        result["metadata"] = json.dumps(existing_meta)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy TOPO topo_reports to OHM-TOPO")
    parser.add_argument("--legacy-db", required=True, help="Path to legacy TOPO DuckDB database")
    parser.add_argument("--legacy-table", default="topo_reports", help="Legacy table name (default: topo_reports)")
    parser.add_argument("--ohm-db", default=None, help="OHM DuckDB path (default: same as legacy)")
    parser.add_argument("--apply", action="store_true", help="Actually insert (default: dry run)")
    parser.add_argument("--lookups", default=None, help="JSON string of lookup tables")
    parser.add_argument(
        "--mapping", default=None,
        help="Path to mapping YAML (default: docs/migrating-from-legacy-topo-mapping.yaml)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    mapping_path = Path(args.mapping) if args.mapping else repo_root / "docs" / "migrating-from-legacy-topo-mapping.yaml"

    mapping = load_mapping(mapping_path)
    lookups = json.loads(args.lookups) if args.lookups else {}

    legacy_conn = duckdb.connect(args.legacy_db, read_only=True)
    try:
        rows = legacy_conn.execute(f"SELECT * FROM {args.legacy_table}").fetchall()
        cols = [desc[0] for desc in legacy_conn.execute(f"SELECT * FROM {args.legacy_table} LIMIT 0").description]
    except Exception as e:
        print(f"Error reading legacy table: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Read {len(rows)} rows from legacy {args.legacy_table}")

    transformed = []
    for row in rows:
        row_dict = dict(zip(cols, row))
        result = apply_mapping(row_dict, mapping, lookups)
        transformed.append(result)

    for i, t in enumerate(transformed[:5]):
        print(f"\n--- Row {i} ---")
        for k, v in t.items():
            val_preview = str(v)[:80] if v else v
            print(f"  {k}: {val_preview}")

    if len(transformed) > 5:
        print(f"\n... and {len(transformed) - 5} more rows")

    if not args.apply:
        print(f"\n[DRY RUN] {len(transformed)} rows transformed. Use --apply to insert.")
        return

    ohm_path = args.ohm_db or args.legacy_db
    ohm_conn = duckdb.connect(ohm_path)
    try:
        from ohm.graph.schema import TOPO_SCHEMA, initialize_schema
        initialize_schema(ohm_conn, TOPO_SCHEMA)
    except ImportError:
        print("Warning: Could not import OHM schema. Ensure OHM is installed.", file=sys.stderr)

    inserted = 0
    for t in transformed:
        try:
            ohm_conn.execute(
                """INSERT INTO topo_reports
                   (id, report_type, node_id, plan_id, title, summary, findings,
                    recommendations, confidence_adjustments, status, version,
                    superseded_by, created_by, created_at, updated_at, finalized_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    t["id"], t["report_type"], t.get("node_id"), t.get("plan_id"),
                    t.get("title"), t.get("summary"), t.get("findings"),
                    t.get("recommendations"), t.get("confidence_adjustments"),
                    t.get("status"), t.get("version"), t.get("superseded_by"),
                    t.get("created_by"), t.get("created_at"), t.get("updated_at"),
                    t.get("finalized_at"), t.get("metadata"),
                ],
            )
            inserted += 1
        except Exception as e:
            print(f"  SKIP {t.get('id')}: {e}", file=sys.stderr)

    print(f"\n[APPLIED] {inserted}/{len(transformed)} rows inserted into {ohm_path}")

    ohm_conn.close()
    legacy_conn.close()


if __name__ == "__main__":
    main()
