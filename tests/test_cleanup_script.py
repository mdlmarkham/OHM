"""Regression test for scripts/cleanup_test_artifacts.py (OHM-kg16 item 1).

The cleanup script soft-deletes test artifacts (e.g., the metis_test_*
nodes left by the 2026-06-30 live daemon test) from a live or temp
DuckDB. This test verifies the script:

- dry-run reports candidates and does NOT modify the DB
- the real run soft-deletes matching nodes AND cascades to edges
  (including edges that touch a deleted node via real_node_X — the
  edge becomes meaningless when one endpoint is gone)
- --deleted-by is plumbed through (verified via the summary JSON's
  absence of error)
- re-running is idempotent (reports "Nothing to do")
- --ids explicit-list path works, including graceful handling of
  nonexistent ids (no crash, error reported per-id)

We use a temp file DuckDB (not :memory:) because the script opens a
new connection and :memory: connections don't share state across
subprocesses.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "cleanup_test_artifacts.py"


def _seed_db(path: Path) -> None:
    """Seed a fresh DuckDB at ``path`` with three test nodes, three real
    nodes, and two edges (one between test nodes, one between a real
    node and a test node)."""
    if path.exists():
        path.unlink()
    conn = duckdb.connect(str(path))
    try:
        # Initialize via the production schema helper.
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.schema import initialize_schema

        initialize_schema(conn)

        for i in range(3):
            conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                "VALUES (?, ?, 'concept', 'metis', CURRENT_TIMESTAMP)",
                [f"metis_test_{i:03d}", f"Metis test {i}"],
            )
        for i in range(3):
            conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                "VALUES (?, ?, 'concept', 'metis', CURRENT_TIMESTAMP)",
                [f"real_node_{i}", f"Real node {i}"],
            )
        # Edge between two test nodes — must be cleaned up.
        conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, "
            "confidence, created_by, created_at) "
            "VALUES ('edge_1', 'metis_test_000', 'metis_test_001', 'CAUSES', "
            "'L3', 0.5, 'metis', CURRENT_TIMESTAMP)"
        )
        # Edge from real_node to test_node — endpoint goes away, must
        # also be cleaned up by the cascade.
        conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, "
            "confidence, created_by, created_at) "
            "VALUES ('edge_2', 'real_node_0', 'metis_test_002', 'CAUSES', "
            "'L3', 0.5, 'metis', CURRENT_TIMESTAMP)"
        )
    finally:
        conn.close()


def _run_script(*args: str) -> subprocess.CompletedProcess:
    """Invoke the cleanup script with the given args and capture output."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _active_ids(conn, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT id FROM {table} WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()}


def _deleted_ids(conn, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        f"SELECT id FROM {table} WHERE deleted_at IS NOT NULL ORDER BY id"
    ).fetchall()}


@pytest.fixture
def seeded_db(tmp_path):
    """Yield a fresh temp DuckDB seeded with metis_test_* + real_node_* + edges."""
    db_path = tmp_path / "cleanup_test.duckdb"
    _seed_db(db_path)
    yield db_path
    if db_path.exists():
        db_path.unlink()


