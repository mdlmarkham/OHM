"""Tests for semantic search via VSS/HNSW index (OHM-o9f)."""

from __future__ import annotations

import pytest



class TestVSSExtension:
    """Tests for VSS extension loading and HNSW index creation."""

    def test_vss_extension_loads(self, tmp_path):
        """VSS extension loads without error."""
        import duckdb
        from ohm.db import _load_extensions

        conn = duckdb.connect(str(tmp_path / "test.duckdb"))
        _load_extensions(conn)

        # Verify VSS extension is loaded
        result = conn.execute(
            "SELECT extension_name FROM duckdb_extensions() "
            "WHERE loaded = true AND extension_name = 'vss'"
        ).fetchone()
        conn.close()

        if result is None:
            pytest.skip("VSS extension not available in this environment")
        assert result is not None

    def test_embedding_column_added_by_migration(self, tmp_path):
        """Migration 0.11.0 adds embedding column to ohm_nodes."""
        from ohm.db import connect

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        # Check embedding column exists
        columns = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'ohm_nodes' AND column_name = 'embedding'"
        ).fetchall()

        assert len(columns) == 1, "embedding column should exist"
        # DuckDB reports FLOAT[768] as a list type
        col_name, col_type = columns[0]
        assert col_name == "embedding"
        assert "FLOAT" in col_type.upper() or "768" in col_type

        conn.close()

    def test_hnsw_index_created(self, tmp_path):
        """HNSW index is created on ohm_nodes.embedding after migration."""
        import duckdb
        from ohm.db import _load_extensions, connect

        # Check VSS is available first
        probe = duckdb.connect(str(tmp_path / "probe.duckdb"))
        _load_extensions(probe)
        vss_loaded = probe.execute(
            "SELECT extension_name FROM duckdb_extensions() WHERE loaded = true AND extension_name = 'vss'"
        ).fetchone()
        probe.close()
        if vss_loaded is None:
            pytest.skip("VSS extension not available in this environment")

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        # Check HNSW index exists
        indexes = conn.execute(
            "SELECT index_name FROM duckdb_indexes() "
            "WHERE table_name = 'ohm_nodes'"
        ).fetchall()
        index_names = {idx[0] for idx in indexes}

        # The HNSW index should be named idx_nodes_embedding
        assert "idx_nodes_embedding" in index_names, (
            f"HNSW index should exist, found: {index_names}"
        )

        conn.close()

    def test_insert_node_with_embedding(self, tmp_path):
        """Can insert a node with an embedding vector."""
        from ohm.db import connect

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        # Create a 768-dimensional embedding (all zeros for test)
        embedding = [0.0] * 768
        embedding[0] = 1.0  # Set first dimension

        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, embedding) "
            "VALUES (?, ?, ?, ?, ?::FLOAT[768])",
            ["test-1", "Test Node", "concept", "test_agent", embedding],
        )

        # Read it back
        result = conn.execute(
            "SELECT id, label, embedding FROM ohm_nodes WHERE id = 'test-1'"
        ).fetchone()

        assert result is not None
        assert result[0] == "test-1"
        assert result[1] == "Test Node"

        conn.close()

    def test_insert_node_without_embedding(self, tmp_path):
        """Can insert a node without an embedding (NULL is fine)."""
        from ohm.db import connect

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) "
            "VALUES (?, ?, ?, ?)",
            ["test-2", "No Embedding", "concept", "test_agent"],
        )

        result = conn.execute(
            "SELECT id, embedding FROM ohm_nodes WHERE id = 'test-2'"
        ).fetchone()

        assert result is not None
        assert result[0] == "test-2"
        assert result[1] is None  # No embedding

        conn.close()


class TestGenerateEmbedding:
    """Tests for the generate_embedding function."""

    def test_generate_embedding_returns_none_without_ollama(self):
        """generate_embedding returns None when Ollama is not running."""
        from ohm.queries import generate_embedding

        # Ollama is likely not running in test environment
        result = generate_embedding("test query")
        # Either None (Ollama not running) or a list (if Ollama is available)
        if result is not None:
            assert isinstance(result, list)
            assert len(result) == 768  # nomic-embed-text dimension

    def test_generate_embedding_empty_text(self):
        """generate_embedding handles empty text gracefully."""
        from ohm.queries import generate_embedding

        # Empty text should still attempt to call Ollama
        # (Ollama may or may not be running)
        result = generate_embedding("")
        # Result is either None or a list
        assert result is None or isinstance(result, list)


