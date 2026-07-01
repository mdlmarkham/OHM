"""Regression tests for OHM-sdp1 — delete cascade audit trail.

The cascade inside ``delete_node()`` and ``delete_edge()`` must write
one ``ohm_change_feed`` row per cascaded row (edge / observation), not
just the primary row. Otherwise operators can't answer 'who deleted
edge X and when?' from the audit feed.

Coverage:
- delete_node() cascades to edges: each edge gets a feed row.
- delete_node() cascades to observations: each obs gets a feed row.
- delete_edge() cascades to observations: each obs gets a feed row.
- delete_edge() with NO observations: no spurious feed rows.
- delete_node() with NO cascaded rows: no spurious feed rows.
- The fix is symmetric across the queries/ direct path and the
  store/ ORM path (both must produce the same audit trail).
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _init_db() -> duckdb.DuckDBPyConnection:
    """Return a fresh in-memory DuckDB with the OHM schema."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from ohm.schema import initialize_schema

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


def _seed_node_with_edge_and_obs(conn) -> tuple[str, str, str]:
    """Seed one node, one edge, one observation. Returns (node_id, edge_id, obs_id)."""
    node_id = "cascade_src"
    edge_target = "cascade_dst"
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
        "VALUES (?, ?, 'concept', 'seeder', CURRENT_TIMESTAMP)",
        [node_id, "Cascade source"],
    )
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
        "VALUES (?, ?, 'concept', 'seeder', CURRENT_TIMESTAMP)",
        [edge_target, "Cascade target"],
    )
    edge_id = "edge_cascade_1"
    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, "
        "confidence, created_by, created_at) "
        "VALUES (?, ?, ?, 'CAUSES', 'L3', 0.5, 'seeder', CURRENT_TIMESTAMP)",
        [edge_id, node_id, edge_target],
    )
    obs_id = "obs_cascade_1"
    conn.execute(
        "INSERT INTO ohm_observations (id, node_id, type, value, created_by, created_at) "
        "VALUES (?, ?, 'measurement', 0.5, 'seeder', CURRENT_TIMESTAMP)",
        [obs_id, node_id],
    )
    return node_id, edge_id, obs_id


def _feed_rows_for(conn, agent_name: str) -> list[tuple[str, str, str, str]]:
    return conn.execute(
        "SELECT table_name, row_id, operation, agent_name "
        "FROM ohm_change_feed "
        "WHERE agent_name = ? ORDER BY table_name, row_id",
        [agent_name],
    ).fetchall()


