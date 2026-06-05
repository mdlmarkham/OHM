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

    def test_resolve_existing(self, test_db):
        register_alias(test_db, alias_norm="demand_rationing", node_id="n2")
        result = resolve_alias(test_db, alias_norm="demand_rationing")
        assert result is not None
        assert result["node_id"] == "n2"

    def test_resolve_missing(self, test_db):
        result = resolve_alias(test_db, alias_norm="nonexistent")
        assert result is None

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
        result = resolve_alias(store.conn, alias_norm="hormuz_and-gate")
        assert result is not None
        assert result["node_id"] == "n1"

    def test_alias_not_overwritten_on_update(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", agent_name="test")
        store.write_node(id="n2", label="First Label", type="concept")
        store.write_node(id="n2", label="Second Label", type="concept")
        result = resolve_alias(store.conn, alias_norm="first_label")
        assert result is not None
        assert result["node_id"] == "n2"

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
        assert a["node_id"] == "n4"
        assert b["node_id"] == "n5"
