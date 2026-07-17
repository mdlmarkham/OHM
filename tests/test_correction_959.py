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


@pytest.fixture
def auth_server(tmp_path):
    """Server with two authenticated agents for testing approval flows."""
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "auth_correction_test.duckdb")
    store = OhmStore(
        db_path=db_path,
        agent_name="ohmd",
        embedding_backend=NullBackend(dimensions=768),
    )
    port, server, thread = _start_test_server(
        store,
        tokens={"tok-proposer": "proposer", "tok-reviewer": "reviewer"},
    )
    yield port
    server.shutdown()
    thread.join(timeout=5)
    store.close()


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


# ── Confidence-ceiling second-approval (OHM-962) ────────────────────────


class TestConfidenceCeilingApproval:
    """Tests for the OHM-962 fix: non-proposer agents can supply the second
    approval on high-confidence corrections without force=true."""

    def test_proposer_alone_rejected_on_high_confidence(self, auth_server):
        """Proposer's commit on a high-confidence node is rejected pending a
        second approver."""
        s, _ = _request("POST", auth_server, "/node", {
            "id": "hc-claim", "label": "High confidence claim", "type": "concept", "confidence": 0.9,
        }, token="tok-proposer")
        assert s == 201

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "hc-claim", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        s, data = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")
        assert s == 400
        assert "Second distinct approving agent required" in data.get("message", "")

    def test_second_agent_approves_and_finalizes(self, auth_server):
        """A distinct second agent can call commit (no force) to supply the
        second approval, and the correction finalizes."""
        _request("POST", auth_server, "/node", {
            "id": "hc-claim2", "label": "HC claim 2", "type": "concept", "confidence": 0.9,
        }, token="tok-proposer")

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "hc-claim2", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        # Proposer's first commit: rejected, records proposer as approver 1
        _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")

        # Reviewer's commit (no force): should be accepted as second approval
        s, data = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-reviewer")
        assert s == 200
        assert data["status"] == "committed"

    def test_same_agent_approving_twice_insufficient(self, auth_server):
        """The proposer calling commit twice still only has 1 distinct approver."""
        _request("POST", auth_server, "/node", {
            "id": "hc-claim3", "label": "HC claim 3", "type": "concept", "confidence": 0.9,
        }, token="tok-proposer")

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "hc-claim3", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        # First attempt
        s1, _ = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")
        assert s1 == 400

        # Second attempt by same agent
        s2, _ = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")
        assert s2 == 400

    def test_non_proposer_without_force_on_low_confidence_rejected(self, auth_server):
        """A non-proposer agent committing a low-confidence correction without
        force is still rejected by the authority check."""
        _request("POST", auth_server, "/node", {
            "id": "lc-claim", "label": "Low confidence claim", "type": "concept", "confidence": 0.3,
        }, token="tok-proposer")

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "lc-claim", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        # Reviewer tries to commit without force — should be rejected by authority
        s, data = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-reviewer")
        assert s == 400
        assert "authority check" in data.get("message", "")


# ── Approval modes (OHM-964) ────────────────────────────────────────────