class TestCleanupScriptDryRun:
    """``--dry-run`` must print candidates and not modify the DB."""

    def test_dry_run_reports_candidates(self, seeded_db):
        r = _run_script("--db-path", str(seeded_db), "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "metis_test_000" in r.stdout
        assert "metis_test_001" in r.stdout
        assert "metis_test_002" in r.stdout
        assert "DRY-RUN" in r.stdout

    def test_dry_run_does_not_modify_db(self, seeded_db):
        _run_script("--db-path", str(seeded_db), "--dry-run")
        conn = duckdb.connect(str(seeded_db), read_only=True)
        try:
            assert _active_ids(conn, "ohm_nodes") == {
                "metis_test_000", "metis_test_001", "metis_test_002",
                "real_node_0", "real_node_1", "real_node_2",
            }
            assert _active_ids(conn, "ohm_edges") == {"edge_1", "edge_2"}
        finally:
            conn.close()


class TestCleanupScriptApply:
    """Real run soft-deletes matching nodes AND cascades to edges."""

    def test_apply_deletes_matching_nodes(self, seeded_db):
        r = _run_script(
            "--db-path", str(seeded_db), "--deleted-by", "test_cleanup"
        )
        assert r.returncode == 0, r.stderr

        # Parse the JSON summary printed at the end.
        summary_start = r.stdout.index("{")
        summary = json.loads(r.stdout[summary_start:])
        assert sorted(summary["deleted_nodes"]) == [
            "metis_test_000", "metis_test_001", "metis_test_002",
        ]
        assert summary["edges_removed"] == 2

    def test_apply_writes_audit_trail(self, seeded_db):
        """The ``--deleted-by`` value must reach ``ohm_change_feed`` so the
        operator can answer 'who deleted this node?' from the audit log.
        Without this, the cleanup is anonymous — exactly the kind of gap
        that breaks accountability in production."""
        _run_script(
            "--db-path", str(seeded_db), "--deleted-by", "ops_audit_test"
        )

        conn = duckdb.connect(str(seeded_db), read_only=True)
        try:
            rows = conn.execute(
                "SELECT table_name, row_id, operation, agent_name "
                "FROM ohm_change_feed "
                "WHERE operation = 'DELETE' AND agent_name = 'ops_audit_test' "
                "ORDER BY row_id"
            ).fetchall()
        finally:
            conn.close()

        deleted_ids = {r[1] for r in rows}
        # The 3 test nodes are logged. NOTE: the cascade to edges does
        # NOT write to ohm_change_feed today — only the node row does.
        # See ``scripts/cleanup_test_artifacts.py`` issue tracker for
        # the audit-gap followup (cascade should log edge deletes).
        assert deleted_ids == {
            "metis_test_000", "metis_test_001", "metis_test_002",
        }, f"Audit feed missing node rows: {deleted_ids}"
        # And every row carries our agent name.
        assert all(r[3] == "ops_audit_test" for r in rows)

    def test_apply_cascades_to_edges(self, seeded_db):
        """Edges touching a deleted node (both endpoints in the test set,
        AND edges from real_node_X to a test_node) must also be soft-deleted
        by the cascade — otherwise real_node_X would carry a dangling
        edge to a tombstoned node."""
        _run_script("--db-path", str(seeded_db))

        conn = duckdb.connect(str(seeded_db), read_only=True)
        try:
            # Real nodes survive.
            assert _active_ids(conn, "ohm_nodes") == {
                "real_node_0", "real_node_1", "real_node_2",
            }
            # Test nodes are soft-deleted.
            assert _deleted_ids(conn, "ohm_nodes") == {
                "metis_test_000", "metis_test_001", "metis_test_002",
            }
            # Both edges cascade (edge_1 = test↔test, edge_2 = real→test).
            assert _deleted_ids(conn, "ohm_edges") == {"edge_1", "edge_2"}
            assert _active_ids(conn, "ohm_edges") == set()
        finally:
            conn.close()

    def test_apply_is_idempotent(self, seeded_db):
        """Re-running after a successful cleanup must be a no-op —
        finds no candidates, returns 0, prints 'Nothing to do'."""
        _run_script("--db-path", str(seeded_db))
        r2 = _run_script("--db-path", str(seeded_db))
        assert r2.returncode == 0
        assert "Nothing to do" in r2.stdout


class TestCleanupScriptExplicitIds:
    """``--ids`` overrides ``--pattern`` and handles per-node errors."""

    def test_ids_path_deletes_named_nodes(self, tmp_path):
        db_path = tmp_path / "explicit_ids.duckdb"
        _seed_db(db_path)
        try:
            r = _run_script(
                "--db-path", str(db_path),
                "--ids", "metis_test_000,metis_test_001",
            )
            assert r.returncode == 0, r.stderr
            assert "metis_test_000" in r.stdout
            assert "metis_test_001" in r.stdout

            conn = duckdb.connect(str(db_path), read_only=True)
            try:
                # metis_test_002 was NOT named, so it survives.
                assert "metis_test_002" in _active_ids(conn, "ohm_nodes")
            finally:
                conn.close()
        finally:
            if db_path.exists():
                db_path.unlink()

    def test_ids_path_reports_nonexistent_without_crashing(self, tmp_path):
        """A nonexistent id must be reported in the ``failed`` array;
        the script must NOT exit non-zero because of one bad id."""
        db_path = tmp_path / "nonexistent_ids.duckdb"
        _seed_db(db_path)
        try:
            r = _run_script(
                "--db-path", str(db_path),
                "--ids", "metis_test_000,does_not_exist",
            )
            assert r.returncode == 0, (
                f"Script must exit 0 even when a named id is missing. "
                f"STDERR: {r.stderr}"
            )
            summary_start = r.stdout.index("{")
            summary = json.loads(r.stdout[summary_start:])
            assert "metis_test_000" in summary["deleted_nodes"]
            assert any(
                f["id"] == "does_not_exist" for f in summary.get("failed", [])
            ), f"Expected 'does_not_exist' in failed list: {summary}"
        finally:
            if db_path.exists():
                db_path.unlink()


class TestCleanupScriptPattern:
    """Custom ``--pattern`` overrides the default metis_test_% match."""

    def test_custom_pattern(self, tmp_path):
        db_path = tmp_path / "custom_pattern.duckdb"
        _seed_db(db_path)
        try:
            r = _run_script(
                "--db-path", str(db_path),
                "--pattern", "real_node_%",
                "--dry-run",
            )
            assert r.returncode == 0, r.stderr
            assert "real_node_0" in r.stdout
            assert "real_node_1" in r.stdout
            # Test nodes do NOT match the custom pattern.
            assert "metis_test_000" not in r.stdout
        finally:
            if db_path.exists():
                db_path.unlink()
