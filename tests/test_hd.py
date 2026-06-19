"""Tests for hyperdimensional fingerprinting (OHM-yk7z, ADR-031)."""
from __future__ import annotations

import pytest
from ohm.hd import (
    HDError,
    DEFAULT_DIM,
    DEFAULT_SEED,
    FP_VERSION,
    random_vector,
    bind,
    disbind,
    majority_rule,
    hamming_similarity,
    fingerprint_text,
    fingerprint_node,
)


class TestHDBindDisbind:
    def test_bind_disbind_roundtrip(self):
        a = random_vector(seed=1)
        b = random_vector(seed=2)
        composite = bind(a, b)
        recovered = disbind(composite, b)
        assert recovered == a

    def test_bind_commutative(self):
        a = random_vector(seed=1)
        b = random_vector(seed=2)
        assert bind(a, b) == bind(b, a)

    def test_bind_self_inverse_produces_zero(self):
        a = random_vector(seed=1)
        result = bind(a, a)
        assert all(b == 0 for b in result)

    def test_bind_length_mismatch_raises(self):
        a = random_vector(dim=1000, seed=1)
        b = random_vector(dim=2000, seed=2)
        with pytest.raises(HDError, match="mismatch"):
            bind(a, b)

    def test_disbind_is_bind(self):
        a = random_vector(seed=1)
        b = random_vector(seed=2)
        assert disbind(bind(a, b), b) == bind(bind(a, b), b)


class TestHDSimilarity:
    def test_identical_vectors_similarity_one(self):
        v = random_vector(seed=42)
        assert hamming_similarity(v, v) == 1.0

    def test_random_vectors_similarity_near_half(self):
        a = random_vector(seed=1)
        b = random_vector(seed=2)
        sim = hamming_similarity(a, b)
        assert 0.47 < sim < 0.53

    def test_similar_labels_higher_similarity(self):
        fp1 = fingerprint_text("nuclear reactor safety")
        fp2 = fingerprint_text("nuclear reactor design")
        fp3 = fingerprint_text("completely different topic baking bread")
        sim_close = hamming_similarity(fp1, fp2)
        sim_far = hamming_similarity(fp1, fp3)
        assert sim_close > sim_far

    def test_opposite_vectors_similarity_zero(self):
        a = random_vector(seed=1)
        b = bytearray(~x & 0xFF for x in a)
        assert hamming_similarity(a, b) == 0.0

    def test_length_mismatch_raises(self):
        with pytest.raises(HDError, match="mismatch"):
            hamming_similarity(bytearray(10), bytearray(20))


class TestHDDeterminism:
    def test_same_node_same_fingerprint(self):
        fp1 = fingerprint_node(label="test", node_type="concept")
        fp2 = fingerprint_node(label="test", node_type="concept")
        assert fp1["fingerprint_hex"] == fp2["fingerprint_hex"]

    def test_different_seed_different_fingerprint(self):
        fp1 = fingerprint_node(label="test", node_type="concept", seed=1)
        fp2 = fingerprint_node(label="test", node_type="concept", seed=2)
        assert fp1["fingerprint_hex"] != fp2["fingerprint_hex"]

    def test_different_label_different_fingerprint(self):
        fp1 = fingerprint_node(label="alpha", node_type="concept")
        fp2 = fingerprint_node(label="beta", node_type="concept")
        assert fp1["fingerprint_hex"] != fp2["fingerprint_hex"]

    def test_same_label_different_type(self):
        fp1 = fingerprint_node(label="test", node_type="concept")
        fp2 = fingerprint_node(label="test", node_type="pattern")
        assert fp1["fingerprint_hex"] != fp2["fingerprint_hex"]


class TestHDMajorityRule:
    def test_identical_inputs_similarity_one(self):
        v = random_vector(seed=1)
        result = majority_rule([v, v, v])
        assert hamming_similarity(result, v) == 1.0

    def test_random_inputs_similarity_near_half(self):
        vecs = [random_vector(seed=i) for i in range(51)]
        result = majority_rule(vecs)
        for v in vecs:
            sim = hamming_similarity(result, v)
            assert 0.53 < sim < 0.59

    def test_single_vector_returns_copy(self):
        v = random_vector(seed=1)
        result = majority_rule([v])
        assert result == v

    def test_empty_raises(self):
        with pytest.raises(HDError, match="at least one"):
            majority_rule([])

    def test_wrong_length_raises(self):
        v = random_vector(dim=1000, seed=1)
        with pytest.raises(HDError, match="length"):
            majority_rule([v])


