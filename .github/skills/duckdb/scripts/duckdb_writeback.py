#!/usr/bin/env python3
"""
duckdb_writeback.py v2.0 — Safe write-back: Postgres, Parquet, DuckLake.

Dry-run is ON by default. Use --no-dry-run to commit.
Credentials via --pg-password-env ENV_VAR (preferred over --pg-password).

Usage examples in SKILL.md Write-Back section.
"""

from __future__ import annotations
import argparse
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)
LARGE_WRITE_THRESHOLD = int(os.getenv("LARGE_WRITE_THRESHOLD", "10000"))


def _load_helpers():
    import importlib.util
    import sys

    mod_name = "duckdb_helper"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name,
        Path(__file__).resolve().parent / "duckdb_helper.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod  # register before exec so dataclass refs resolve
    spec.loader.exec_module(mod)
    return mod


def _signoff(count, target):
    print(f"\n\u26a0  LARGE WRITE: {count:,} rows -> {target}")
    print(f"  Threshold: {LARGE_WRITE_THRESHOLD:,}. Type 'yes' to proceed: ", end="", flush=True)
    if sys.stdin.readline().strip().lower() != "yes":
        print("Aborted.")
        sys.exit(1)


def _schema_compat(src_cols, schema, table, con, h):
    h._require_identifier(schema, "pg_schema")
    h._require_identifier(table, "pg_table")
    rows = con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=?", [schema, table]).fetchall()
    have = {r[0] for r in rows}
    return [c for c in src_cols if c not in have]


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------
def writeback_to_postgres(*, duckdb_path, source_sql, host, port, dbname, user, password, pg_schema, pg_table, conflict_key, dry_run):
    import duckdb

    h = _load_helpers()
    safe_schema = h._require_identifier(pg_schema)
    safe_table = h._require_identifier(pg_table)
    safe_key = h._require_identifier(conflict_key) if conflict_key else None
    dsn = f"host='{h._dsn_escape(host)}' port={int(port)} dbname='{h._dsn_escape(dbname)}' user='{h._dsn_escape(user)}' password='{h._dsn_escape(password)}'"
    con = duckdb.connect(duckdb_path, read_only=False)
    try:
        h.ensure_extension(con, "postgres")
        con.execute(f"ATTACH '{dsn}' AS pg_rw (TYPE postgres);")
        con.execute(f"CREATE TEMP TABLE __src AS {source_sql};")
        count = con.execute("SELECT COUNT(*) FROM __src").fetchone()[0]
        raw_cols = [r[0] for r in con.execute("DESCRIBE __src").fetchall()]
        cols = h._require_identifier_list(raw_cols, "source columns")
        missing = _schema_compat(cols, safe_schema, safe_table, con, h)
        if missing:
            print(f"WARNING: source columns not in target: {missing}")
            print("Proceed? [y/N] ", end="", flush=True)
            if sys.stdin.readline().strip().lower() != "y":
                return
        label = f"{host}/{dbname}/{safe_schema}.{safe_table}"
        if dry_run:
            print(f"\n[DRY RUN] {count:,} rows -> {label}  key={safe_key or 'INSERT only'}")
            return
        if count > LARGE_WRITE_THRESHOLD:
            _signoff(count, label)
        tgt = f"pg_rw.{safe_schema}.{safe_table}"
        if safe_key:
            upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != safe_key)
            sql = f"INSERT INTO {tgt} SELECT * FROM __src ON CONFLICT ({safe_key}) DO UPDATE SET {upd};"
        else:
            sql = f"INSERT INTO {tgt} SELECT * FROM __src;"
        con.execute(sql)
        print(f"\u2705 {count:,} rows -> {label}")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Parquet (atomic: write to temp, rename)