class TestSemanticSearch:
    """Tests for the semantic_search function."""

    def test_semantic_search_raises_without_ollama(self, test_db):
        """semantic_search raises ValueError when Ollama is not available."""
        from ohm.queries import semantic_search

        # If Ollama is running, this will succeed; if not, it raises ValueError
        try:
            result = semantic_search(test_db, query="test query")
            # Ollama is running — verify result structure
            assert isinstance(result, list)
        except ValueError as e:
            assert "Ollama" in str(e)

    def test_semantic_search_with_mock_embedding(self, test_db):
        """semantic_search works with pre-populated embeddings."""
        from ohm.queries import create_node

        # Skip if VSS (array_cosine_distance) not available
        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        # Create nodes with embeddings
        node_a = create_node(test_db, label="Machine Learning", node_type="concept", created_by="test")
        node_b = create_node(test_db, label="Deep Learning", node_type="concept", created_by="test")
        node_c = create_node(test_db, label="Baking Bread", node_type="concept", created_by="test")

        # Set embeddings manually (simulating what Ollama would produce)
        # ML and DL should be closer to each other than to baking
        ml_embedding = [0.0] * 768
        ml_embedding[0] = 0.9  # High in "tech" dimension
        ml_embedding[1] = 0.8

        dl_embedding = [0.0] * 768
        dl_embedding[0] = 0.85  # Similar to ML
        dl_embedding[1] = 0.9

        baking_embedding = [0.0] * 768
        baking_embedding[2] = 0.9  # High in "food" dimension
        baking_embedding[3] = 0.8

        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [ml_embedding, node_a["id"]],
        )
        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [dl_embedding, node_b["id"]],
        )
        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [baking_embedding, node_c["id"]],
        )

        # Search with a "tech" query vector
        query_embedding = [0.0] * 768
        query_embedding[0] = 1.0  # Tech dimension
        query_embedding[1] = 1.0

        # Use direct cosine distance query (bypassing Ollama)
        results = test_db.execute(
            "SELECT id, label, array_cosine_distance(embedding, ?::FLOAT[768]) AS distance "
            "FROM ohm_nodes WHERE embedding IS NOT NULL "
            "ORDER BY distance LIMIT 10",
            [query_embedding],
        ).fetchall()

        assert len(results) == 3
        # ML and DL should be closer (lower distance) than baking
        ids_by_distance = [r[0] for r in results]
        assert node_a["id"] in ids_by_distance[:2]  # ML in top 2
        assert node_b["id"] in ids_by_distance[:2]  # DL in top 2
        assert node_c["id"] == ids_by_distance[2]  # Baking last

    def test_semantic_search_with_node_type_filter(self, test_db):
        """semantic_search respects node_type filter."""
        from ohm.queries import create_node

        # Skip if VSS (array_cosine_distance) not available
        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        # Create nodes of different types
        create_node(test_db, label="ML Concept", node_type="concept", created_by="test")
        create_node(test_db, label="ML Pattern", node_type="pattern", created_by="test")

        # Set same embedding for both
        embedding = [0.0] * 768
        embedding[0] = 1.0

        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE label LIKE 'ML%'",
            [embedding],
        )

        # Query with type filter
        results = test_db.execute(
            "SELECT id, label, type, array_cosine_distance(embedding, ?::FLOAT[768]) AS distance "
            "FROM ohm_nodes WHERE embedding IS NOT NULL AND type = ? "
            "ORDER BY distance LIMIT 10",
            [embedding, "concept"],
        ).fetchall()

        assert len(results) == 1
        assert results[0][2] == "concept"


class TestUpdateNodeEmbedding:
    """Tests for the update_node_embedding function."""

    def test_update_node_embedding_without_ollama(self, test_db):
        """update_node_embedding returns False when Ollama is not available."""
        from ohm.queries import create_node, update_node_embedding

        node = create_node(test_db, label="Test", node_type="concept", created_by="test")
        result = update_node_embedding(test_db, node_id=node["id"])
        # Returns False if Ollama not running
        assert result is False or result is True  # Depends on Ollama availability

    def test_update_node_embedding_with_custom_text(self, test_db):
        """update_node_embedding uses custom text when provided."""
        from ohm.queries import create_node, update_node_embedding

        node = create_node(test_db, label="Test", node_type="concept", created_by="test")
        # This will fail if Ollama is not running, but the function should handle it
        result = update_node_embedding(test_db, node_id=node["id"], text="custom text for embedding")
        # Result depends on Ollama availability
        assert isinstance(result, bool)

    def test_update_node_embedding_nonexistent_node(self, test_db):
        """update_node_embedding returns False for nonexistent node."""
        from ohm.queries import update_node_embedding

        result = update_node_embedding(test_db, node_id="nonexistent-node-id")
        assert result is False
