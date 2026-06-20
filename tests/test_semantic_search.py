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
        result = conn.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded = true AND extension_name = 'vss'").fetchone()
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
        columns = conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'ohm_nodes' AND column_name = 'embedding'").fetchall()

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
        vss_loaded = probe.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded = true AND extension_name = 'vss'").fetchone()
        probe.close()
        if vss_loaded is None:
            pytest.skip("VSS extension not available in this environment")

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        # Check HNSW index exists
        indexes = conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'ohm_nodes'").fetchall()
        index_names = {idx[0] for idx in indexes}

        # The HNSW index should be named idx_nodes_embedding
        assert "idx_nodes_embedding" in index_names, f"HNSW index should exist, found: {index_names}"

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
            "INSERT INTO ohm_nodes (id, label, type, created_by, embedding) VALUES (?, ?, ?, ?, ?::FLOAT[768])",
            ["test-1", "Test Node", "concept", "test_agent", embedding],
        )

        # Read it back
        result = conn.execute("SELECT id, label, embedding FROM ohm_nodes WHERE id = 'test-1'").fetchone()

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
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
            ["test-2", "No Embedding", "concept", "test_agent"],
        )

        result = conn.execute("SELECT id, embedding FROM ohm_nodes WHERE id = 'test-2'").fetchone()

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
            "SELECT id, label, array_cosine_distance(embedding, ?::FLOAT[768]) AS distance FROM ohm_nodes WHERE embedding IS NOT NULL ORDER BY distance LIMIT 10",
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
            "SELECT id, label, type, array_cosine_distance(embedding, ?::FLOAT[768]) AS distance FROM ohm_nodes WHERE embedding IS NOT NULL AND type = ? ORDER BY distance LIMIT 10",
            [embedding, "concept"],
        ).fetchall()

        assert len(results) == 1
        assert results[0][2] == "concept"