class TestFingerprintText:
    def test_empty_text_returns_zero_vector(self):
        fp = fingerprint_text("")
        assert len(fp) == (DEFAULT_DIM + 7) // 8
        assert all(b == 0 for b in fp)

    def test_whitespace_text_returns_zero_vector(self):
        fp = fingerprint_text("   ")
        assert all(b == 0 for b in fp)

    def test_deterministic(self):
        assert fingerprint_text("hello world") == fingerprint_text("hello world")

    def test_different_text_different_fp(self):
        assert fingerprint_text("alpha") != fingerprint_text("beta")


class TestFingerprintNode:
    def test_expected_keys(self):
        fp = fingerprint_node(label="test", node_type="concept")
        assert "fingerprint_hex" in fp
        assert "dimension" in fp
        assert "seed" in fp
        assert "method" in fp
        assert "components" in fp

    def test_method_tag(self):
        fp = fingerprint_node(label="test", node_type="concept")
        assert fp["method"] == FP_VERSION

    def test_dimension_default(self):
        fp = fingerprint_node(label="test", node_type="concept")
        assert fp["dimension"] == DEFAULT_DIM

    def test_components_list(self):
        fp = fingerprint_node(label="test", node_type="concept")
        assert "type" in fp["components"]
        assert "label" in fp["components"]
        assert "content" not in fp["components"]

    def test_content_in_components(self):
        fp = fingerprint_node(label="test", node_type="concept", content="some body text")
        assert "content" in fp["components"]

    def test_tags_in_components(self):
        fp = fingerprint_node(label="test", node_type="concept", tags=["tag1", "tag2"])
        assert "tags" in fp["components"]

    def test_provenance_in_components(self):
        fp = fingerprint_node(label="test", node_type="concept", provenance="research")
        assert "provenance" in fp["components"]

    def test_hex_length(self):
        fp = fingerprint_node(label="test", node_type="concept")
        n_bytes = (DEFAULT_DIM + 7) // 8
        assert len(fp["fingerprint_hex"]) == n_bytes * 2

    def test_content_changes_fingerprint(self):
        fp1 = fingerprint_node(label="test", node_type="concept")
        fp2 = fingerprint_node(label="test", node_type="concept", content="extra context")
        assert fp1["fingerprint_hex"] != fp2["fingerprint_hex"]

    def test_empty_primitive_raises(self):
        with pytest.raises(HDError, match="non-empty"):
            from ohm.inference.hd import _base_vector
            _base_vector("", dim=DEFAULT_DIM, seed=DEFAULT_SEED)

    def test_invalid_dim_raises(self):
        with pytest.raises(HDError, match="positive"):
            random_vector(dim=0, seed=1)


