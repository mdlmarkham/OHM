#!/usr/bin/env python3
"""Rebuild OHM local database from DuckLake mirror.
Column name mapping between DuckLake (VARCHAR) and local schema.
"""

import sys
import os

DB_PATH = "/var/lib/ohm/ohm.duckdb"
LAKE_PATH = "/var/lib/ohm/ohm_lake.ducklake"

# Column mapping: DuckLake mirror column -> local schema column
# Only include columns that exist in both. DuckLake is all VARCHAR.
NODE_MAP = {
    "id": "id",
    "label": "label",
    "type": "type",
    "content": "content",
    "url": "url",
    "created_by": "created_by",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "updated_by": "updated_by",
    "confidence": "confidence",
    "visibility": "visibility",
    "provenance": "provenance",
    "tags": "tags",
    "metadata": "metadata",
    "priority": "priority",
    # Note: deleted_at not in DuckLake mirror (set NULL). embedding not in mirror.
}

EDGE_MAP = {
    "id": "id",
    "from_node": "from_node",
    "to_node": "to_node",
    "edge_type": "edge_type",
    "layer": "layer",
    "confidence": "confidence",
    "condition": "condition",
    "probability": "probability",
    "urgency": "urgency",
    "challenge_of": "challenge_of",
    "challenge_type": "challenge_type",
    "provenance": "provenance",
    "created_by": "created_by",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "updated_by": "updated_by",
    "metadata": "metadata",
}

OBS_MAP = {
    "id": "id",
    "node_id": "node_id",
    "edge_id": "edge_id",
    "type": "type",
    "value": "value",
    "baseline": "baseline",
    "sigma": "sigma",
    "source": "source",
    "created_by": "created_by",
    "created_at": "created_at",
    "metadata": "metadata",
    "notes": "notes",
    "source_name": "source_name",
    "source_url": "source_url",
    # sentiment not in DuckLake mirror - will be NULL
}


