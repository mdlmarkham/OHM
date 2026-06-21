"""HTTP integration tests for internalized verification endpoints (OHM-bsse).

Tests source_tier + confidence ceilings, verification scan/decay,
TELOS signing fields, POST /outcome, POST /agent/synthesis
source_diversity, and snapshot endpoints.

Marks: integration (HTTP server required, slow setup/teardown).
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

pytestmark = pytest.mark.integration

from tests.conftest import _start_test_server, _request


@pytest.mark.xdist_group("server")
class TestSourceTierNodeEndpoint:
    """POST /node with source_tier and confidence — ADR-028 ceilings."""

    def test_post_node_with_valid_source_tier(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": "st-valid", "label": "Valid Tier", "type": "concept", "source_tier": "raw", "confidence": 0.2},
        )
        assert status == 201
        assert data.get("source_tier") == "raw"

    def test_post_node_with_official_tier(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": "st-official", "label": "Official Tier", "type": "concept", "source_tier": "official", "confidence": 0.85},
        )
        assert status == 201
        assert data.get("source_tier") == "official"

    def test_post_node_with_verified_tier_full_confidence(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": "st-verified", "label": "Verified Tier", "type": "concept", "source_tier": "verified", "confidence": 1.0},
        )
        assert status == 201
        assert data.get("source_tier") == "verified"

    def test_post_node_confidence_exceeds_ceiling_rejected(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": "st-exceed", "label": "Exceeds Ceiling", "type": "concept", "source_tier": "raw", "confidence": 0.9},
        )
        assert status == 400
        assert "ceiling" in data.get("message", "").lower() or "exceeds" in data.get("message", "").lower()

    def test_post_node_invalid_source_tier_rejected(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": "st-bad", "label": "Bad Tier", "type": "concept", "source_tier": "imaginary", "confidence": 0.5},
        )
        assert status == 400
        assert "source_tier" in data.get("message", "").lower() or "invalid" in data.get("message", "").lower()

    def test_post_node_without_source_tier_defaults_ok(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={"id": "st-none", "label": "No Tier", "type": "concept", "confidence": 0.99},
        )
        assert status == 201

    def test_get_node_returns_source_tier(self, test_server):
        port, _ = test_server
        _request(
            "POST",
            port,
            "/node",
            body={"id": "st-get", "label": "Get Tier", "type": "concept", "source_tier": "preliminary", "confidence": 0.6},
        )
        status, data = _request("GET", port, "/node/st-get")
        assert status == 200
        assert data.get("source_tier") == "preliminary"


@pytest.mark.xdist_group("server")
class TestSourceTierEdgeEndpoint:
    """POST /edge with source_tier — confidence ceiling enforcement."""

    def test_post_edge_with_valid_source_tier(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "e-st-from", "label": "From", "type": "concept"})
        _request("POST", port, "/node", body={"id": "e-st-to", "label": "To", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={"from": "e-st-from", "to": "e-st-to", "type": "CAUSES", "layer": "L3", "source_tier": "raw", "confidence": 0.2},
        )
        assert status == 201

    def test_post_edge_confidence_exceeds_ceiling_rejected(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "e-st-from2", "label": "From", "type": "concept"})
        _request("POST", port, "/node", body={"id": "e-st-to2", "label": "To", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={"from": "e-st-from2", "to": "e-st-to2", "type": "CAUSES", "layer": "L3", "source_tier": "raw", "confidence": 0.9},
        )
        assert status == 400


@pytest.mark.xdist_group("server")
class TestSynthesisSourceDiversity:
    """POST /agent/synthesis returns source_diversity enrichment (OHM-8q5d)."""

    def test_synthesis_returns_source_diversity(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "sd-cluster1", "label": "Cluster 1", "type": "concept"})
        _request("POST", port, "/node", body={"id": "sd-cluster2", "label": "Cluster 2", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/agent/synthesis",
            body={
                "label": "Synthesis Test",
                "content": "A synthesis of two clusters",
                "cluster_ids": ["sd-cluster1", "sd-cluster2"],
                "edge_type": "SUPPORTS",
                "confidence": 0.8,
            },
        )
        assert status == 201
        assert "source_diversity" in data
        assert "aggregate_score" in data["source_diversity"]
        assert data["source_diversity"]["cluster_count"] == 2

    def test_synthesis_missing_fields_returns_error(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/agent/synthesis",
            body={"label": "Missing content and clusters"},
        )
        assert status == 400

    def test_synthesis_no_valid_cluster_ids_returns_error(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/agent/synthesis",
            body={
                "label": "Bad Cluster IDs",
                "content": "Testing nonexistent cluster ids",
                "cluster_ids": ["nonexistent-node-xyz"],
            },
        )
        assert status == 400


@pytest.mark.xdist_group("server")
class TestTelosSigningFields:
    """GET /node/{id} includes TELOS signing fields if present."""

    def test_unsigned_node_has_null_signing_fields(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "telos-unsigned", "label": "Unsigned", "type": "concept"})
        status, data = _request("GET", port, "/node/telos-unsigned")
        assert status == 200
        assert data.get("write_signature") is None
        assert data.get("signing_key_id") is None
        assert data.get("signed_at") is None

    def test_signed_node_returns_signing_fields(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "telos-sign", "label": "To Sign", "type": "concept"})
        from ohm.graph.queries import sign_node_write

        sign_node_write(store.conn, "telos-sign", key=b"test-secret-key", key_id="test-key-1")
        status, data = _request("GET", port, "/node/telos-sign")
        assert status == 200
        assert data.get("write_signature") is not None
        assert data.get("signing_key_id") == "test-key-1"
        assert data.get("signed_at") is not None


@pytest.mark.xdist_group("server")
class TestVerificationScanEndpoint:
    """GET /admin/verification-scan — unverified edge scan (ADR-018)."""

    def test_verification_scan_returns_ok(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/admin/verification-scan")
        assert status == 200
        assert "unverified_edge_count" in data or "unverified_edges" in data

    def test_verification_scan_with_params(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/admin/verification-scan?days_threshold=7&confidence_threshold=0.9&causal_only=false")
        assert status == 200

    def test_verification_scan_finds_unverified_causal_edge(self, test_server):
        port, store = test_server
        from datetime import datetime, timedelta

        _request("POST", port, "/node", body={"id": "vscan-from", "label": "Cause", "type": "concept"})
        _request("POST", port, "/node", body={"id": "vscan-to", "label": "Effect", "type": "concept"})
        _request(
            "POST",
            port,
            "/edge",
            body={"from": "vscan-from", "to": "vscan-to", "type": "CAUSES", "layer": "L3", "confidence": 0.9},
        )
        store.conn.execute(
            "UPDATE ohm_edges SET created_at = ? WHERE from_node = 'vscan-from'",
            [(datetime.utcnow() - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S")],
        )
        status, data = _request("GET", port, "/admin/verification-scan?days_threshold=7")
        assert status == 200
        assert data.get("unverified_edge_count", 0) >= 1


@pytest.mark.xdist_group("server")
class TestVerificationDecayEndpoint:
    """POST /admin/verification-decay — confidence decay for unverified claims."""

    def test_verification_decay_dry_run(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/admin/verification-decay",
            body={"dry_run": True},
        )
        assert status == 200
        assert "dry_run" in data
        assert "decayed_count" in data

    def test_verification_decay_with_params(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/admin/verification-decay",
            body={
                "dry_run": True,
                "unverified_half_life_days": 15,
                "verified_half_life_days": 180,
                "min_confidence": 0.05,
            },
        )
        assert status == 200


@pytest.mark.xdist_group("server")
class TestOutcomeEndpoint:
    """POST /outcome — record verification outcomes for source reliability."""

    def test_outcome_records_true(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "out-true", "label": "True Outcome", "type": "concept"})
        _request("POST", port, "/observe/out-true", body={"type": "measurement", "value": 0.9})
        status, data = _request(
            "POST",
            port,
            "/outcome",
            body={"source_agent": "test-agent", "claim_node": "out-true", "outcome": True},
        )
        assert status in (200, 201)

    def test_outcome_records_false(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "out-false", "label": "False Outcome", "type": "concept"})
        _request("POST", port, "/observe/out-false", body={"type": "measurement", "value": 0.7})
        status, data = _request(
            "POST",
            port,
            "/outcome",
            body={"source_agent": "test-agent", "claim_node": "out-false", "outcome": False},
        )
        assert status in (200, 201)

    def test_outcome_missing_fields_returns_error(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/outcome", body={})
        assert status == 400


@pytest.mark.xdist_group("server")
class TestSnapshotEndpoints:
    """GET /admin/snapshots and GET /graph/at — DuckLake snapshot endpoints."""

    def test_admin_snapshots_returns_ok(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/admin/snapshots")
        assert status == 200
        assert "snapshots" in data

    def test_graph_at_missing_version_returns_error(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at")
        assert status == 400

    def test_graph_at_non_integer_version_returns_error(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at?version=abc")
        assert status == 400

    def test_graph_at_without_ducklake_returns_degraded(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at?version=1")
        assert status == 200
        assert data.get("degraded") is True


@pytest.mark.xdist_group("server")
class TestSoftDeleteVisibility:
    """Soft-deleted nodes are excluded from normal GET but visible in snapshots."""

    def test_deleted_node_not_in_get(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "del-me", "label": "Delete Me", "type": "concept"})
        store.conn.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'del-me'")
        status, data = _request("GET", port, "/node/del-me")
        assert status == 404

    def test_deleted_node_not_in_search(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "del-search", "label": "Delete Search", "type": "concept"})
        store.conn.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'del-search'")
        status, data = _request("GET", port, "/nodes?q=Delete+Search")
        if status == 200:
            nodes = data.get("nodes", data.get("results", []))
            node_ids = [n.get("id") for n in nodes]
            assert "del-search" not in node_ids
