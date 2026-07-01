"""Tests for alias resolution and content hashing (OHM-g0kv)."""

import pytest

from ohm.schema import initialize_schema
from ohm.queries import register_alias, resolve_alias, query_aliases, register_content_hash, lookup_content_hash
from ohm.validation import normalize_alias, compute_content_hash


class TestNormalizeAlias:
    def test_lowercase(self):
        assert normalize_alias("Hormuz AND-Gate") == "hormuz_and-gate"

    def test_collapse_whitespace(self):
        assert normalize_alias("  Demand  Rationing  ") == "demand_rationing"

    def test_remove_punctuation(self):
        assert normalize_alias("Strait of Hormuz") == "strait_of_hormuz"

    def test_hyphens_preserved(self):
        assert normalize_alias("AND-Gate") == "and-gate"

    def test_empty_string(self):
        assert normalize_alias("") == ""

    def test_already_normalized(self):
        assert normalize_alias("hormuz_and_gate") == "hormuz_and_gate"


class TestComputeContentHash:
    def test_deterministic(self):
        h1 = compute_content_hash("test content")
        h2 = compute_content_hash("test content")
        assert h1 == h2

    def test_different_content(self):
        h1 = compute_content_hash("alpha")
        h2 = compute_content_hash("beta")
        assert h1 != h2

    def test_sha256_length(self):
        h = compute_content_hash("anything")
        assert len(h) == 64


class TestAliasRegistration:
    def test_register_alias(self, test_db):
        result = register_alias(test_db, alias_norm="hormuz_and-gate", node_id="n1")
        assert result["created"] is True
        assert result["alias_norm"] == "hormuz_and-gate"
        assert result["node_id"] == "n1"

    def test_register_duplicate_returns_existing(self, test_db):
        r1 = register_alias(test_db, alias_norm="test_alias", node_id="n1")
        r2 = register_alias(test_db, alias_norm="test_alias", node_id="n1")
        assert r1["created"] is True
        assert r2["created"] is False
        assert r1["id"] == r2["id"]

    def test_register_same_alias_different_nodes(self, test_db):
        r1 = register_alias(test_db, alias_norm="shared_alias", node_id="n1")
        r2 = register_alias(test_db, alias_norm="shared_alias", node_id="n2")
        assert r1["created"] is True
        assert r2["created"] is True
        assert r1["id"] != r2["id"]

    def test_resolve_existing(self, test_db):
        register_alias(test_db, alias_norm="demand_rationing", node_id="n2")
        results = resolve_alias(test_db, alias_norm="demand_rationing")
        assert len(results) == 1
        assert results[0]["node_id"] == "n2"

    def test_resolve_missing(self, test_db):
        results = resolve_alias(test_db, alias_norm="nonexistent")
        assert results == []

    def test_query_aliases_by_node(self, test_db):
        register_alias(test_db, alias_norm="alias_a", node_id="n1")
        register_alias(test_db, alias_norm="alias_b", node_id="n1")
        register_alias(test_db, alias_norm="alias_c", node_id="n2")
        aliases = query_aliases(test_db, node_id="n1")
        assert len(aliases) == 2

    def test_query_aliases_by_prefix(self, test_db):
        register_alias(test_db, alias_norm="hormuz_and-gate", node_id="n1")
        register_alias(test_db, alias_norm="hormuz_strait", node_id="n2")
        register_alias(test_db, alias_norm="demand_rationing", node_id="n3")
        aliases = query_aliases(test_db, prefix="hormuz")
        assert len(aliases) == 2

    def test_query_aliases_all(self, test_db):
        register_alias(test_db, alias_norm="a1", node_id="n1")
        register_alias(test_db, alias_norm="a2", node_id="n2")
        aliases = query_aliases(test_db)
        assert len(aliases) == 2


class TestContentHash:
    def test_register_hash(self, test_db):
        result = register_content_hash(test_db, node_id="src1", content_hash="abc123")
        assert result["created"] is True
        assert result["node_id"] == "src1"
        assert result["content_hash"] == "abc123"

    def test_upsert_hash(self, test_db):
        r1 = register_content_hash(test_db, node_id="src1", content_hash="hash1")
        r2 = register_content_hash(test_db, node_id="src1", content_hash="hash2")
        assert r1["created"] is True
        assert r2["created"] is False
        assert r2["content_hash"] == "hash2"

    def test_lookup_hash(self, test_db):
        register_content_hash(test_db, node_id="src1", content_hash="sha256abc")
        results = lookup_content_hash(test_db, content_hash="sha256abc")
        assert len(results) == 1
        assert results[0]["node_id"] == "src1"

    def test_lookup_missing_hash(self, test_db):
        results = lookup_content_hash(test_db, content_hash="nonexistent")
        assert results == []


