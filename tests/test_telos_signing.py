"""Tests for TELOS signing (OHM-enwb, ADR-035)."""
from __future__ import annotations

import pytest
from ohm.graph.crypto import canonical_payload, sign_hmac, verify_hmac, sign_write, verify_write
from ohm.graph.queries import sign_node_write, sign_edge_write, verify_node_write, verify_edge_write
from ohm.framework.sdk import Graph


class TestCanonicalPayload:
    def test_node_deterministic(self):
        r1 = {"id": "n1", "label": "test", "type": "concept", "created_by": "a1", "confidence": 0.9}
        r2 = {"id": "n1", "label": "test", "type": "concept", "created_by": "a1", "confidence": 0.9}
        assert canonical_payload(r1, kind="node") == canonical_payload(r2, kind="node")

    def test_excludes_extra_fields(self):
        r1 = {"id": "n1", "label": "test", "type": "concept", "created_by": "a1", "extra_field": "ignored"}
        r2 = {"id": "n1", "label": "test", "type": "concept", "created_by": "a1"}
        assert canonical_payload(r1, kind="node") == canonical_payload(r2, kind="node")

    def test_edge_kind(self):
        r = {"id": "e1", "from_node": "n1", "to_node": "n2", "edge_type": "CAUSES", "layer": "L3", "created_by": "a1"}
        payload = canonical_payload(r, kind="edge")
        assert b"from_node" in payload
        assert b"to_node" in payload

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="kind"):
            canonical_payload({}, kind="invalid")


class TestHmacSigning:
    def test_sign_verify_roundtrip(self):
        key = b"secret-key-12345678901234567890"
        payload = b"test payload"
        sig = sign_hmac(payload, key)
        assert verify_hmac(payload, sig, key)

    def test_wrong_key_fails(self):
        key1 = b"secret-key-12345678901234567890"
        key2 = b"other-key-1234567890123456789012"
        payload = b"test payload"
        sig = sign_hmac(payload, key1)
        assert not verify_hmac(payload, sig, key2)

    def test_tamper_detection(self):
        key = b"secret-key-12345678901234567890"
        sig = sign_hmac(b"original", key)
        assert not verify_hmac(b"tampered", sig, key)


class TestSignWrite:
    def test_sign_node(self):
        key = b"test-key-123456789012345678901234"
        record = {"id": "n1", "label": "test", "type": "concept", "created_by": "a1", "confidence": 0.9}
        result = sign_write(record, kind="node", key=key)
        assert "write_signature" in result
        assert result["write_signature"].startswith("hmac-sha256:")
        assert "signing_key_id" in result
        assert "signed_at" in result

    def test_verify_node(self):
        key = b"test-key-123456789012345678901234"
        record = {"id": "n1", "label": "test", "type": "concept", "created_by": "a1", "confidence": 0.9}
        signed = sign_write(record, kind="node", key=key)
        record.update(signed)
        assert verify_write(record, kind="node", key=key)

    def test_unsigned_returns_false(self):
        assert not verify_write({}, kind="node", key=b"key")

    def test_tampered_record_fails(self):
        key = b"test-key-123456789012345678901234"
        record = {"id": "n1", "label": "test", "type": "concept", "created_by": "a1", "confidence": 0.9}
        signed = sign_write(record, kind="node", key=key)
        record.update(signed)
        record["label"] = "tampered"
        assert not verify_write(record, kind="node", key=key)


class TestSignNodeWrite:
    def test_sign_and_verify(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'a1', 'team', 0.9)"""
        )
        key = b"test-key-123456789012345678901234"
        result = sign_node_write(conn, "n1", key=key)
        assert result["write_signature"].startswith("hmac-sha256:")
        verify_result = verify_node_write(conn, "n1", key=key)
        assert verify_result["verified"] is True

    def test_verify_unsigned(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'a1', 'team', 0.9)"""
        )
        result = verify_node_write(conn, "n1", key=b"any-key-1234567890123456789012")
        assert result["verified"] is False


class TestSignEdgeWrite:
    def test_sign_and_verify(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'from', 'concept', 'a1', 'team', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n2', 'to', 'concept', 'a1', 'team', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
               VALUES ('e1', 'n1', 'n2', 'CAUSES', 'L3', 'a1', 0.8)"""
        )
        key = b"test-key-123456789012345678901234"
        sign_edge_write(conn, "e1", key=key)
        result = verify_edge_write(conn, "e1", key=key)
        assert result["verified"] is True


class TestSDKSigning:
    def test_sdk_sign_and_verify(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'a1', 'team', 0.9)"""
        )
        key = b"test-key-123456789012345678901234"
        with Graph(conn, actor="test") as g:
            g._signing_key = key
            g.sign_node("n1")
            result = g.verify_node("n1")
            assert result["verified"] is True

    def test_sdk_no_key_raises(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'test', 'concept', 'a1', 'team', 0.9)"""
        )
        with Graph(conn, actor="test") as g:
            with pytest.raises(ValueError, match="No signing key"):
                g.sign_node("n1")