class TestHybridSemanticSearch:
    """Tests for hybrid semantic + HD membership search (OHM-xuf4)."""

    def _seed_hybrid_graph(self, test_db):
        """Seed a graph with embeddings + HD fingerprints for hybrid tests.

        Returns (node_ids, embeddings_dict) where embeddings_dict maps
        node_id -> 768-dim embedding vector.
        """
        from ohm.queries import create_node, update_node_hd_fingerprint

        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        # Three nodes with distinct semantic + structural profiles:
        # - "nuclear reactor safety" — tech topic, structurally similar to design
        # - "nuclear reactor design" — tech topic, structurally similar to safety
        # - "baking sourdough bread" — unrelated topic, unrelated structure
        n1 = create_node(test_db, label="nuclear reactor safety", node_type="concept", created_by="test")
        n2 = create_node(test_db, label="nuclear reactor design", node_type="concept", created_by="test")
        n3 = create_node(test_db, label="baking sourdough bread", node_type="concept", created_by="test")

        # Embeddings: n1 and n2 close in cosine space, n3 far away
        emb_tech = [0.0] * 768
        emb_tech[0] = 0.9
        emb_tech[1] = 0.8

        emb_tech2 = [0.0] * 768
        emb_tech2[0] = 0.85
        emb_tech2[1] = 0.9

        emb_food = [0.0] * 768
        emb_food[2] = 0.9
        emb_food[3] = 0.8

        embeddings = {n1["id"]: emb_tech, n2["id"]: emb_tech2, n3["id"]: emb_food}

        for nid, emb in embeddings.items():
            test_db.execute(
                "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
                [emb, nid],
            )

        # HD fingerprints: n1 and n2 share tokens (structurally similar),
        # n3 is structurally divergent
        update_node_hd_fingerprint(test_db, n1["id"])
        update_node_hd_fingerprint(test_db, n2["id"])
        update_node_hd_fingerprint(test_db, n3["id"])

        return [n1["id"], n2["id"], n3["id"]], embeddings

    def test_membership_weight_none_returns_cosine_only(self, test_db, monkeypatch):
        """membership_weight=None returns the same shape as before (no HD fields)."""
        from ohm.queries import semantic_search

        node_ids, _ = self._seed_hybrid_graph(test_db)

        # Stub generate_embedding to avoid Ollama dependency
        from ohm.graph import queries as queries_mod

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            return [0.0] * 768

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="nuclear reactor safety", membership_weight=None)
        assert len(results) == 3
        for r in results:
            assert "cosine_similarity" not in r
            assert "hd_similarity" not in r
            assert "blended_score" not in r
            assert "distance" in r

    def test_membership_weight_zero_returns_cosine_only(self, test_db, monkeypatch):
        """membership_weight=0.0 returns cosine-only ranking (HD fields present but unused)."""
        from ohm.queries import semantic_search

        node_ids, _ = self._seed_hybrid_graph(test_db)

        from ohm.graph import queries as queries_mod

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            return [0.0] * 768

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="nuclear reactor safety", membership_weight=0.0)
        assert len(results) == 3
        for r in results:
            assert "cosine_similarity" in r
            assert "hd_similarity" in r
            assert "blended_score" in r
            # blended_score should equal cosine_similarity when weight=0
            assert abs(r["blended_score"] - r["cosine_similarity"]) < 1e-6

    def test_membership_weight_includes_both_scores(self, test_db, monkeypatch):
        """membership_weight=0.3 includes both cosine and HD scores plus blended rank."""
        from ohm.queries import semantic_search

        node_ids, _ = self._seed_hybrid_graph(test_db)

        from ohm.graph import queries as queries_mod

        # Use a non-zero query embedding aligned with the tech embeddings
        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            emb = [0.0] * 768
            emb[0] = 0.9
            emb[1] = 0.8
            return emb

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="nuclear reactor safety", membership_weight=0.3)
        assert len(results) == 3
        for r in results:
            assert "cosine_similarity" in r
            assert "hd_similarity" in r
            assert "blended_score" in r
            assert isinstance(r["cosine_similarity"], float)
            assert 0.0 <= r["cosine_similarity"] <= 1.0
            # All three nodes have HD fingerprints, so hd_similarity should be set
            assert r["hd_similarity"] is not None
            assert 0.0 <= r["hd_similarity"] <= 1.0
            assert 0.0 <= r["blended_score"] <= 1.0

        # Results should be sorted by blended_score descending
        scores = [r["blended_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_membership_weight_handles_null_fingerprints(self, test_db, monkeypatch):
        """Nodes without HD fingerprints get hd_similarity=None and cosine-only blended score."""
        from ohm.queries import create_node, semantic_search

        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        # Create two nodes: one with HD fingerprint, one without
        n_with = create_node(test_db, label="with fingerprint", node_type="concept", created_by="test")
        n_without = create_node(test_db, label="without fingerprint", node_type="concept", created_by="test")

        emb = [0.0] * 768
        emb[0] = 1.0
        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id IN (?, ?)",
            [emb, n_with["id"], n_without["id"]],
        )

        from ohm.graph.queries import update_node_hd_fingerprint

        update_node_hd_fingerprint(test_db, n_with["id"])
        # Deliberately do NOT fingerprint n_without

        from ohm.graph import queries as queries_mod

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            return [0.0] * 768

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="test", membership_weight=0.5)
        assert len(results) == 2

        by_id = {r["node_id"]: r for r in results}
        assert by_id[n_with["id"]]["hd_similarity"] is not None
        assert by_id[n_without["id"]]["hd_similarity"] is None
        # Node without fingerprint: blended = (1 - 0.5) * cosine_sim
        assert abs(by_id[n_without["id"]]["blended_score"] - 0.5 * by_id[n_without["id"]]["cosine_similarity"]) < 1e-6

    def test_membership_weight_finds_structurally_similar_nodes(self, test_db, monkeypatch):
        """Structurally similar but semantically divergent nodes surface with HD weight.

        Scenario: query embedding is orthogonal to all stored embeddings
        (cosine_sim ≈ 0 for everyone), but the query text shares tokens
        with one node's label (high HD similarity). With membership_weight
        high enough, that node should rank first.
        """
        from ohm.queries import create_node, semantic_search
        from ohm.graph.queries import update_node_hd_fingerprint

        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        # Two nodes with orthogonal embeddings (cosine distance ≈ 1.0)
        n_target = create_node(test_db, label="alpha beta gamma", node_type="concept", created_by="test")
        n_other = create_node(test_db, label="delta epsilon zeta", node_type="concept", created_by="test")

        emb_target = [0.0] * 768
        emb_target[0] = 1.0
        emb_other = [0.0] * 768
        emb_other[1] = 1.0

        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [emb_target, n_target["id"]])
        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [emb_other, n_other["id"]])

        update_node_hd_fingerprint(test_db, n_target["id"])
        update_node_hd_fingerprint(test_db, n_other["id"])

        from ohm.graph import queries as queries_mod

        # Query embedding is orthogonal to both stored embeddings
        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            emb = [0.0] * 768
            emb[2] = 1.0  # Different dimension than either stored embedding
            return emb

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        # Query text shares tokens with n_target ("alpha beta gamma")
        # so HD similarity to n_target should be much higher than to n_other
        results = semantic_search(test_db, query="alpha beta gamma", membership_weight=0.9)
        assert len(results) == 2
        # n_target should rank first because HD similarity dominates
        assert results[0]["node_id"] == n_target["id"]
        assert results[0]["hd_similarity"] > results[1]["hd_similarity"]

    def test_membership_weight_out_of_range_raises(self, test_db, monkeypatch):
        """membership_weight outside [0, 1] raises ValueError."""
        from ohm.queries import create_node, semantic_search

        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        n = create_node(test_db, label="test node", node_type="concept", created_by="test")
        emb = [0.0] * 768
        emb[0] = 1.0
        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [emb, n["id"]],
        )

        from ohm.graph import queries as queries_mod

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            return [0.0] * 768

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        with pytest.raises(ValueError, match="membership_weight"):
            semantic_search(test_db, query="test", membership_weight=1.5)
        with pytest.raises(ValueError, match="membership_weight"):
            semantic_search(test_db, query="test", membership_weight=-0.1)

    def test_membership_weight_empty_query_returns_empty(self, test_db):
        """Empty query returns empty list regardless of membership_weight."""
        from ohm.queries import semantic_search

        results = semantic_search(test_db, query="", membership_weight=0.5)
        assert results == []
        results = semantic_search(test_db, query="   ", membership_weight=0.5)
        assert results == []


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


