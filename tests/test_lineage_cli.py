"""Tests for OHM-815: lineage default edge-type fix and OHM-810 CLI commands."""

from __future__ import annotations

import subprocess
import sys
import json
import tempfile
import pathlib
import pytest

from ohm.graph.schema import initialize_schema, DEFAULT_SCHEMA
from ohm.graph.queries import create_node, create_edge


@pytest.fixture
def db_with_lineage(tmp_path):
    """Create a DB with a DERIVES_FROM lineage chain: C -> B -> A."""
    import duckdb

    db_path = str(tmp_path / "test_lineage.duckdb")
    conn = duckdb.connect(db_path)
    initialize_schema(conn)

    a = create_node(conn, label="Source A", created_by="test")
    b = create_node(conn, label="Derived B", created_by="test")
    c = create_node(conn, label="Derived C", created_by="test")

    create_edge(conn, from_node=b["id"], to_node=a["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")
    create_edge(conn, from_node=c["id"], to_node=b["id"], edge_type="DERIVES_FROM", layer="L2", created_by="test")

    conn.close()
    return db_path, c["id"], b["id"], a["id"]


class TestLineageDefaultEdgeType:
    """OHM-815: The default --edge-type must be a real, registered edge type."""

    def test_default_edge_type_is_valid(self):
        """DERIVES_FROM must be a registered edge type in the generic schema."""
        assert "DERIVES_FROM" in DEFAULT_SCHEMA.layer_edge_types.get("L2", set())

    def test_lineage_with_default_returns_results(self, db_with_lineage):
        """ohm graph lineage with no --edge-type flag returns results (OHM-815)."""
        db_path, c_id, b_id, a_id = db_with_lineage

        result = subprocess.run(
            [sys.executable, "-m", "ohm.cli", "graph", "lineage", c_id, "--db", db_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stdout: {result.stdout}, stderr: {result.stderr}"
        # Should find the lineage chain C -> B -> A
        assert "Derived C" in result.stdout or c_id in result.stdout
        assert "Derived B" in result.stdout or b_id in result.stdout
        assert "DERIVES_FROM" in result.stdout

    def test_lineage_with_explicit_edge_type(self, db_with_lineage):
        """ohm graph lineage with explicit --edge-type DERIVES_FROM works."""
        db_path, c_id, b_id, a_id = db_with_lineage

        result = subprocess.run(
            [sys.executable, "-m", "ohm.cli", "graph", "lineage", c_id, "--edge-type", "DERIVES_FROM", "--db", db_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "DERIVES_FROM" in result.stdout

    def test_lineage_nonexistent_node(self, tmp_path):
        """ohm graph lineage for a nonexistent node returns empty message."""
        import duckdb

        db_path = str(tmp_path / "empty.duckdb")
        conn = duckdb.connect(db_path)
        initialize_schema(conn)
        conn.close()

        result = subprocess.run(
            [sys.executable, "-m", "ohm.cli", "graph", "lineage", "nonexistent_node", "--db", db_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "No DERIVES_FROM lineage" in result.stdout


class TestGapsCommand:
    """OHM-810: ohm graph gaps command."""

    def test_gaps_returns_results(self, tmp_path):
        """ohm graph gaps finds nodes with no L3 edges."""
        import duckdb

        db_path = str(tmp_path / "gaps.duckdb")
        conn = duckdb.connect(db_path)
        initialize_schema(conn)

        # Create two nodes, only one has an L3 edge
        a = create_node(conn, label="Has Edge", created_by="test")
        b = create_node(conn, label="No Edge", created_by="test")
        create_edge(conn, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="test")
        conn.close()

        result = subprocess.run(
            [sys.executable, "-m", "ohm.cli", "graph", "gaps", "--layer", "L3", "--db", db_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        # "Has Edge" should appear as having no incoming L3 edge
        # "No Edge" should appear as having no outgoing L3 edge
        assert "No Edge" in result.stdout or "no outgoing" in result.stdout.lower() or "no incoming" in result.stdout.lower()