class TestApprovalModes:
    """Tests for OHM-964: configurable approval modes (actor/family/human)."""

    def test_family_mode_same_family_blocked(self, auth_server, monkeypatch):
        """In family mode, two agents from the same family cannot satisfy the
        second-approval requirement."""
        monkeypatch.setenv("OHM_CORRECTION_APPROVAL_MODE", "family")

        _request("POST", auth_server, "/node", {
            "id": "hc-fam1", "label": "HC family test", "type": "concept", "confidence": 0.9,
        }, token="tok-proposer")

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "hc-fam1", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        # Proposer commits — records family, rejected pending second family
        s1, _ = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")
        assert s1 == 400
        assert "family" in str(s1).lower() or "family" in str(_request("POST", auth_server, "/correction/commit", {"correction_id": corr["id"]}, token="tok-proposer")[1]).lower()

    def test_family_mode_cross_family_approved(self, tmp_path, monkeypatch):
        """In family mode, two agents from different families can approve."""
        monkeypatch.setenv("OHM_CORRECTION_APPROVAL_MODE", "family")
        monkeypatch.delenv("OHM_DISABLE_CONCURRENCY_GUARD", raising=False)

        from ohm.graph.embeddings import NullBackend
        from ohm.store import OhmStore

        db_path = str(tmp_path / "fam_test.duckdb")
        store = OhmStore(
            db_path=db_path,
            agent_name="ohmd",
            embedding_backend=NullBackend(dimensions=768),
        )
        port, server, thread = _start_test_server(
            store,
            tokens={"tok-alpha-1": "alpha-1", "tok-beta-1": "beta-1"},
        )

        try:
            _request("POST", port, "/node", {
                "id": "hc-cross", "label": "HC cross-family", "type": "concept", "confidence": 0.9,
            }, token="tok-alpha-1")

            s, corr = _request("POST", port, "/correction/propose", {
                "old_node_id": "hc-cross", "reason": "Wrong",
            }, token="tok-alpha-1")
            assert s == 201

            # Alpha-1 commits — records alpha family, rejected
            s1, _ = _request("POST", port, "/correction/commit", {
                "correction_id": corr["id"],
            }, token="tok-alpha-1")
            assert s1 == 400

            # Beta-1 commits — different family, should finalize
            s2, data = _request("POST", port, "/correction/commit", {
                "correction_id": corr["id"],
            }, token="tok-beta-1")
            assert s2 == 200
            assert data["status"] == "committed"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            store.close()

    def test_human_mode_transitions_to_pending(self, auth_server, monkeypatch):
        """In human mode, a high-confidence commit transitions to pending_human
        and returns an approval token."""
        monkeypatch.setenv("OHM_CORRECTION_APPROVAL_MODE", "human")

        _request("POST", auth_server, "/node", {
            "id": "hc-human", "label": "HC human test", "type": "concept", "confidence": 0.9,
        }, token="tok-proposer")

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "hc-human", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        s, data = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")
        assert s == 400
        assert "Human approval required" in data.get("message", "")
        assert "approval_token" in data.get("message", "") or "token" in data.get("message", "").lower()

    def test_human_mode_approve_endpoint_finalizes(self, auth_server, monkeypatch):
        """POST /correction/approve with the correct token finalizes the correction."""
        import re
        monkeypatch.setenv("OHM_CORRECTION_APPROVAL_MODE", "human")

        _request("POST", auth_server, "/node", {
            "id": "hc-human2", "label": "HC human approve", "type": "concept", "confidence": 0.9,
        }, token="tok-proposer")

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "hc-human2", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        # Trigger pending_human and extract token
        s, data = _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")
        msg = data.get("message", "")
        token_match = re.search(r"token[:\s]+([a-f0-9-]+)", msg, re.IGNORECASE)
        assert token_match, f"No approval token found in message: {msg}"
        approval_token = token_match.group(1)

        # Approve with the token
        s, data = _request("POST", auth_server, "/correction/approve", {
            "correction_id": corr["id"],
            "approval_token": approval_token,
            "reviewer": "human-reviewer",
        }, token="tok-reviewer")
        assert s == 200
        assert data["status"] == "committed"

    def test_human_mode_wrong_token_rejected(self, auth_server, monkeypatch):
        """POST /correction/approve with wrong token returns 400."""
        monkeypatch.setenv("OHM_CORRECTION_APPROVAL_MODE", "human")

        _request("POST", auth_server, "/node", {
            "id": "hc-human3", "label": "HC wrong token", "type": "concept", "confidence": 0.9,
        }, token="tok-proposer")

        s, corr = _request("POST", auth_server, "/correction/propose", {
            "old_node_id": "hc-human3", "reason": "Wrong",
        }, token="tok-proposer")
        assert s == 201

        # Trigger pending_human
        _request("POST", auth_server, "/correction/commit", {
            "correction_id": corr["id"],
        }, token="tok-proposer")

        # Try with wrong token
        s, _ = _request("POST", auth_server, "/correction/approve", {
            "correction_id": corr["id"],
            "approval_token": "wrong-token-xyz",
        }, token="tok-reviewer")
        assert s == 400