def rebuild():
    print("Rebuilding from DuckLake mirror...")

    # Remove corrupted DB
    for path in [DB_PATH, DB_PATH + ".wal"]:
        if os.path.exists(path):
            os.remove(path)
            print(f"Removed: {path}")

    import duckdb

    conn = duckdb.connect(DB_PATH)

    # Initialize schema
    sys.path.insert(0, "/tmp/OHM/src")
    from ohm.schema import initialize_schema

    initialize_schema(conn)
    print("Schema initialized")

    # Attach DuckLake
    try:
        conn.execute(f"ATTACH IF NOT EXISTS '{LAKE_PATH}' AS ohm_lake (TYPE ducklake)")
        print("DuckLake attached")
    except Exception as e:
        print(f"Failed to attach DuckLake: {e}")
        conn.close()
        return False

    # Pull nodes
    dl_cols = ", ".join(NODE_MAP.keys())
    local_cols = ", ".join(NODE_MAP.values())
    cast_cols = []
    for dl_col, local_col in NODE_MAP.items():
        if local_col in ("confidence",):
            cast_cols.append(f"CAST({dl_col} AS FLOAT) AS {local_col}")
        elif local_col in ("created_at", "updated_at"):
            cast_cols.append(f"CAST({dl_col} AS TIMESTAMP) AS {local_col}")
        else:
            cast_cols.append(f'"{dl_col}" AS {local_col}')
    select = ", ".join(cast_cols)

    try:
        conn.execute(f"""
            INSERT INTO ohm_nodes ({local_cols}, deleted_at)
            SELECT {select}, NULL::TIMESTAMP FROM ohm_lake.ohm_nodes
        """)
    except Exception as e:
        print(f"Bulk node insert failed: {e}")
        print("Trying row-by-row...")
        rows = conn.execute(f"SELECT {dl_cols} FROM ohm_lake.ohm_nodes").fetchall()
        col_names = list(NODE_MAP.values())
        inserted = 0
        for row in rows:
            try:
                placeholders = ", ".join(["?"] * len(col_names))
                # Cast types manually
                values = list(row)
                for i, (dl, local) in enumerate(NODE_MAP.items()):
                    if local == "confidence" and values[i] is not None:
                        try:
                            values[i] = float(values[i])
                        except Exception:
                            values[i] = 0.8
                    elif local in ("created_at", "updated_at") and values[i] is not None:
                        try:
                            values[i] = str(values[i])  # Let DuckDB handle the cast
                        except Exception:
                            pass
                conn.execute(f"INSERT INTO ohm_nodes ({local_cols}) VALUES ({placeholders})", values)
                inserted += 1
            except Exception as e2:
                if "Constraint" in str(e2) or "duplicate" in str(e2).lower():
                    pass  # Skip duplicates
                elif "foreign" in str(e2).lower():
                    pass  # Skip broken references
                else:
                    print(f"  Node insert error: {e2}")
        print(f"Inserted {inserted} nodes (row-by-row)")

    nodes = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
    print(f"Active nodes: {nodes}")

    # Pull edges (only where both endpoints exist)
    dl_cols = ", ".join(EDGE_MAP.keys())
    local_cols = ", ".join(EDGE_MAP.values())
    cast_cols = []
    for dl_col, local_col in EDGE_MAP.items():
        if local_col in ("confidence", "probability"):
            cast_cols.append(f"CAST({dl_col} AS FLOAT) AS {local_col}")
        elif local_col in ("created_at", "updated_at"):
            cast_cols.append(f"CAST({dl_col} AS TIMESTAMP) AS {local_col}")
        elif local_col == "metadata":
            cast_cols.append(f"CAST({dl_col} AS JSON) AS {local_col}")
        else:
            cast_cols.append(f'"{dl_col}" AS {local_col}')
    select = ", ".join(cast_cols)

    try:
        conn.execute(f"""
            INSERT INTO ohm_edges ({local_cols}, deleted_at)
            SELECT {select}, NULL::TIMESTAMP FROM ohm_lake.ohm_edges e
            WHERE EXISTS (SELECT 1 FROM ohm_nodes n WHERE n.id = e.from_node)
            AND EXISTS (SELECT 1 FROM ohm_nodes n WHERE n.id = e.to_node)
        """)
    except Exception as e:
        print(f"Edge insert error: {e}")

    edges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL").fetchone()[0]
    print(f"Active edges: {edges}")

    # Pull observations
    # For observations, skip columns that don't exist in DuckLake mirror
    # Check which columns actually exist
    try:
        dl_obs_cols = [r[0] for r in conn.execute("SELECT column_name FROM duckdb_columns() WHERE database_name = 'ohm_lake' AND table_name = 'ohm_observations'").fetchall()]
    except Exception:
        dl_obs_cols = []

    # Filter OBS_MAP to only include columns that exist in DuckLake
    filtered_obs_map = {k: v for k, v in OBS_MAP.items() if k in dl_obs_cols}
    dl_cols = ", ".join(filtered_obs_map.keys())
    local_cols = ", ".join(filtered_obs_map.values())
    cast_cols = []
    for dl_col, local_col in OBS_MAP.items():
        if local_col in ("value", "sigma", "baseline"):
            cast_cols.append(f"CAST({dl_col} AS FLOAT) AS {local_col}")
        elif local_col in ("created_at",):
            cast_cols.append(f"CAST({dl_col} AS TIMESTAMP) AS {local_col}")
        elif local_col == "metadata":
            cast_cols.append(f"CAST({dl_col} AS JSON) AS {local_col}")
        else:
            cast_cols.append(f'"{dl_col}" AS {local_col}')
    select = ", ".join(cast_cols)

    try:
        conn.execute(f"""
            INSERT INTO ohm_observations ({local_cols}, deleted_at)
            SELECT {select}, NULL::TIMESTAMP FROM ohm_lake.ohm_observations o
            WHERE EXISTS (SELECT 1 FROM ohm_nodes n WHERE n.id = o.node_id)
        """)
    except Exception as e:
        print(f"Observation insert error: {e}")

    obs = conn.execute("SELECT COUNT(*) FROM ohm_observations").fetchone()[0]
    print(f"Observations: {obs}")

    # Detach DuckLake
    try:
        conn.execute("DETACH ohm_lake")
    except Exception:
        pass

    # Checkpoint
    conn.execute("CHECKPOINT")

    # Final stats
    nodes = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL").fetchone()[0]
    obs = conn.execute("SELECT COUNT(*) FROM ohm_observations").fetchone()[0]
    print(f"\nRebuild complete: {nodes} nodes, {edges} edges, {obs} observations")

    conn.close()
    return True


if __name__ == "__main__":
    sys.exit(0 if rebuild() else 1)