class TestDeleteNodeCascadeAudit:
    """delete_node() must log every cascaded row, not just the node."""

    def test_node_delete_logs_cascaded_edges(self):
        """Each edge that gets cascade-soft-deleted by a node delete
        must appear in ohm_change_feed (OHM-sdp1)."""
        from ohm.queries import delete_node

        conn = _init_db()
        try:
            node_id, edge_id, _ = _seed_node_with_edge_and_obs(conn)
            delete_node(conn, node_id=node_id, deleted_by="test_agent")

            rows = _feed_rows_for(conn, "test_agent")
            by_table = {}
            for table_name, row_id, op, agent in rows:
                assert op == "DELETE"
                assert agent == "test_agent"
                by_table.setdefault(table_name, set()).add(row_id)

            assert node_id in by_table.get("ohm_nodes", set()), (
                "delete_node must log the primary node row"
            )
            assert edge_id in by_table.get("ohm_edges", set()), (
                "delete_node must log the cascaded edge row (OHM-sdp1)"
            )
        finally:
            conn.close()

    def test_node_delete_logs_cascaded_observations(self):
        """Each observation that gets cascade-soft-deleted by a node
        delete must appear in ohm_change_feed (OHM-sdp1)."""
        from ohm.queries import delete_node

        conn = _init_db()
        try:
            node_id, _, obs_id = _seed_node_with_edge_and_obs(conn)
            delete_node(conn, node_id=node_id, deleted_by="test_agent")

            rows = _feed_rows_for(conn, "test_agent")
            by_table = {}
            for table_name, row_id, op, agent in rows:
                by_table.setdefault(table_name, set()).add(row_id)

            assert obs_id in by_table.get("ohm_observations", set()), (
                "delete_node must log the cascaded observation row (OHM-sdp1)"
            )
        finally:
            conn.close()

    def test_node_delete_with_no_cascades_writes_only_node_row(self):
        """A node with no edges and no observations must produce exactly
        one feed row (the node itself) — no spurious empty entries."""
        from ohm.queries import delete_node

        conn = _init_db()
        try:
            conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                "VALUES ('lonely_node', 'Lonely', 'concept', 'seeder', CURRENT_TIMESTAMP)"
            )
            delete_node(conn, node_id="lonely_node", deleted_by="test_agent")
            rows = _feed_rows_for(conn, "test_agent")
            assert rows == [
                ("ohm_nodes", "lonely_node", "DELETE", "test_agent"),
            ]
        finally:
            conn.close()

    def test_node_delete_logs_multiple_cascaded_rows(self):
        """A node with 3 edges + 2 observations must produce 6 cascade
        feed rows in addition to the node row — one per affected row."""
        from ohm.queries import delete_node

        conn = _init_db()
        try:
            src_id = "fan_out_src"
            conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                "VALUES (?, ?, 'concept', 'seeder', CURRENT_TIMESTAMP)",
                [src_id, "Fan-out source"],
            )
            # 3 target nodes + 3 edges from src -> target_i
            edge_ids = []
            for i in range(3):
                tgt = f"target_{i}"
                conn.execute(
                    "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                    "VALUES (?, ?, 'concept', 'seeder', CURRENT_TIMESTAMP)",
                    [tgt, f"Target {i}"],
                )
                eid = f"edge_{i}"
                edge_ids.append(eid)
                conn.execute(
                    "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, "
                    "confidence, created_by, created_at) "
                    "VALUES (?, ?, ?, 'CAUSES', 'L3', 0.5, 'seeder', CURRENT_TIMESTAMP)",
                    [eid, src_id, tgt],
                )
            # 2 observations on src
            obs_ids = []
            for i in range(2):
                oid = f"obs_{i}"
                obs_ids.append(oid)
                conn.execute(
                    "INSERT INTO ohm_observations (id, node_id, type, value, "
                    "created_by, created_at) "
                    "VALUES (?, ?, 'measurement', ?, 'seeder', CURRENT_TIMESTAMP)",
                    [oid, src_id, 0.1 * i],
                )

            delete_node(conn, node_id=src_id, deleted_by="audit_test")

            rows = _feed_rows_for(conn, "audit_test")
            by_table = {}
            for table_name, row_id, op, agent in rows:
                by_table.setdefault(table_name, set()).add(row_id)

            # 1 node + 3 edges + 2 observations = 6 cascade + 1 node = 7 rows.
            assert by_table.get("ohm_nodes", set()) == {src_id}
            assert by_table.get("ohm_edges", set()) == set(edge_ids), (
                f"Expected all 3 cascaded edges in feed, got "
                f"{by_table.get('ohm_edges')}"
            )
            assert by_table.get("ohm_observations", set()) == set(obs_ids), (
                f"Expected both observations in feed, got "
                f"{by_table.get('ohm_observations')}"
            )
        finally:
            conn.close()