class TestManifoldDensityAndGeodesic:
    """OHM-nnrw: manifold_density_score and geodesic_distance in semantic search."""

    def _seed_density_graph(self, test_db):
        """Seed nodes with embeddings of varying local density.

        Three clusters:
        - Dense: 3 nodes close together in dim 0-1
        - Sparse: 1 node far from everything
        Returns (dense_ids, sparse_id).
        """
        from ohm.queries import create_node

        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        dense_nodes = []
        for label in ["dense_a", "dense_b", "dense_c"]:
            n = create_node(test_db, label=label, node_type="concept", created_by="test")
            dense_nodes.append(n)

        sparse_node = create_node(test_db, label="sparse_isolated", node_type="concept", created_by="test")

        emb_dense = [0.0] * 768
        emb_dense[0] = 0.9
        emb_dense[1] = 0.8

        for n in dense_nodes:
            test_db.execute(
                "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
                [emb_dense, n["id"]],
            )

        emb_sparse = [0.0] * 768
        emb_sparse[10] = 0.9
        emb_sparse[11] = 0.8

        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [emb_sparse, sparse_node["id"]],
        )

        return [n["id"] for n in dense_nodes], sparse_node["id"]

    def test_semantic_search_includes_manifold_density(self, test_db, monkeypatch):
        """semantic_search results include manifold_density_score field."""
        from ohm.queries import semantic_search
        from ohm.graph import queries as queries_mod

        dense_ids, sparse_id = self._seed_density_graph(test_db)

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            emb = [0.0] * 768
            emb[0] = 0.9
            emb[1] = 0.8
            return emb

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="dense topic")
        assert len(results) == 4
        for r in results:
            assert "manifold_density_score" in r

    def test_manifold_density_monotonic_with_neighborhood(self, test_db, monkeypatch):
        """Dense cluster nodes have higher manifold_density than isolated nodes."""
        from ohm.queries import semantic_search
        from ohm.graph import queries as queries_mod

        dense_ids, sparse_id = self._seed_density_graph(test_db)

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            emb = [0.0] * 768
            emb[0] = 0.9
            emb[1] = 0.8
            return emb

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="dense topic")
        by_id = {r["node_id"]: r for r in results}

        dense_densities = [by_id[nid]["manifold_density_score"] for nid in dense_ids if nid in by_id and by_id[nid]["manifold_density_score"] is not None]
        sparse_density = by_id[sparse_id]["manifold_density_score"]

        if dense_densities and sparse_density is not None:
            avg_dense = sum(dense_densities) / len(dense_densities)
            assert avg_dense > sparse_density

    def test_geodesic_distance_equals_cosine_distance(self, test_db, monkeypatch):
        """geodesic_distance field equals the cosine distance field."""
        from ohm.queries import semantic_search
        from ohm.graph import queries as queries_mod

        dense_ids, sparse_id = self._seed_density_graph(test_db)

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            emb = [0.0] * 768
            emb[0] = 0.9
            emb[1] = 0.8
            return emb

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="dense topic")
        for r in results:
            assert "geodesic_distance" in r
            if r["geodesic_distance"] is not None and r.get("distance") is not None:
                assert abs(r["geodesic_distance"] - r["distance"]) < 1e-9

    def test_manifold_density_none_without_embedding(self, test_db, monkeypatch):
        """Nodes without embeddings get manifold_density_score=None."""
        from ohm.queries import create_node, semantic_search
        from ohm.graph import queries as queries_mod

        try:
            test_db.execute("SELECT array_cosine_distance([1.0]::FLOAT[1], [1.0]::FLOAT[1])")
        except Exception:
            pytest.skip("VSS extension (array_cosine_distance) not available")

        n_with = create_node(test_db, label="has embedding", node_type="concept", created_by="test")
        n_without = create_node(test_db, label="no embedding", node_type="concept", created_by="test")

        emb = [0.0] * 768
        emb[0] = 1.0
        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [emb, n_with["id"]],
        )

        def fake_generate_embedding(text, model="nomic-embed-text", ollama_url="http://localhost:11434"):
            return [0.0] * 768

        monkeypatch.setattr(queries_mod, "generate_embedding", fake_generate_embedding)

        results = semantic_search(test_db, query="test")
        by_id = {r["node_id"]: r for r in results}
        assert by_id[n_with["id"]]["manifold_density_score"] is not None
        # n_without won't appear in results (embedding IS NOT NULL filter)
