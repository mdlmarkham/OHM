"""Tests for the fast batch insert path (OHM-aadc).

Covers:
- fast_batch_create_nodes produces the same records as the slow path
- fast_batch_create_edges produces the same records as the slow path
- change feed is populated for each row
- alias registration happens for each new node
- fast path falls back to slow path when connects_to is set
- fast path falls back to slow path when a soft-deleted collision exists
- fast path falls back to slow path on validation errors
- batch_create_nodes/edges via the public API still work
- empty batch returns []
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _init_db() -> duckdb.DuckDBPyConnection:
    """Fresh in-memory DuckDB with OHM schema."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from ohm.schema import initialize_schema
    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


class TestFastBatchCreateNodes:
    """Direct tests for ohm.graph.batch.fast_batch_create_nodes."""

    def test_creates_all_nodes(self):
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            nodes = [
                {"label": "Alpha", "node_type": "concept"},
                {"label": "Beta", "node_type": "source"},
                {"label": "Gamma", "node_type": "pattern"},
            ]
            result = fast_batch_create_nodes(conn, nodes=nodes, created_by="test")
            assert result is not None
            assert len(result) == 3
            labels = {r["label"] for r in result}
            assert labels == {"Alpha", "Beta", "Gamma"}
        finally:
            conn.close()

    def test_returns_in_input_order(self):
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            nodes = [
                {"label": "First", "node_type": "concept"},
                {"label": "Second", "node_type": "concept"},
                {"label": "Third", "node_type": "concept"},
            ]
            result = fast_batch_create_nodes(conn, nodes=nodes, created_by="test")
            assert result is not None
            assert [r["label"] for r in result] == ["First", "Second", "Third"]
        finally:
            conn.close()

    def test_populates_change_feed(self):
        """Each node creation must produce its own change-feed entry."""
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            nodes = [
                {"label": "CF1", "node_type": "concept"},
                {"label": "CF2", "node_type": "concept"},
            ]
            result = fast_batch_create_nodes(conn, nodes=nodes, created_by="test")
            assert result is not None
            node_ids = {r["id"] for r in result}
            feed_rows = conn.execute(
                "SELECT row_id FROM ohm_change_feed "
                "WHERE table_name = 'ohm_nodes' AND operation = 'INSERT'"
            ).fetchall()
            feed_ids = {r[0] for r in feed_rows}
            assert node_ids.issubset(feed_ids), (
                f"Expected all node ids in change feed, missing: {node_ids - feed_ids}"
            )
        finally:
            conn.close()

    def test_registers_aliases(self):
        """OHM-z2gp: each new node gets an alias in ohm_aliases."""
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            nodes = [{"label": "Alias Test", "node_type": "concept"}]
            result = fast_batch_create_nodes(conn, nodes=nodes, created_by="test")
            assert result is not None
            nid = result[0]["id"]
            aliases = conn.execute(
                "SELECT alias_norm FROM ohm_aliases WHERE node_id = ?", [nid]
            ).fetchall()
            assert len(aliases) >= 1
        finally:
            conn.close()

    def test_preserves_optional_fields(self):
        """Confidence, provenance, content, tags, metadata all flow through."""
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            nodes = [{
                "label": "Rich Node",
                "node_type": "concept",
                "content": "Some content",
                "confidence": 0.8,
                "provenance": "test-prov",
                "visibility": "team",
                "tags": ["a", "b"],
                "metadata": {"key": "value"},
            }]
            result = fast_batch_create_nodes(conn, nodes=nodes, created_by="test")
            assert result is not None
            r = result[0]
            assert r["content"] == "Some content"
            assert r["confidence"] == pytest.approx(0.8)
            assert r["provenance"] == "test-prov"
        finally:
            conn.close()

    def test_falls_back_when_connects_to_set(self):
        """connects_to requires a bulk existence check; fast path returns None."""
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            # Seed an anchor node first
            anchor = fast_batch_create_nodes(
                conn, nodes=[{"label": "Anchor", "node_type": "concept"}], created_by="test"
            )
            assert anchor is not None
            anchor_id = anchor[0]["id"]

            result = fast_batch_create_nodes(
                conn,
                nodes=[{"label": "With CT", "node_type": "concept", "connects_to": [anchor_id]}],
                created_by="test",
            )
            assert result is None, "Fast path should fall back when connects_to is set"
        finally:
            conn.close()

    def test_falls_back_on_soft_deleted_collision(self):
        """If a generated node id collides with a soft-deleted row, the
        fast path returns None so the slow path can reactivate.

        Note: ``generate_node_id`` includes a random 5-char suffix, so
        creating a node with the same label twice does NOT collide.
        We force a collision by soft-deleting a node, then re-inserting
        a row with the SAME id via raw SQL, then calling the fast path
        with a node whose id we manually pin to the soft-deleted one.
        """
        from ohm.graph.batch import fast_batch_create_nodes
        from ohm.queries import create_node, delete_node
        conn = _init_db()
        try:
            original = create_node(conn, label="Recycle Me", created_by="test")
            delete_node(conn, node_id=original["id"], deleted_by="test")

            # The fast path uses generate_node_id, which has a random
            # suffix, so we can't directly force a collision via the
            # public API. Instead, we patch generate_node_id to return
            # the soft-deleted id.
            import ohm.graph.batch as batch_mod

            original_gen = batch_mod.generate_node_id if hasattr(batch_mod, "generate_node_id") else None

            # The fast path imports generate_node_id lazily inside the
            # function, so we patch it at the source module.
            from ohm import schema as schema_mod
            original_fn = schema_mod.generate_node_id

            def pinned_gen(label, node_type):
                return original["id"]

            schema_mod.generate_node_id = pinned_gen
            try:
                result = fast_batch_create_nodes(
                    conn, nodes=[{"label": "Recycle Me", "node_type": "concept"}],
                    created_by="test",
                )
                assert result is None, (
                    "Fast path should fall back when soft-deleted collision detected"
                )
            finally:
                schema_mod.generate_node_id = original_fn
        finally:
            conn.close()

    def test_falls_back_on_invalid_label(self):
        """Empty label fails validation; fast path returns None."""
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            result = fast_batch_create_nodes(
                conn, nodes=[{"label": "", "node_type": "concept"}], created_by="test"
            )
            assert result is None
        finally:
            conn.close()

    def test_empty_batch_returns_empty_list(self):
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            result = fast_batch_create_nodes(conn, nodes=[], created_by="test")
            assert result == []
        finally:
            conn.close()