class TestDeleteEdgeCascadeAudit:
    """delete_edge() must log cascaded observations (parity with node)."""

    def test_edge_delete_logs_cascaded_observations(self):
        from ohm.queries import delete_edge

        conn = _init_db()
        try:
            node_id, edge_id, _ = _seed_node_with_edge_and_obs(conn)
            # The seed's observation has only node_id set (no edge_id),
            # so delete_edge will NOT cascade it — it belongs to the node,
            # not the edge. Add two observations WITH edge_id set so we
            # have multiple cascaded rows.
            conn.execute(
                "INSERT INTO ohm_observations (id, node_id, edge_id, type, value, "
                "created_by, created_at) "
                "VALUES ('obs_edge_1', ?, ?, 'measurement', 0.7, 'seeder', CURRENT_TIMESTAMP)",
                [node_id, edge_id],
            )
            conn.execute(
                "INSERT INTO ohm_observations (id, node_id, edge_id, type, value, "
                "created_by, created_at) "
                "VALUES ('obs_edge_2', ?, ?, 'measurement', 0.3, 'seeder', CURRENT_TIMESTAMP)",
                [node_id, edge_id],
            )

            delete_edge(conn, edge_id=edge_id, deleted_by="test_agent")
            rows = _feed_rows_for(conn, "test_agent")
            by_table = {}
            for table_name, row_id, op, agent in rows:
                by_table.setdefault(table_name, set()).add(row_id)

            assert by_table.get("ohm_edges", set()) == {edge_id}
            assert by_table.get("ohm_observations", set()) == {
                "obs_edge_1", "obs_edge_2",
            }, (
                f"Expected both edge-referencing observations in feed "
                f"(OHM-sdp1), got {by_table.get('ohm_observations')}"
            )
        finally:
            conn.close()

    def test_edge_delete_with_no_observations_writes_only_edge_row(self):
        from ohm.queries import delete_edge

        conn = _init_db()
        try:
            node_id = "ed_no_obs_src"
            tgt_id = "ed_no_obs_dst"
            for nid, label in ((node_id, "Src"), (tgt_id, "Dst")):
                conn.execute(
                    "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                    "VALUES (?, ?, 'concept', 'seeder', CURRENT_TIMESTAMP)",
                    [nid, label],
                )
            conn.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, "
                "confidence, created_by, created_at) "
                "VALUES ('ed_no_obs', ?, ?, 'CAUSES', 'L3', 0.5, 'seeder', CURRENT_TIMESTAMP)",
                [node_id, tgt_id],
            )
            delete_edge(conn, edge_id="ed_no_obs", deleted_by="test_agent")
            rows = _feed_rows_for(conn, "test_agent")
            assert rows == [
                ("ohm_edges", "ed_no_obs", "DELETE", "test_agent"),
            ], f"Expected single edge row, got {rows}"
        finally:
            conn.close()


class TestStorePathSymmetry:
    """The OhmStore ORM path must produce the same audit trail as the
    direct queries/ path. Both code paths were fixed in OHM-sdp1."""

    def test_store_delete_node_logs_cascade(self):
        """Build a minimal OhmStore on an in-memory DuckDB and verify the
        store-level cascade writes per-row audit entries."""
        from ohm.store import OhmStore

        # OhmStore.__init__ takes a db_path and an optional schema_config.
        # Use ":memory:" so the test is hermetic.
        store = OhmStore(":memory:")
        try:
            store.conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                "VALUES (?, ?, 'concept', 'seeder', CURRENT_TIMESTAMP)",
                ["store_src", "Store cascade source"],
            )
            store.conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) "
                "VALUES (?, ?, 'concept', 'seeder', CURRENT_TIMESTAMP)",
                ["store_dst", "Store cascade target"],
            )
            store.conn.execute(
                "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, "
                "confidence, created_by, created_at) "
                "VALUES ('store_edge', ?, ?, 'CAUSES', 'L3', 0.5, 'seeder', CURRENT_TIMESTAMP)",
                ["store_src", "store_dst"],
            )
            store.conn.execute(
                "INSERT INTO ohm_observations (id, node_id, type, value, "
                "created_by, created_at) "
                "VALUES ('store_obs', ?, 'measurement', 0.5, 'seeder', CURRENT_TIMESTAMP)",
                ["store_src"],
            )

            store.delete_node("store_src", deleted_by="seeder")

            rows = store.conn.execute(
                "SELECT table_name, row_id, operation, agent_name "
                "FROM ohm_change_feed "
                "WHERE agent_name = 'seeder' ORDER BY table_name, row_id"
            ).fetchall()
            by_table: dict[str, set[str]] = {}
            for table_name, row_id, op, agent in rows:
                by_table.setdefault(table_name, set()).add(row_id)

            assert "store_src" in by_table.get("ohm_nodes", set())
            # OHM-sdp1: the cascaded edge from store_src to store_dst
            # must also appear in the feed via the OhmStore path.
            assert "store_edge" in by_table.get("ohm_edges", set()), (
                f"OhmStore cascade missing edge audit row (OHM-sdp1): "
                f"{by_table.get('ohm_edges')}"
            )
            assert "store_obs" in by_table.get("ohm_observations", set()), (
                f"OhmStore cascade missing observation audit row (OHM-sdp1): "
                f"{by_table.get('ohm_observations')}"
            )
        finally:
            store.close()