# ---------------------------------------------------------------------------
def writeback_to_parquet(*, duckdb_path, source_sql, output_path, partition_by, compression, dry_run):
    import duckdb

    h = _load_helpers()
    safe_path = h._validate_path(output_path, "output_path")
    safe_comp = h._validate_compression(compression)
    safe_parts = h._require_identifier_list(partition_by or [], "partition_by")
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        count = con.execute(f"SELECT COUNT(*) FROM ({source_sql})").fetchone()[0]
        if dry_run:
            print(f"\n[DRY RUN] {count:,} rows -> {safe_path}  compression={safe_comp}  partitions={safe_parts or 'none'}")
            return
        if count > LARGE_WRITE_THRESHOLD:
            _signoff(count, safe_path)
        tp = Path(safe_path)
        tp.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tp.parent) as tmp:
            tmp_out = str(Path(tmp) / tp.name)
            part = f", PARTITION_BY ({', '.join(safe_parts)}), OVERWRITE_OR_IGNORE" if safe_parts else ""
            con.execute(f"COPY ({source_sql}) TO '{h._sql_str_escape(tmp_out)}' (FORMAT parquet, COMPRESSION {safe_comp}{part});")
            if tp.exists() and tp.is_dir():
                shutil.rmtree(tp)
            shutil.move(tmp_out, safe_path)
        print(f"\u2705 {count:,} rows -> {safe_path}")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# DuckLake (new in v2.0) — INSERT or MERGE with full ACID
