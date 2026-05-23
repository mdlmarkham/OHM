"""Tests for Metis zettelkasten → OHM bridge (OHM-8c6)."""

from __future__ import annotations


class TestWikilinkExtraction:
    """Tests for wikilink parsing from note content."""

    def test_simple_wikilink(self):
        """Extract [[target]] wikilink."""
        from ohm.metis_bridge import _extract_wikilinks

        links = _extract_wikilinks("See [[AND→OR Conversion]] for details")
        assert len(links) == 1
        assert links[0][0] == "AND→OR Conversion"
        assert links[0][1] is None

    def test_wikilink_with_context(self):
        """Extract [[target|context]] wikilink."""
        from ohm.metis_bridge import _extract_wikilinks

        links = _extract_wikilinks("This [[Democracy|refines]] the earlier finding")
        assert len(links) == 1
        assert links[0][0] == "Democracy"
        assert links[0][1] == "refines"

    def test_multiple_wikilinks(self):
        """Extract multiple wikilinks from text."""
        from ohm.metis_bridge import _extract_wikilinks

        links = _extract_wikilinks("[[A]] and [[B|supports]] and [[C]]")
        assert len(links) == 3
        assert links[0][0] == "A"
        assert links[1][0] == "B"
        assert links[1][1] == "supports"
        assert links[2][0] == "C"

    def test_no_wikilinks(self):
        """Return empty list when no wikilinks present."""
        from ohm.metis_bridge import _extract_wikilinks

        links = _extract_wikilinks("No wikilinks here, just plain text.")
        assert links == []

    def test_empty_text(self):
        """Handle empty text gracefully."""
        from ohm.metis_bridge import _extract_wikilinks

        assert _extract_wikilinks("") == []
        assert _extract_wikilinks(None) == []


class TestEdgeTypeDerivation:
    """Tests for deriving OHM edge type from wikilink context."""

    def test_refines_context(self):
        from ohm.metis_bridge import _derive_edge_type

        edge_type, layer = _derive_edge_type("refines")
        assert edge_type == "REFINES"
        assert layer == "L3"

    def test_supports_context(self):
        from ohm.metis_bridge import _derive_edge_type

        edge_type, layer = _derive_edge_type("supports")
        assert edge_type == "SUPPORTS"
        assert layer == "L3"

    def test_derives_context(self):
        from ohm.metis_bridge import _derive_edge_type

        edge_type, layer = _derive_edge_type("derives from")
        assert edge_type == "DERIVES_FROM"
        assert layer == "L2"

    def test_contradicts_context(self):
        from ohm.metis_bridge import _derive_edge_type

        edge_type, layer = _derive_edge_type("contradicts")
        assert edge_type == "CONTRADICTS"
        assert layer == "L3"

    def test_no_context_defaults_to_references(self):
        from ohm.metis_bridge import _derive_edge_type

        edge_type, layer = _derive_edge_type(None)
        assert edge_type == "REFERENCES"
        assert layer == "L2"

    def test_unknown_context_defaults_to_references(self):
        from ohm.metis_bridge import _derive_edge_type

        edge_type, layer = _derive_edge_type("something unrelated")
        assert edge_type == "REFERENCES"
        assert layer == "L2"