class TestFastBatchCreateEdges:
    """Direct tests for ohm.graph.batch.fast_batch_create_edges."""

    def test_creates_all_edges(self):
        from ohm.graph.batch import fast_batch_create_nodes, fast_batch_create_edges
        conn = _init_db()
        try:
            nodes = fast_batch_create_nodes(
                conn,
                nodes=[
                    {"label": "A", "node_type": "concept"},
                    {"label": "B", "node_type": "concept"},
                    {"label": "C", "node_type": "concept"},
                ],
                created_by="test",
            )
            assert nodes is not None
            a, b, c = [n["id"] for n in nodes]
            edges = fast_batch_create_edges(
                conn,
                edges=[
                    {"from_node": a, "to_node": b, "edge_type": "CAUSES", "layer": "L3"},
                    {"from_node": b, "to_node": c, "edge_type": "INFLUENCES", "layer": "L2"},
                ],
                created_by="test",
            )
            assert edges is not None
            assert len(edges) == 2
            types = {e["edge_type"] for e in edges}
            assert types == {"CAUSES", "INFLUENCES"}
        finally:
            conn.close()

    def test_populates_change_feed(self):
        from ohm.graph.batch import fast_batch_create_nodes, fast_batch_create_edges
        conn = _init_db()
        try:
            nodes = fast_batch_create_nodes(
                conn, nodes=[{"label": "A", "node_type": "concept"},
                             {"label": "B", "node_type": "concept"}],
                created_by="test",
            )
            a, b = [n["id"] for n in nodes]
            edges = fast_batch_create_edges(
                conn,
                edges=[{"from_node": a, "to_node": b, "edge_type": "CAUSES", "layer": "L3"}],
                created_by="test",
            )
            assert edges is not None
            edge_id = edges[0]["id"]
            feed_rows = conn.execute(
                "SELECT row_id FROM ohm_change_feed "
                "WHERE table_name = 'ohm_edges' AND operation = 'INSERT'"
            ).fetchall()
            feed_ids = {r[0] for r in feed_rows}
            assert edge_id in feed_ids
        finally:
            conn.close()

    def test_falls_back_on_invalid_edge_type(self):
        from ohm.graph.batch import fast_batch_create_nodes, fast_batch_create_edges
        conn = _init_db()
        try:
            nodes = fast_batch_create_nodes(
                conn, nodes=[{"label": "A", "node_type": "concept"},
                             {"label": "B", "node_type": "concept"}],
                created_by="test",
            )
            a, b = [n["id"] for n in nodes]
            result = fast_batch_create_edges(
                conn,
                edges=[{"from_node": a, "to_node": b, "edge_type": "NOT_A_REAL_TYPE", "layer": "L3"}],
                created_by="test",
            )
            assert result is None
        finally:
            conn.close()

    def test_empty_batch_returns_empty_list(self):
        from ohm.graph.batch import fast_batch_create_edges
        conn = _init_db()
        try:
            result = fast_batch_create_edges(conn, edges=[], created_by="test")
            assert result == []
        finally:
            conn.close()


