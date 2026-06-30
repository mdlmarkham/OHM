"""Tests for scripts/cleanup_test_artifacts.py — live DB test-artifact cleanup.

Strategy: run the script's main() in-process (not via subprocess) so we
don't hit Windows file-lock contention with DuckDB's single-writer model.
The script is a thin argparse wrapper around ohm.queries.delete_node; we
test that wrapper plus the cascade semantics end-to-end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Import after sys.path is set up so the script's REPO_ROOT manipulation works
import cleanup_test_artifacts as cta  # noqa: E402


@pytest.fixture
def seeded_db(tmp_path):
    """Create a fresh on-disk DuckDB with the schema and seed test artifacts."""
    import duckdb

    from ohm.schema import initialize_schema
    from ohm.queries import create_node, create_edge

    db_path = str(tmp_path / "cleanup.duckdb")
    conn = duckdb.connect(db_path)
    initialize_schema(conn)

    # 3 test nodes (label LIKE 'Test %') + 1 unrelated production node
    create_node(conn, label="Test A", node_type="concept", created_by="metis")
    create_node(conn, label="Test B", node_type="concept", created_by="metis")
    create_node(conn, label="Real production claim", node_type="concept", created_by="metis")
    create_node(conn, label="Other agent", node_type="concept", created_by="clio")

    # Edge between the two test nodes — verifies cascade
    rows = conn.execute(
        "SELECT id FROM ohm_nodes WHERE label = 'Test A' AND deleted_at IS NULL"
    ).fetchall()
    test_a_id = rows[0][0]
    rows = conn.execute(
        "SELECT id FROM ohm_nodes WHERE label = 'Test B' AND deleted_at IS NULL"
    ).fetchall()
    test_b_id = rows[0][0]
    create_edge(
        conn,
        from_node=test_a_id,
        to_node=test_b_id,
        layer="L3",
        edge_type="CAUSES",
        created_by="metis",
    )

    # Force CHECKPOINT and close so the file is released before main() opens it
    try:
        conn.execute("CHECKPOINT")
    except Exception:
        pass
    conn.close()
    return db_path


def _live_test_count(db_path: str, like: str = "Test %") -> int:
    import duckdb

    conn = duckdb.connect(db_path, read_only=True)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE label LIKE ? AND deleted_at IS NULL",
            [like],
        ).fetchone()
        return count
    finally:
        conn.close()


def _edge_count(db_path: str) -> int:
    import duckdb

    conn = duckdb.connect(db_path, read_only=True)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL"
        ).fetchone()
        return count
    finally:
        conn.close()


def _node_id_by_label(db_path: str, label: str) -> str:
    import duckdb

    conn = duckdb.connect(db_path, read_only=True)
    try:
        (nid,) = conn.execute(
            "SELECT id FROM ohm_nodes WHERE label = ? AND deleted_at IS NULL", [label]
        ).fetchone()
        return nid
    finally:
        conn.close()


class TestCleanupDryRun:
    def test_finds_matches(self, seeded_db, capsys):
        rc = cta.main(["--db-path", seeded_db, "--dry-run", "--pattern", "Test %"])
        assert rc == 0
        captured = capsys.readouterr()
        # The script auto-generates ids from label+type, so verify by id prefix.
        assert "concept-test_a_" in captured.out
        assert "concept-test_b_" in captured.out
        assert "DRY-RUN" in captured.out

    def test_dry_run_does_not_modify(self, seeded_db):
        cta.main(["--db-path", seeded_db, "--dry-run", "--pattern", "Test %"])
        assert _live_test_count(seeded_db) == 2


class TestCleanupApply:
    def test_deletes_matching_nodes(self, seeded_db, capsys):
        rc = cta.main(["--db-path", seeded_db, "--pattern", "Test %"])
        assert rc == 0
        captured = capsys.readouterr()
        # No more live test nodes
        assert _live_test_count(seeded_db) == 0
        # Production node preserved
        import duckdb
        conn = duckdb.connect(seeded_db, read_only=True)
        try:
            (alive,) = conn.execute(
                "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL"
            ).fetchone()
            assert alive == 2  # Real production claim + Other agent
        finally:
            conn.close()
        # Summary block printed
        assert '"deleted_nodes"' in captured.out

    def test_cascades_to_edges(self, seeded_db):
        cta.main(["--db-path", seeded_db, "--pattern", "Test %"])
        assert _edge_count(seeded_db) == 0

    def test_explicit_ids_overrides_pattern(self, seeded_db):
        prod_id = _node_id_by_label(seeded_db, "Real production claim")
        # Use --ids with a single production node, expect Test A/B to survive
        rc = cta.main(["--db-path", seeded_db, "--ids", prod_id])
        assert rc == 0
        # Production node gone
        assert _live_test_count(seeded_db, "Real production claim") == 0
        # Test nodes untouched
        assert _live_test_count(seeded_db, "Test %") == 2

    def test_no_matches_exits_zero(self, seeded_db, capsys):
        rc = cta.main(["--db-path", seeded_db, "--pattern", "no_such_node_%"])
        assert rc == 0
        assert "Nothing to do" in capsys.readouterr().out

    def test_summary_contains_deleted_ids(self, seeded_db, capsys):
        rc = cta.main(["--db-path", seeded_db, "--pattern", "Test %"])
        assert rc == 0
        captured = capsys.readouterr()
        start = captured.out.find("{")
        assert start != -1
        summary = json.loads(captured.out[start:])
        assert "deleted_nodes" in summary
        assert len(summary["deleted_nodes"]) == 2
        assert summary["edges_removed"] >= 1