class TestAliasOnNodeCreation:
    """Tests for auto-alias registration when nodes are created (OHM-g0kv.3)."""

    def test_alias_auto_registered_on_create(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", agent_name="test")
        store.write_node(id="n1", label="Hormuz AND-Gate", type="concept")
        results = resolve_alias(store.conn, alias_norm="hormuz_and-gate")
        assert len(results) == 1
        assert results[0]["node_id"] == "n1"

    def test_alias_not_overwritten_on_update(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", agent_name="test")
        store.write_node(id="n2", label="First Label", type="concept")
        store.write_node(id="n2", label="Second Label", type="concept")
        results = resolve_alias(store.conn, alias_norm="first_label")
        assert len(results) == 1
        assert results[0]["node_id"] == "n2"

    def test_resolve_node_by_alias(self, test_db):
        from ohm.graph.store import OhmStore
        from ohm.queries import resolve_node_by_alias

        store = OhmStore(db_path=":memory:", agent_name="test")
        store.write_node(id="n3", label="Demand Rationing", type="concept")
        node = resolve_node_by_alias(store.conn, query="demand rationing")
        assert node is not None
        assert node["id"] == "n3"
        assert node["label"] == "Demand Rationing"

    def test_resolve_node_by_alias_no_match(self, test_db):
        from ohm.queries import resolve_node_by_alias

        node = resolve_node_by_alias(test_db, query="nonexistent concept")
        assert node is None

    def test_multiple_aliases_for_different_nodes(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", agent_name="test")
        store.write_node(id="n4", label="Alpha Concept", type="concept")
        store.write_node(id="n5", label="Beta Concept", type="concept")
        a = resolve_alias(store.conn, alias_norm="alpha_concept")
        b = resolve_alias(store.conn, alias_norm="beta_concept")
        assert a[0]["node_id"] == "n4"
        assert b[0]["node_id"] == "n5"


class TestAliasDuplicateDetection:
    """Tests for alias-based duplicate detection (OHM-g0kv.5)."""

    def test_no_duplicates_empty(self, test_db):
        from ohm.methods import detect_alias_duplicates

        result = detect_alias_duplicates(test_db)
        assert result == []

    def test_detects_alias_collision(self, test_db):
        from ohm.methods import detect_alias_duplicates

        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["n1", "Hormuz Gate", "concept", "test"])
        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["n2", "Hormuz Gate", "concept", "test"])
        register_alias(test_db, alias_norm="hormuz_gate", node_id="n1")
        register_alias(test_db, alias_norm="hormuz_gate", node_id="n2")
        result = detect_alias_duplicates(test_db)
        assert len(result) == 1
        assert result[0]["kind"] == "alias_collision"
        assert set([result[0]["node_a"], result[0]["node_b"]]) == {"n1", "n2"}

    def test_detects_content_hash_collision(self, test_db):
        from ohm.methods import detect_alias_duplicates

        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["s1", "Source A", "source", "test"])
        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["s2", "Source B", "source", "test"])
        register_content_hash(test_db, node_id="s1", content_hash="abc123")
        register_content_hash(test_db, node_id="s2", content_hash="abc123")
        result = detect_alias_duplicates(test_db)
        assert len(result) == 1
        assert result[0]["kind"] == "content_hash_collision"

    def test_no_collision_different_aliases(self, test_db):
        from ohm.methods import detect_alias_duplicates

        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["n1", "Alpha", "concept", "test"])
        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["n2", "Beta", "concept", "test"])
        register_alias(test_db, alias_norm="alpha", node_id="n1")
        register_alias(test_db, alias_norm="beta", node_id="n2")
        result = detect_alias_duplicates(test_db)
        assert result == []


# ── OHM-z2gp: queries/ path alias + merge + semantic dedup ──────────────────


class TestCreateNodeAutoAlias:
    """create_node() in queries/ should auto-register aliases (OHM-z2gp)."""

    def test_create_node_registers_alias(self, test_db):
        from ohm.queries import create_node, resolve_alias

        node = create_node(test_db, label="Test Concept Alpha", node_type="concept", created_by="test")
        results = resolve_alias(test_db, alias_norm="test_concept_alpha")
        assert len(results) == 1
        assert results[0]["node_id"] == node["id"]

    def test_create_node_alias_idempotent(self, test_db):
        from ohm.queries import create_node

        n1 = create_node(test_db, label="Unique Label", node_type="concept", created_by="test")
        # The alias should exist — creating another node with same label
        # should NOT overwrite the first alias (it's a separate node with
        # a different auto-generated id)
        n2 = create_node(test_db, label="Unique Label", node_type="concept", created_by="test")
        assert n1["id"] != n2["id"]
        # Both aliases should exist
        from ohm.queries import resolve_alias

        results = resolve_alias(test_db, alias_norm="unique_label")
        assert len(results) == 2