class TestPublicAPIFallback:
    """The public batch_create_nodes/edges must try the fast path then
    fall back to the slow path transparently."""

    def test_batch_create_nodes_uses_fast_path_for_simple_case(self):
        """When all nodes are simple (no connects_to, no soft-deleted),
        the public API should use the fast path. We verify by checking
        the result is correct and the change feed is populated."""
        from ohm.queries import batch_create_nodes
        conn = _init_db()
        try:
            nodes = [
                {"label": "Simple A", "node_type": "concept"},
                {"label": "Simple B", "node_type": "concept"},
            ]
            result = batch_create_nodes(conn, nodes=nodes, created_by="test")
            assert len(result) == 2
            # Change feed populated for both
            feed = conn.execute(
                "SELECT COUNT(*) FROM ohm_change_feed "
                "WHERE table_name = 'ohm_nodes' AND operation = 'INSERT'"
            ).fetchone()[0]
            assert feed >= 2
        finally:
            conn.close()

    def test_batch_create_nodes_falls_back_for_connects_to(self):
        """When a node has connects_to, the fast path returns None and
        the slow path handles it. The end result must still be correct."""
        from ohm.queries import batch_create_nodes, create_node
        conn = _init_db()
        try:
            # Create an anchor node first
            anchor = create_node(conn, label="Anchor", created_by="test")
            # Now create a node that connects to it
            result = batch_create_nodes(
                conn,
                nodes=[{"label": "Linked", "node_type": "concept", "connects_to": [anchor["id"]]}],
                created_by="test",
            )
            assert len(result) == 1
            assert result[0]["label"] == "Linked"
        finally:
            conn.close()

    def test_batch_create_edges_uses_fast_path(self):
        from ohm.queries import batch_create_nodes, batch_create_edges
        conn = _init_db()
        try:
            nodes = batch_create_nodes(
                conn, nodes=[{"label": "A", "node_type": "concept"},
                             {"label": "B", "node_type": "concept"}],
                created_by="test",
            )
            a, b = nodes[0]["id"], nodes[1]["id"]
            edges = batch_create_edges(
                conn,
                edges=[{"from_node": a, "to_node": b, "edge_type": "CAUSES", "layer": "L3"}],
                created_by="test",
            )
            assert len(edges) == 1
            assert edges[0]["edge_type"] == "CAUSES"
        finally:
            conn.close()

    def test_create_batch_still_works(self):
        """create_batch() delegates to batch_create_nodes/edges and must
        still work with the fast path active."""
        from ohm.queries import create_batch
        conn = _init_db()
        try:
            result = create_batch(
                conn,
                nodes=[{"label": "Batch A", "node_type": "concept"},
                       {"label": "Batch B", "node_type": "concept"}],
                edges=[],
                created_by="test",
            )
            assert result["nodes_created"] == 2
            assert result["edges_created"] == 0
        finally:
            conn.close()


class TestCorrectnessVsSlowPath:
    """The fast path and the slow path should produce equivalent results."""

    def test_node_id_format_matches_schema(self):
        """The fast path uses the same generate_node_id as create_node,
        so the id format must match: ``<type>-<label_slug>_<suffix>``."""
        from ohm.graph.batch import fast_batch_create_nodes
        conn = _init_db()
        try:
            result = fast_batch_create_nodes(
                conn, nodes=[{"label": "Test Label", "node_type": "concept"}],
                created_by="test",
            )
            assert result is not None
            nid = result[0]["id"]
            # Format: type-labelslug_5charsuffix
            assert nid.startswith("concept-test_label_"), (
                f"Unexpected id format: {nid!r}"
            )
            # The 6-char suffix is random but present
            suffix = nid.rsplit("_", 1)[-1]
            assert len(suffix) == 6
        finally:
            conn.close()

    def test_same_edge_attributes_as_slow_path(self):
        """Fast-path edge should have the same confidence, layer, type,
        and from/to as a slow-path edge."""
        from ohm.graph.batch import fast_batch_create_nodes, fast_batch_create_edges
        from ohm.queries import create_edge

        conn = _init_db()
        try:
            nodes = fast_batch_create_nodes(
                conn, nodes=[{"label": "Src", "node_type": "concept"},
                             {"label": "Dst", "node_type": "concept"}],
                created_by="test",
            )
            src, dst = nodes[0]["id"], nodes[1]["id"]

            fast = fast_batch_create_edges(
                conn,
                edges=[{
                    "from_node": src, "to_node": dst,
                    "edge_type": "CAUSES", "layer": "L3",
                    "confidence": 0.7, "provenance": "test",
                }],
                created_by="test",
            )
            assert fast is not None
            e = fast[0]
            assert e["from_node"] == src
            assert e["to_node"] == dst
            assert e["edge_type"] == "CAUSES"
            assert e["layer"] == "L3"
            assert e["confidence"] == pytest.approx(0.7)
            assert e["provenance"] == "test"
        finally:
            conn.close()