class TestProjectZettelkasten:
    """Tests for the project_zettelkasten function."""

    def test_project_with_no_metis_db(self, test_db):
        """Returns error when Metis database is not available."""
        from ohm.metis_bridge import project_zettelkasten

        result = project_zettelkasten(test_db, metis_conn=None)
        assert result["nodes_created"] == 0
        assert result["edges_created"] == 0
        assert len(result["errors"]) > 0

    def test_project_dry_run(self, test_db, tmp_path):
        """Dry run reports what would be projected without making changes."""
        import duckdb
        from ohm.metis_bridge import project_zettelkasten

        # Create a mock Metis zettelkasten database
        metis_path = str(tmp_path / "metis.duckdb")
        metis_conn = duckdb.connect(metis_path)
        metis_conn.execute("""
            CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                confidence FLOAT,
                type TEXT,
                tags TEXT
            )
        """)
        metis_conn.execute("""
            INSERT INTO notes VALUES
            ('note1', 'AND→OR Conversion', 'Direction determines function [[Democracy|refines]]', 0.9, 'pattern', 'logic'),
            ('note2', 'Democracy', 'Institutions matter', 0.85, 'pattern', 'politics'),
            ('note3', 'Low Confidence', 'Skip this', 0.3, 'pattern', 'draft')
        """)

        result = project_zettelkasten(test_db, metis_conn=metis_conn, dry_run=True)
        assert result["nodes_created"] == 2  # note1 and note2 (note3 below threshold)
        assert result["errors"] == []
        metis_conn.close()

    def test_project_creates_nodes(self, test_db, tmp_path):
        """Projection creates OHM nodes for eligible notes."""
        import duckdb
        from ohm.metis_bridge import project_zettelkasten

        # Create mock Metis zettelkasten
        metis_path = str(tmp_path / "metis.duckdb")
        metis_conn = duckdb.connect(metis_path)
        metis_conn.execute("""
            CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                confidence FLOAT,
                type TEXT,
                tags TEXT
            )
        """)
        metis_conn.execute("""
            INSERT INTO notes VALUES
            ('note1', 'Pattern A', 'Content A', 0.9, 'pattern', 'test'),
            ('note2', 'Pattern B', 'Content B', 0.8, 'concept', 'test')
        """)

        result = project_zettelkasten(test_db, metis_conn=metis_conn)
        assert result["nodes_created"] == 2
        assert result["edges_created"] == 0  # No wikilinks
        assert result["errors"] == []

        # Verify nodes exist in OHM
        nodes = test_db.execute("SELECT label, provenance FROM ohm_nodes WHERE provenance = 'metis_zettelkasten'").fetchall()
        assert len(nodes) == 2
        labels = {n[0] for n in nodes}
        assert "Pattern A" in labels
        assert "Pattern B" in labels
        metis_conn.close()

    def test_project_creates_edges_for_wikilinks(self, test_db, tmp_path):
        """Projection creates OHM edges for wikilinks between notes."""
        import duckdb
        from ohm.metis_bridge import project_zettelkasten

        metis_path = str(tmp_path / "metis.duckdb")
        metis_conn = duckdb.connect(metis_path)
        metis_conn.execute("""
            CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                confidence FLOAT,
                type TEXT,
                tags TEXT
            )
        """)
        metis_conn.execute("""
            INSERT INTO notes VALUES
            ('note1', 'Pattern A', 'See [[Pattern B|refines]] for details', 0.9, 'pattern', 'test'),
            ('note2', 'Pattern B', 'Base pattern', 0.85, 'pattern', 'test')
        """)

        result = project_zettelkasten(test_db, metis_conn=metis_conn)
        assert result["nodes_created"] == 2
        assert result["edges_created"] == 1
        assert result["errors"] == []

        # Verify edge exists
        edges = test_db.execute("SELECT edge_type FROM ohm_edges WHERE edge_type = 'REFINES'").fetchall()
        assert len(edges) == 1
        metis_conn.close()

    def test_project_skips_low_confidence(self, test_db, tmp_path):
        """Notes below confidence threshold are not projected."""
        import duckdb
        from ohm.metis_bridge import project_zettelkasten

        metis_path = str(tmp_path / "metis.duckdb")
        metis_conn = duckdb.connect(metis_path)
        metis_conn.execute("""
            CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                confidence FLOAT,
                type TEXT,
                tags TEXT
            )
        """)
        metis_conn.execute("""
            INSERT INTO notes VALUES
            ('note1', 'High Confidence', 'Good', 0.9, 'pattern', 'test'),
            ('note2', 'Low Confidence', 'Skip', 0.3, 'pattern', 'draft')
        """)

        result = project_zettelkasten(test_db, metis_conn=metis_conn)
        assert result["nodes_created"] == 1
        # Low-confidence notes are filtered by the SQL query, not counted as skipped
        assert result["patterns_skipped"] == 0
        metis_conn.close()

    def test_project_is_idempotent(self, test_db, tmp_path):
        """Running projection twice doesn't duplicate nodes or edges."""
        import duckdb
        from ohm.metis_bridge import project_zettelkasten

        metis_path = str(tmp_path / "metis.duckdb")
        metis_conn = duckdb.connect(metis_path)
        metis_conn.execute("""
            CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                confidence FLOAT,
                type TEXT,
                tags TEXT
            )
        """)
        metis_conn.execute("""
            INSERT INTO notes VALUES
            ('note1', 'Idempotent Test', 'Content', 0.9, 'pattern', 'test')
        """)

        # First projection
        result1 = project_zettelkasten(test_db, metis_conn=metis_conn)
        assert result1["nodes_created"] == 1

        # Second projection (should not duplicate)
        result2 = project_zettelkasten(test_db, metis_conn=metis_conn)
        assert result2["nodes_created"] == 1  # find_or_create_node is idempotent

        # Verify only one node exists
        nodes = test_db.execute("SELECT COUNT(*) FROM ohm_nodes WHERE label = 'Idempotent Test'").fetchone()
        assert nodes[0] == 1
        metis_conn.close()

    def test_project_records_observation(self, test_db, tmp_path):
        """Projection records metadata in observations table."""
        import duckdb
        from ohm.metis_bridge import project_zettelkasten

        metis_path = str(tmp_path / "metis.duckdb")
        metis_conn = duckdb.connect(metis_path)
        metis_conn.execute("""
            CREATE TABLE notes (
                id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                confidence FLOAT,
                type TEXT,
                tags TEXT
            )
        """)
        metis_conn.execute("""
            INSERT INTO notes VALUES
            ('note1', 'Obs Test', 'Content', 0.9, 'pattern', 'test')
        """)

        result = project_zettelkasten(test_db, metis_conn=metis_conn)
        assert result["nodes_created"] == 1

        # Check observation was recorded
        obs = test_db.execute("SELECT type, value, notes FROM ohm_observations WHERE type = 'projection'").fetchall()
        assert len(obs) >= 1
        assert obs[0][0] == "projection"
        metis_conn.close()