# ---------------------------------------------------------------------------
def writeback_to_ducklake(*, duckdb_path, source_sql, dl_catalog, dl_data_path, dl_schema, dl_table, dl_alias, conflict_key, dry_run):
    """
    Write rows into a DuckLake table using MERGE (upsert) or INSERT (append).

    The operation is wrapped in a DuckLake ACID transaction — either all rows
    commit as a single snapshot or none do.

    Security: all identifiers validated; dl_data_path null-byte checked;
    dl_catalog is not parameterisable (DuckLake URI syntax) — only trusted
    values should be passed.
    """
    import duckdb

    h = _load_helpers()
    safe_alias = h._require_identifier(dl_alias, "dl_alias")
    safe_schema = h._require_identifier(dl_schema, "dl_schema")
    safe_table = h._require_identifier(dl_table, "dl_table")
    safe_key = h._require_identifier(conflict_key, "conflict_key") if conflict_key else None
    h._validate_path(dl_data_path, "dl_data_path")

    con = duckdb.connect(duckdb_path, read_only=False)
    try:
        h.ensure_extension(con, "ducklake")
        con.execute(f"ATTACH 'ducklake:{dl_catalog}' AS {safe_alias} (DATA_PATH '{h._sql_str_escape(dl_data_path)}');")
        con.execute(f"CREATE TEMP TABLE __dl_src AS {source_sql};")
        count = con.execute("SELECT COUNT(*) FROM __dl_src").fetchone()[0]
        raw_cols = [r[0] for r in con.execute("DESCRIBE __dl_src").fetchall()]
        cols = h._require_identifier_list(raw_cols, "source columns")
        label = f"DuckLake {safe_alias}.{safe_schema}.{safe_table}"

        if dry_run:
            print(f"\n[DRY RUN] {count:,} rows -> {label}")
            print(f"  Catalog  : {dl_catalog}")
            print(f"  DataPath : {dl_data_path}")
            print(f"  Mode     : {'MERGE on ' + safe_key if safe_key else 'INSERT (append)'}")
            return
        if count > LARGE_WRITE_THRESHOLD:
            _signoff(count, label)

        tgt = f"{safe_alias}.{safe_schema}.{safe_table}"
        if safe_key:
            non_key = [c for c in cols if c != safe_key]
            upd_set = ", ".join(f"target.{c}=source.{c}" for c in non_key)
            ins_cols = ", ".join(cols)
            ins_vals = ", ".join(f"source.{c}" for c in cols)
            sql = f"MERGE INTO {tgt} AS target USING __dl_src AS source ON target.{safe_key}=source.{safe_key} WHEN MATCHED THEN UPDATE SET {upd_set} WHEN NOT MATCHED THEN INSERT ({ins_cols}) VALUES ({ins_vals});"
        else:
            sql = f"INSERT INTO {tgt} SELECT * FROM __dl_src;"
        con.execute(sql)

        snap_n = con.execute(f"SELECT COUNT(*) FROM {safe_alias}.snapshots()").fetchone()[0]
        print(f"\u2705 {count:,} rows -> {label}  (now {snap_n} snapshots)")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parser():
    p = argparse.ArgumentParser(
        description="DuckDB safe write-back — Postgres | Parquet | DuckLake",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--target", required=True, choices=["postgres", "parquet", "ducklake"])
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false")

    pg = p.add_argument_group("Postgres")
    pg.add_argument("--pg-host")
    pg.add_argument("--pg-port", type=int, default=5432)
    pg.add_argument("--pg-db")
    pg.add_argument("--pg-user")
    pg.add_argument("--pg-password")
    pg.add_argument("--pg-password-env", metavar="ENV_VAR")
    pg.add_argument("--pg-schema", default="public")
    pg.add_argument("--pg-table")
    pg.add_argument("--conflict-key")

    pq = p.add_argument_group("Parquet")
    pq.add_argument("--output-path")
    pq.add_argument("--partition-by", nargs="*")
    pq.add_argument("--compression", default="zstd", choices=["zstd", "snappy", "gzip", "none"])

    dl = p.add_argument_group("DuckLake")
    dl.add_argument("--dl-catalog")
    dl.add_argument("--dl-data-path")
    dl.add_argument("--dl-schema", default="main")
    dl.add_argument("--dl-table")
    dl.add_argument("--dl-alias", default="lake")
    dl.add_argument("--dl-conflict-key")
    return p


def _pg_password(args):
    if getattr(args, "pg_password_env", None):
        pw = os.environ.get(args.pg_password_env)
        if not pw:
            print("ERROR: pg_password_env not set", file=sys.stderr)
            sys.exit(1)
        return pw
    if getattr(args, "pg_password", None):
        return args.pg_password
    print("ERROR: need --pg-password or --pg-password-env", file=sys.stderr)
    sys.exit(1)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parser().parse_args()
    print(f"DRY RUN: {'ON' if args.dry_run else 'OFF'}  Target: {args.target.upper()}")

    if args.target == "postgres":
        for r in ("pg_host", "pg_db", "pg_user", "pg_table"):
            if not getattr(args, r, None):
                print(f"ERROR: --{r.replace('_', '-')} required", file=sys.stderr)
                sys.exit(1)
        writeback_to_postgres(
            duckdb_path=args.source,
            source_sql=args.query,
            host=args.pg_host,
            port=args.pg_port,
            dbname=args.pg_db,
            user=args.pg_user,
            password=_pg_password(args),
            pg_schema=args.pg_schema,
            pg_table=args.pg_table,
            conflict_key=getattr(args, "conflict_key", None),
            dry_run=args.dry_run,
        )
    elif args.target == "parquet":
        if not args.output_path:
            print("ERROR: --output-path required", file=sys.stderr)
            sys.exit(1)
        writeback_to_parquet(
            duckdb_path=args.source,
            source_sql=args.query,
            output_path=args.output_path,
            partition_by=args.partition_by,
            compression=args.compression,
            dry_run=args.dry_run,
        )
    elif args.target == "ducklake":
        for r in ("dl_catalog", "dl_data_path", "dl_table"):
            if not getattr(args, r, None):
                print(f"ERROR: --{r.replace('_', '-')} required", file=sys.stderr)
                sys.exit(1)
        writeback_to_ducklake(
            duckdb_path=args.source,
            source_sql=args.query,
            dl_catalog=args.dl_catalog,
            dl_data_path=args.dl_data_path,
            dl_schema=args.dl_schema,
            dl_table=args.dl_table,
            dl_alias=args.dl_alias,
            conflict_key=getattr(args, "dl_conflict_key", None),
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
