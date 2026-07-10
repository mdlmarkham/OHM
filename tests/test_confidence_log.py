"""OHM-733: append-only confidence change log tests.

Verifies that confidence-affecting events are logged (not silently
overwritten), the log is append-only, and concurrent writes from
different agents both land without loss.
"""

from __future__ import annotations

import pytest

from ohm.graph.queries import (
    create_edge,
    create_node,
    get_confidence_history,
    log_confidence_change,
    recompute_confidence_from_log,
)


class TestConfidenceLog:
    """Core append-only log behavior."""

    def test_log_records_change_and_updates_column(self, test_db):
        n1 = create_node(test_db, label="A", created_by="t")
        n2 = create_node(test_db, label="B", created_by="t")
        e = create_edge(test_db, from_node=n1["id"], to_node=n2["id"], layer="L3", edge_type="CAUSES", created_by="t", confidence=0.8)

        result = log_confidence_change(test_db, edge_id=e["id"], agent="metis", old_value=0.8, new_value=0.6, reason="challenge")
        assert result["new_value"] == 0.6
        assert result["reason"] == "challenge"

        # Column is refreshed
        row = test_db.execute("SELECT confidence FROM ohm_edges WHERE id = ?", [e["id"]]).fetchone()
        assert row[0] == 0.6

    def test_history_returns_all_entries_newest_first(self, test_db):
        n1 = create_node(test_db, label="A", created_by="t")
        n2 = create_node(test_db, label="B", created_by="t")
        e = create_edge(test_db, from_node=n1["id"], to_node=n2["id"], layer="L3", edge_type="CAUSES", created_by="t", confidence=0.9)

        log_confidence_change(test_db, edge_id=e["id"], agent="metis", old_value=0.9, new_value=0.7, reason="challenge")
        log_confidence_change(test_db, edge_id=e["id"], agent="apollo", old_value=0.7, new_value=0.5, reason="decay")

        hist = get_confidence_history(test_db, e["id"])
        assert len(hist) == 2
        assert hist[0]["agent"] == "apollo"
        assert hist[0]["new_value"] == 0.5
        assert hist[1]["agent"] == "metis"
        assert hist[1]["new_value"] == 0.7

    def test_concurrent_writes_both_land_in_log(self, test_db):
        """Two agents writing to the same edge — neither entry is lost."""
        n1 = create_node(test_db, label="A", created_by="t")
        n2 = create_node(test_db, label="B", created_by="t")
        e = create_edge(test_db, from_node=n1["id"], to_node=n2["id"], layer="L3", edge_type="CAUSES", created_by="t", confidence=0.8)

        # Simulate two concurrent confidence-affecting events
        log_confidence_change(test_db, edge_id=e["id"], agent="metis", old_value=0.8, new_value=0.6, reason="challenge")
        log_confidence_change(test_db, edge_id=e["id"], agent="apollo", old_value=0.8, new_value=0.5, reason="support")

        hist = get_confidence_history(test_db, e["id"])
        assert len(hist) == 2, "Both agents' entries must land in the log"
        agents = {h["agent"] for h in hist}
        assert agents == {"metis", "apollo"}

    def test_recompute_is_idempotent(self, test_db):
        n1 = create_node(test_db, label="A", created_by="t")
        n2 = create_node(test_db, label="B", created_by="t")
        e = create_edge(test_db, from_node=n1["id"], to_node=n2["id"], layer="L3", edge_type="CAUSES", created_by="t", confidence=0.8)

        log_confidence_change(test_db, edge_id=e["id"], agent="metis", old_value=0.8, new_value=0.6, reason="challenge")

        val1 = recompute_confidence_from_log(test_db, e["id"])
        val2 = recompute_confidence_from_log(test_db, e["id"])
        assert val1 == 0.6
        assert val2 == 0.6, "Recompute must be idempotent"

    def test_recompute_returns_none_for_no_history(self, test_db):
        n1 = create_node(test_db, label="A", created_by="t")
        n2 = create_node(test_db, label="B", created_by="t")
        e = create_edge(test_db, from_node=n1["id"], to_node=n2["id"], layer="L3", edge_type="CAUSES", created_by="t", confidence=0.8)

        val = recompute_confidence_from_log(test_db, e["id"])
        assert val is None, "Edge with no log history should return None"

    def test_log_table_exists_after_schema_init(self, test_db):
        rows = test_db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'ohm_confidence_log'"
        ).fetchall()
        assert len(rows) == 1

    def test_schema_version_at_least_0_46_0(self):
        from ohm.schema import SCHEMA_VERSION

        assert SCHEMA_VERSION >= "0.46.0"