class TestFindOrCreateAliasResolution:
    """find_or_create_node() should use alias resolution first (OHM-z2gp)."""

    def test_find_or_create_finds_via_alias(self, test_db):
        from ohm.queries import create_node, find_or_create_node

        original = create_node(test_db, label="Hormuz AND-Gate", node_type="concept", created_by="metis")
        # Search with a differently-cased label that normalizes to the same alias
        found = find_or_create_node(test_db, label="hormuz and-gate", node_type="concept", created_by="metis")
        assert found["id"] == original["id"]
        assert found.get("created") is False

    def test_find_or_create_finds_via_label_fallback(self, test_db):
        from ohm.queries import find_or_create_node

        # Insert a node directly (bypassing create_node alias registration)
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
            ["manual-1", "Manual Node", "concept", "test"],
        )
        found = find_or_create_node(test_db, label="Manual Node", node_type="concept", created_by="test")
        assert found["id"] == "manual-1"
        assert found.get("created") is False

    def test_find_or_create_creates_when_not_found(self, test_db):
        from ohm.queries import find_or_create_node

        node = find_or_create_node(test_db, label="Brand New Concept", node_type="concept", created_by="test")
        assert node.get("created") is True
        assert node["label"] == "Brand New Concept"


class TestMergeNodesQueries:
    """merge_nodes() in queries/ — re-points edges/observations and soft-deletes (OHM-z2gp)."""

    def test_merge_soft_deletes_merge_node(self, test_db):
        from ohm.queries import create_node, merge_nodes

        keep = create_node(test_db, label="Keep Me", node_type="concept", created_by="metis")
        merge = create_node(test_db, label="Merge Me", node_type="concept", created_by="metis")
        result = merge_nodes(test_db, keep_id=keep["id"], merge_id=merge["id"], merged_by="metis")
        assert result["keep"] == keep["id"]
        assert result["merged"] == merge["id"]
        # merge node should be soft-deleted
        row = test_db.execute("SELECT deleted_at FROM ohm_nodes WHERE id = ?", [merge["id"]]).fetchone()
        assert row[0] is not None

    def test_merge_same_id_raises(self, test_db):
        from ohm.queries import create_node, merge_nodes
        import pytest as _pytest

        n = create_node(test_db, label="Same", node_type="concept", created_by="metis")
        with _pytest.raises(ValueError, match="nothing to merge"):
            merge_nodes(test_db, keep_id=n["id"], merge_id=n["id"], merged_by="metis")

    def test_merge_missing_keep_raises(self, test_db):
        from ohm.queries import merge_nodes
        from ohm.exceptions import NodeNotFoundError
        import pytest as _pytest

        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["real-1", "Real", "concept", "test"])
        with _pytest.raises(NodeNotFoundError):
            merge_nodes(test_db, keep_id="does-not-exist", merge_id="real-1", merged_by="test")

    def test_merge_repoints_edges(self, test_db):
        from ohm.queries import create_node, create_edge, merge_nodes

        a = create_node(test_db, label="A", node_type="concept", created_by="metis")
        b = create_node(test_db, label="B", node_type="concept", created_by="metis")
        c = create_node(test_db, label="C", node_type="concept", created_by="metis")
        # Edge from b → c
        create_edge(test_db, from_node=b["id"], to_node=c["id"], edge_type="CAUSES", layer="L3", created_by="metis")
        # Merge b into a
        merge_nodes(test_db, keep_id=a["id"], merge_id=b["id"], merged_by="metis")
        # Edge should now be from a → c
        edges = test_db.execute(
            "SELECT from_node, to_node FROM ohm_edges WHERE from_node = ? AND deleted_at IS NULL",
            [a["id"]],
        ).fetchall()
        assert len(edges) == 1
        assert edges[0][1] == c["id"]


class TestSemanticDuplicates:
    """detect_semantic_duplicates() finds nodes with similar embeddings (OHM-z2gp)."""

    def test_no_embeddings_returns_empty(self, test_db):
        from ohm.methods import detect_semantic_duplicates

        test_db.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)", ["n1", "A", "concept", "test"])
        result = detect_semantic_duplicates(test_db)
        assert result == []

    def test_returns_empty_on_empty_db(self, test_db):
        from ohm.methods import detect_semantic_duplicates

        result = detect_semantic_duplicates(test_db, similarity_threshold=0.85)
        assert result == []