class TestComputeHDFingerprint:
    def test_fingerprint_node_via_sdk(self, test_db):
        from ohm.graph.methods import compute_hd_fingerprint

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n1', 'nuclear safety', 'concept', 'test', 'team', 'research', 0.9)"""
        )
        result = compute_hd_fingerprint(conn, "n1")
        assert result["node_id"] == "n1"
        assert result["label"] == "nuclear safety"
        assert result["type"] == "concept"
        assert result["method"] == FP_VERSION
        assert "fingerprint_hex" in result

    def test_missing_node_raises(self, test_db):
        from ohm.exceptions import NodeNotFoundError
        from ohm.graph.methods import compute_hd_fingerprint

        with pytest.raises(NodeNotFoundError):
            compute_hd_fingerprint(test_db, "nonexistent")

    def test_deleted_node_raises(self, test_db):
        from ohm.exceptions import NodeNotFoundError
        from ohm.graph.methods import compute_hd_fingerprint

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, deleted_at)
               VALUES ('n1', 'deleted', 'concept', 'test', 'team', CURRENT_TIMESTAMP)"""
        )
        with pytest.raises(NodeNotFoundError):
            compute_hd_fingerprint(conn, "n1")


class TestHDSimilaritySearch:
    def test_search_returns_similar_nodes(self, test_db):
        from ohm.graph.methods import hd_similarity_search

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n1', 'nuclear reactor safety', 'concept', 'test', 'team', 'research', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n2', 'nuclear reactor design', 'concept', 'test', 'team', 'research', 0.8)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n3', 'baking sourdough bread', 'concept', 'test', 'team', 'conversation', 0.7)"""
        )
        results = hd_similarity_search(conn, "n1", threshold=0.5)
        assert len(results) >= 1
        assert results[0]["node_id"] in ("n2", "n3")

    def test_search_excludes_self(self, test_db):
        from ohm.graph.methods import hd_similarity_search

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'test', 'team', 1.0)"""
        )
        results = hd_similarity_search(conn, "n1")
        node_ids = [r["node_id"] for r in results]
        assert "n1" not in node_ids

    def test_search_respects_limit(self, test_db):
        from ohm.graph.methods import hd_similarity_search

        conn = test_db
        for i in range(5):
            conn.execute(
                """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
                   VALUES (?, ?, 'concept', 'test', 'team', 1.0)""",
                [f"n{i}", f"node {i}", ],
            )
        results = hd_similarity_search(conn, "n0", limit=2, threshold=0.0)
        assert len(results) <= 2

    def test_search_missing_node_raises(self, test_db):
        from ohm.exceptions import NodeNotFoundError
        from ohm.graph.methods import hd_similarity_search

        with pytest.raises(NodeNotFoundError):
            hd_similarity_search(test_db, "nonexistent")

    def test_search_sorted_by_similarity_desc(self, test_db):
        from ohm.graph.methods import hd_similarity_search

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n1', 'nuclear reactor safety', 'concept', 'test', 'team', 'research', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n2', 'nuclear reactor design', 'concept', 'test', 'team', 'research', 0.8)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n3', 'baking sourdough bread', 'concept', 'test', 'team', 'conversation', 0.7)"""
        )
        results = hd_similarity_search(conn, "n1", threshold=0.0)
        sims = [r["hd_similarity"] for r in results]
        assert sims == sorted(sims, reverse=True)


