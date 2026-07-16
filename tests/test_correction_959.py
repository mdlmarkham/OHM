"""Tests for OHM-959: First-class correction workflow with immutable-node supersession."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from tests.conftest import _request, _start_test_server  # noqa: E402


@pytest.fixture
def correction_server(tmp_path):
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "correction_test.duckdb")
    store = OhmStore(
        db_path=db_path,
        agent_name="test_agent",
        embedding_backend=NullBackend(dimensions=768),
    )
    port, server, thread = _start_test_server(store, no_auth=True)
    yield port
    server.shutdown()
    thread.join(timeout=5)
    store.close()


@pytest.fixture
def target_node(correction_server):
    status, data = _request("POST", correction_server, "/node", {
        "id": "claim-brent-100",
        "label": "Brent oil price is $100/bbl",
        "type": "concept",
        "confidence": 0.5,
    })
    assert status == 201
    return data


@pytest.fixture
def sample_correction(correction_server, target_node):
    status, data = _request("POST", correction_server, "/correction/propose", {
        "old_node_id": target_node["id"],
        "reason": "Brent price was actually $95/bbl on that date",
        "field": "label",
        "old_value": "Brent oil price is $100/bbl",
        "new_value": "Brent oil price is $95/bbl",
        "severity": "moderate",
    })
    assert status == 201
    return data


# ── Propose ──────────────────────────────────────────────────────────────


class TestProposeCorrection:
    def test_propose_correction(self, correction_server, target_node):
        status, data = _request("POST", correction_server, "/correction/propose", {
            "old_node_id": target_node["id"],
            "reason": "Factually incorrect",
        })
        assert status == 201
        assert data["type"] == "decision"

    def test_propose_requires_old_node_id(self, correction_server):
        status, _ = _request("POST", correction_server, "/correction/propose", {
            "reason": "test",
        })
        assert status == 400

    def test_propose_requires_reason(self, correction_server, target_node):
        status, _ = _request("POST", correction_server, "/correction/propose", {
            "old_node_id": target_node["id"],
        })
        assert status == 400

    def test_propose_nonexistent_node(self, correction_server):
        status, _ = _request("POST", correction_server, "/correction/propose", {
            "old_node_id": "nonexistent",
            "reason": "test",
        })
        assert status == 404


# ── Commit ───────────────────────────────────────────────────────────────


class TestCommitCorrection:
    def test_commit_correction(self, correction_server, sample_correction):
        status, data = _request("POST", correction_server, "/correction/commit", {
            "correction_id": sample_correction["id"],
        })
        assert status == 200
        assert data["status"] == "committed"

    def test_commit_requires_id(self, correction_server):
        status, _ = _request("POST", correction_server, "/correction/commit", {})
        assert status == 400

    def test_commit_already_committed_fails(self, correction_server, sample_correction):
        """Committing an already-committed correction returns 409 (OHM-961)."""
        _request("POST", correction_server, "/correction/commit", {
            "correction_id": sample_correction["id"],
        })
        status, _ = _request("POST", correction_server, "/correction/commit", {
            "correction_id": sample_correction["id"],
        })
        assert status == 409

    def test_commit_rejected_correction_fails(self, correction_server, sample_correction):
        """Committing a rejected correction returns 409 (OHM-961)."""
        _request("POST", correction_server, "/correction/reject", {
            "correction_id": sample_correction["id"],
        })
        status, _ = _request("POST", correction_server, "/correction/commit", {
            "correction_id": sample_correction["id"],
        })
        assert status == 409


# ── Reject ───────────────────────────────────────────────────────────────


class TestRejectCorrection:
    def test_reject_correction(self, correction_server, sample_correction):
        status, data = _request("POST", correction_server, "/correction/reject", {
            "correction_id": sample_correction["id"],
            "rejection_reason": "Insufficient evidence",
        })
        assert status == 200
        assert data["status"] == "rejected"

    def test_reject_requires_id(self, correction_server):
        status, _ = _request("POST", correction_server, "/correction/reject", {})
        assert status == 400

    def test_reject_already_committed_fails(self, correction_server, sample_correction):
        """Rejecting an already-committed correction returns 409 (OHM-961)."""
        _request("POST", correction_server, "/correction/commit", {
            "correction_id": sample_correction["id"],
        })
        status, _ = _request("POST", correction_server, "/correction/reject", {
            "correction_id": sample_correction["id"],
        })
        assert status == 409


# ── List corrections ─────────────────────────────────────────────────────


class TestListCorrections:
    def test_list_empty(self, correction_server):
        status, data = _request("GET", correction_server, "/corrections")
        assert status == 200
        assert "corrections" in data

    def test_list_after_propose(self, correction_server, sample_correction, target_node):
        status, data = _request("GET", correction_server, f"/corrections?node_id={target_node['id']}")
        assert status == 200
        assert data["count"] >= 1

    def test_list_filter_by_status(self, correction_server, sample_correction):
        """GET /corrections?status=proposed filters by correction status (OHM-961)."""
        # Should find the proposed correction
        status, data = _request("GET", correction_server, "/corrections?status=proposed")
        assert status == 200
        assert data["count"] >= 1

        # Should NOT find any committed corrections
        status, data = _request("GET", correction_server, "/corrections?status=committed")
        assert status == 200
        assert data["count"] == 0


# ── MCP tool schemas ─────────────────────────────────────────────────────


class TestMCPSchemas959:
    def test_correction_tools_present(self):
        from ohm.mcp.tools import all_tools
        tool_names = {t.name for t in all_tools()}
        expected = {"ohm_propose_correction", "ohm_commit_correction", "ohm_reject_correction", "ohm_corrections"}
        assert expected.issubset(tool_names)

    def test_tool_count_76(self):
        from ohm.mcp.tools import all_tools
        assert len(all_tools()) == 76


# ── MCP dispatch ─────────────────────────────────────────────────────────


class TestMCPDispatch959:
    def test_dispatch_propose(self):
        from ohm.mcp.dispatch import build_request
        m, p, b = build_request("ohm_propose_correction", {
            "old_node_id": "n1", "reason": "test",
        }, "agent")
        assert m == "POST"
        assert p == "/correction/propose"

    def test_dispatch_commit(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_commit_correction", {"correction_id": "c1"}, "agent")
        assert m == "POST"
        assert p == "/correction/commit"

    def test_dispatch_reject(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_reject_correction", {"correction_id": "c1"}, "agent")
        assert m == "POST"
        assert p == "/correction/reject"

    def test_dispatch_corrections(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_corrections", {"node_id": "n1"}, "agent")
        assert m == "GET"
        assert "node_id=n1" in p


# ── Schema tests ─────────────────────────────────────────────────────────


class TestSchema959:
    def test_corrects_edge_in_l3(self):
        from ohm.graph.schema import DEFAULT_SCHEMA
        assert "CORRECTS" in DEFAULT_SCHEMA.layer_edge_types["L3"]

    def test_schema_version_058(self):
        from ohm.graph.schema import SCHEMA_VERSION
        assert SCHEMA_VERSION == "0.58.0"