class TestSDKHDFingerprint:
    def test_sdk_fingerprint(self, test_db):
        from ohm.framework.sdk import Graph

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test node', 'concept', 'test', 'team', 1.0)"""
        )
        with Graph(conn, actor="test") as g:
            fp = g.fingerprint("n1")
            assert fp["node_id"] == "n1"
            assert fp["method"] == FP_VERSION

    def test_sdk_hd_similarity_search(self, test_db):
        from ohm.framework.sdk import Graph

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n1', 'nuclear safety', 'concept', 'test', 'team', 'research', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n2', 'nuclear design', 'concept', 'test', 'team', 'research', 0.8)"""
        )
        with Graph(conn, actor="test") as g:
            results = g.hd_similarity_search("n1", threshold=0.0)
            assert len(results) >= 1
            assert all("hd_similarity" in r for r in results)


class TestUpdateHDFingerprint:
    def test_update_stores_fingerprint(self, test_db):
        from ohm.graph.queries import update_node_hd_fingerprint

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test node', 'concept', 'test', 'team', 1.0)"""
        )
        result = update_node_hd_fingerprint(conn, "n1")
        assert result["stored"] is True
        assert result["node_id"] == "n1"
        row = conn.execute("SELECT hd_fingerprint FROM ohm_nodes WHERE id = 'n1'").fetchone()
        assert row[0] is not None
        assert len(row[0]) == 1250

    def test_update_missing_node_raises(self, test_db):
        from ohm.exceptions import NodeNotFoundError
        from ohm.graph.queries import update_node_hd_fingerprint

        with pytest.raises(NodeNotFoundError):
            update_node_hd_fingerprint(test_db, "nonexistent")

    def test_sdk_update_hd_fingerprint(self, test_db):
        from ohm.framework.sdk import Graph

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'test', 'team', 1.0)"""
        )
        with Graph(conn, actor="test") as g:
            result = g.update_hd_fingerprint("n1")
            assert result["stored"] is True


class TestHDMembershipSearch:
    def test_search_stored_fingerprints(self, test_db):
        from ohm.graph.queries import update_node_hd_fingerprint, hd_membership_search

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n1', 'nuclear reactor safety', 'concept', 'test', 'team', 'research', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n2', 'nuclear reactor design', 'concept', 'test', 'team', 'research', 0.8)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
               VALUES ('n3', 'baking sourdough bread', 'concept', 'test', 'team', 'conversation', 0.7)"""
        )
        update_node_hd_fingerprint(conn, "n1")
        update_node_hd_fingerprint(conn, "n2")
        update_node_hd_fingerprint(conn, "n3")

        fp_hex = conn.execute("SELECT hd_fingerprint FROM ohm_nodes WHERE id = 'n1'").fetchone()[0].hex()
        results = hd_membership_search(conn, fp_hex, threshold=0.0, limit=10)
        assert len(results) >= 2
        n1_result = [r for r in results if r["node_id"] == "n1"]
        assert len(n1_result) == 1
        assert n1_result[0]["hd_similarity"] == 1.0
        sims = [r["hd_similarity"] for r in results]
        assert sims == sorted(sims, reverse=True)

    def test_search_node_type_filter(self, test_db):
        from ohm.graph.queries import update_node_hd_fingerprint, hd_membership_search

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'test', 'team', 1.0)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n2', 'test', 'pattern', 'test', 'team', 1.0)"""
        )
        update_node_hd_fingerprint(conn, "n1")
        update_node_hd_fingerprint(conn, "n2")

        fp_hex = conn.execute("SELECT hd_fingerprint FROM ohm_nodes WHERE id = 'n1'").fetchone()[0].hex()
        results = hd_membership_search(conn, fp_hex, node_type="pattern")
        assert all(r["type"] == "pattern" for r in results)

    def test_search_empty_hex_raises(self, test_db):
        from ohm.graph.queries import hd_membership_search

        with pytest.raises(ValueError, match="non-empty"):
            hd_membership_search(test_db, "")

    def test_sdk_hd_membership_search(self, test_db):
        from ohm.framework.sdk import Graph

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test alpha', 'concept', 'test', 'team', 1.0)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n2', 'test beta', 'concept', 'test', 'team', 1.0)"""
        )
        with Graph(conn, actor="test") as g:
            g.update_hd_fingerprint("n1")
            g.update_hd_fingerprint("n2")
            fp_hex = conn.execute("SELECT hd_fingerprint FROM ohm_nodes WHERE id = 'n1'").fetchone()[0].hex()
            results = g.hd_membership_search(fp_hex, threshold=0.0)
            assert len(results) >= 1


class TestBatchUpdateHDFingerprints:
    def test_batch_updates_missing_fingerprints(self, test_db):
        from ohm.graph.queries import batch_update_hd_fingerprints

        conn = test_db
        for i in range(5):
            conn.execute(
                """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
                   VALUES (?, ?, 'concept', 'test', 'team', 1.0)""",
                [f"n{i}", f"node {i}"],
            )
        result = batch_update_hd_fingerprints(conn)
        assert result["updated"] == 5
        assert result["skipped"] == 0
        nulls = conn.execute("SELECT count(*) FROM ohm_nodes WHERE hd_fingerprint IS NULL AND deleted_at IS NULL").fetchone()[0]
        assert nulls == 0

    def test_batch_skips_already_fingerprinted(self, test_db):
        from ohm.graph.queries import update_node_hd_fingerprint, batch_update_hd_fingerprints

        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'test', 'team', 1.0)"""
        )
        update_node_hd_fingerprint(conn, "n1")
        result = batch_update_hd_fingerprints(conn)
        assert result["updated"] == 0

    def test_sdk_batch_update(self, test_db):
        from ohm.framework.sdk import Graph

        conn = test_db
        for i in range(3):
            conn.execute(
                """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
                   VALUES (?, ?, 'concept', 'test', 'team', 1.0)""",
                [f"n{i}", f"node {i}"],
            )
        with Graph(conn, actor="test") as g:
            result = g.batch_update_hd_fingerprints()
            assert result["updated"] == 3


class TestValidateHDFingerprint:
    def test_valid_bytes(self):
        from ohm.validation import validate_hd_fingerprint

        fp = bytes(1250)
        result = validate_hd_fingerprint(fp, dimensions=10000)
        assert result == fp

    def test_none_returns_none(self):
        from ohm.validation import validate_hd_fingerprint

        assert validate_hd_fingerprint(None, dimensions=10000) is None

    def test_wrong_size_raises(self):
        from ohm.exceptions import ValidationError
        from ohm.validation import validate_hd_fingerprint

        with pytest.raises(ValidationError):
            validate_hd_fingerprint(bytes(100), dimensions=10000)
