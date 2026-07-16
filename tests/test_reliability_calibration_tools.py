"""Tests for OHM #910 — source reliability and calibration MCP tools + nudges.

Covers four layers:
  - Query functions (``query_source_reliability``, ``compute_agent_profile``)
    tested directly against an in-memory DuckDB via the ``test_db`` fixture.
  - HTTP endpoints (``/agent/reliability``, ``/agent/calibration``) tested via
    the ``test_server`` and ``auth_server`` fixtures.
  - MCP tool registration and dispatch mapping (unit tests).
  - Nudge generation (``overconfidence_divergence``, ``high_reliability_challenge``)
    tested at the unit level via ``generate_nudges`` and via HTTP.

Privacy: peer reliability lookups are anonymised by default; self-lookups
return the full named result.
"""

from __future__ import annotations

import pytest

from ohm.mcp.config import WRITE_TOOLS
from ohm.mcp.dispatch import build_request
from ohm.mcp.tools import all_tools
from ohm.server.nudges import generate_nudges


# ── Helpers ─────────────────────────────────────────────────────────────────


def _seed_outcomes(conn, agent, n_correct, n_incorrect, claim_node="claim_1"):
    """Insert outcome rows for an agent with explicit claimed_by."""
    for i in range(n_correct):
        conn.execute(
            "INSERT INTO ohm_outcomes (id, source_agent, claim_node, outcome, recorded_by, claimed_by, domain) "
            "VALUES (?, ?, ?, TRUE, ?, ?, '*')",
            [f"out_{agent}_c{i}", agent, claim_node, "tester", agent],
        )
    for i in range(n_incorrect):
        conn.execute(
            "INSERT INTO ohm_outcomes (id, source_agent, claim_node, outcome, recorded_by, claimed_by, domain) "
            "VALUES (?, ?, ?, FALSE, ?, ?, '*')",
            [f"out_{agent}_i{i}", agent, claim_node, "tester", agent],
        )


class _FakeStore:
    """Minimal store stand-in exposing .conn for nudge queries."""

    pass


# ── Query-layer tests ───────────────────────────────────────────────────────


class TestSourceReliabilityQuery:
    """Tests for query_source_reliability (queries layer)."""

    def test_returns_correct_p_accurate(self, test_db):
        from ohm.queries import query_source_reliability

        _seed_outcomes(test_db, "edr-scanner", n_correct=8, n_incorrect=2)
        result = query_source_reliability(test_db, "edr-scanner")
        assert result["p_accurate"] == pytest.approx(0.8, abs=1e-4)
        assert result["total_outcomes"] == 10
        assert result["accurate_count"] == 8
        assert result["false_positive_count"] == 2

    def test_false_positive_rate(self, test_db):
        from ohm.queries import query_source_reliability

        _seed_outcomes(test_db, "siem", n_correct=7, n_incorrect=3)
        result = query_source_reliability(test_db, "siem")
        assert result["false_positive_rate"] == pytest.approx(0.3, abs=1e-4)

    def test_low_confidence_warning_with_few_outcomes(self, test_db):
        from ohm.queries import query_source_reliability

        _seed_outcomes(test_db, "newbie", n_correct=1, n_incorrect=0)
        result = query_source_reliability(test_db, "newbie")
        assert result["low_confidence_warning"] is True
        assert result["total_outcomes"] == 1

    def test_no_low_confidence_warning_with_many_outcomes(self, test_db):
        from ohm.queries import query_source_reliability

        _seed_outcomes(test_db, "veteran", n_correct=5, n_incorrect=0)
        result = query_source_reliability(test_db, "veteran")
        assert result["low_confidence_warning"] is False

    def test_empty_agent_returns_none_p_accurate(self, test_db):
        from ohm.queries import query_source_reliability

        result = query_source_reliability(test_db, "ghost-agent")
        assert result["p_accurate"] is None
        assert result["total_outcomes"] == 0


class TestMyCalibrationQuery:
    """Tests for compute_agent_profile (calibration layer)."""

    def test_returns_all_expected_fields(self, test_db):
        from ohm.graph.calibration import compute_agent_profile

        profile = compute_agent_profile(test_db, "alice")
        for field in (
            "brier_score",
            "overconfidence_rate",
            "novelty_score",
            "max_loop_risk",
            "total_l3_l4_edges",
        ):
            assert field in profile, f"missing field: {field}"

    def test_brier_score_reflects_miscalibration(self, test_db):
        from ohm.queries import create_edge, create_node
        from ohm.graph.calibration import compute_agent_profile

        src = create_node(test_db, label="src", node_type="concept", created_by="alice")
        dst = create_node(test_db, label="dst", node_type="concept", created_by="alice")
        create_edge(
            test_db,
            from_node=src["id"],
            to_node=dst["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="alice",
            confidence=0.9,
        )
        _seed_outcomes(test_db, "alice", n_correct=0, n_incorrect=1, claim_node=src["id"])
        profile = compute_agent_profile(test_db, "alice")
        assert profile["brier_score"] > 0.5

    def test_n_predictions_counts_l3_l4_edges(self, test_db):
        from ohm.queries import create_edge, create_node
        from ohm.graph.calibration import compute_agent_profile

        src = create_node(test_db, label="s", node_type="concept", created_by="bob")
        dst = create_node(test_db, label="d", node_type="concept", created_by="bob")
        create_edge(
            test_db,
            from_node=src["id"],
            to_node=dst["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="bob",
            confidence=0.5,
        )
        profile = compute_agent_profile(test_db, "bob")
        assert profile["total_l3_l4_edges"] >= 1


# ── MCP tool registration + dispatch ────────────────────────────────────────


class TestMCPToolsAndDispatch:
    """Tool registration and dispatch mapping for the two new tools."""

    def test_source_reliability_tool_registered(self):
        names = {t.name for t in all_tools()}
        assert "ohm_source_reliability" in names

    def test_my_calibration_tool_registered(self):
        names = {t.name for t in all_tools()}
        assert "ohm_my_calibration" in names

    def test_source_reliability_not_write_tool(self):
        assert "ohm_source_reliability" not in WRITE_TOOLS

    def test_my_calibration_not_write_tool(self):
        assert "ohm_my_calibration" not in WRITE_TOOLS

    def test_dispatch_source_reliability_self(self):
        method, path, body = build_request("ohm_source_reliability", {}, "metis")
        assert method == "GET"
        assert path == "/agent/reliability"
        assert body is None

    def test_dispatch_source_reliability_peer(self):
        method, path, body = build_request(
            "ohm_source_reliability", {"agent_id": "observer"}, "metis"
        )
        assert method == "GET"
        assert "agent_id=observer" in path
        assert body is None

    def test_dispatch_my_calibration(self):
        method, path, body = build_request("ohm_my_calibration", {}, "metis")
        assert method == "GET"
        assert path == "/agent/calibration"
        assert body is None

    def test_source_reliability_tool_has_agent_id_param(self):
        tool = next(t for t in all_tools() if t.name == "ohm_source_reliability")
        assert "agent_id" in tool.inputSchema["properties"]
        assert "format" in tool.inputSchema["properties"]

    def test_my_calibration_tool_has_format_param(self):
        tool = next(t for t in all_tools() if t.name == "ohm_my_calibration")
        assert "format" in tool.inputSchema["properties"]


# ── HTTP endpoint tests ─────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestHTTPReliability:
    """Tests for GET /agent/reliability via the HTTP daemon."""

    def test_self_reliability_returns_named_result(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "ohm", n_correct=8, n_incorrect=2)
        status, data = _request("GET", port, "/agent/reliability")
        assert status == 200
        assert data["agent_id"] == "ohm"
        assert data["anonymized"] is False
        assert data["p_accurate"] == pytest.approx(0.8, abs=1e-4)
        assert data["n_outcomes"] == 10

    def test_self_reliability_with_explicit_agent_id(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "ohm", n_correct=6, n_incorrect=4)
        status, data = _request("GET", port, "/agent/reliability?agent_id=ohm")
        assert status == 200
        assert data["agent_id"] == "ohm"
        assert data["anonymized"] is False
        assert data["p_accurate"] == pytest.approx(0.6, abs=1e-4)

    def test_reliability_includes_n_outcomes_alias(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "ohm", n_correct=3, n_incorrect=1)
        status, data = _request("GET", port, "/agent/reliability")
        assert status == 200
        assert data["n_outcomes"] == 4
        assert data["n_outcomes"] == data["total_outcomes"]


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestHTTPCalibration:
    """Tests for GET /agent/calibration via the HTTP daemon."""

    def test_returns_caller_calibration(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/agent/calibration")
        assert status == 200
        assert data["agent_id"] == "ohm"
        for field in ("brier_score", "overconfidence_rate", "novelty_score", "loop_risk", "n_predictions"):
            assert field in data, f"missing field: {field}"

    def test_calibration_n_predictions_is_int(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/agent/calibration")
        assert status == 200
        assert isinstance(data["n_predictions"], int)


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestPrivacy:
    """Peer reliability lookups are anonymised by default."""

    def test_peer_lookup_is_anonymized(self, auth_server):
        port, store = auth_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "observer", n_correct=9, n_incorrect=1)
        status, data = _request(
            "GET", port, "/agent/reliability?agent_id=observer", token="test-token-abc"
        )
        assert status == 200
        assert data["anonymized"] is True
        assert data["agent_id"] != "observer"
        assert data["agent_id"].startswith("peer_")
        assert data["p_accurate"] == pytest.approx(0.9, abs=1e-4)
        assert "privacy_note" in data

    def test_self_lookup_not_anonymized(self, auth_server):
        port, store = auth_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "metis", n_correct=7, n_incorrect=3)
        status, data = _request(
            "GET", port, "/agent/reliability?agent_id=metis", token="test-token-abc"
        )
        assert status == 200
        assert data["anonymized"] is False
        assert data["agent_id"] == "metis"

    def test_omitted_agent_id_uses_caller(self, auth_server):
        port, store = auth_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "metis", n_correct=8, n_incorrect=2)
        status, data = _request(
            "GET", port, "/agent/reliability", token="test-token-abc"
        )
        assert status == 200
        assert data["anonymized"] is False
        assert data["agent_id"] == "metis"
        assert data["p_accurate"] == pytest.approx(0.8, abs=1e-4)

    def test_two_agents_do_not_leak_each_others_names(self, auth_server):
        port, store = auth_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "observer", n_correct=9, n_incorrect=1)
        _seed_outcomes(store.conn, "metis", n_correct=5, n_incorrect=5)
        # metis asks about observer → anonymised
        _, peer_data = _request(
            "GET", port, "/agent/reliability?agent_id=observer", token="test-token-abc"
        )
        assert "observer" not in str(peer_data.get("agent_id", ""))
        assert peer_data["anonymized"] is True
        # metis asks about self → named
        _, self_data = _request(
            "GET", port, "/agent/reliability?agent_id=metis", token="test-token-abc"
        )
        assert self_data["agent_id"] == "metis"
        assert self_data["anonymized"] is False


# ── Nudge unit tests ─────────────────────────────────────────────────────────


class TestOverconfidenceNudge:
    """overconfidence_divergence nudge: confidence > historical accuracy + 0.2."""

    def test_fires_when_confidence_exceeds_accuracy(self, test_db):
        _seed_outcomes(test_db, "alice", n_correct=5, n_incorrect=5)
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="edge",
            confidence=0.9,
            edge_type="CAUSES",
            agent="alice",
            store=store,
        )
        types = [n["type"] for n in nudges]
        assert "overconfidence_divergence" in types
        n = next(n for n in nudges if n["type"] == "overconfidence_divergence")
        assert n["severity"] == "warning"
        assert n["data"]["historical_accuracy"] == pytest.approx(0.5, abs=1e-4)
        assert n["data"]["divergence"] == pytest.approx(0.4, abs=1e-4)
        assert n["data"]["n_outcomes"] == 10

    def test_does_not_fire_when_confidence_within_range(self, test_db):
        _seed_outcomes(test_db, "alice", n_correct=8, n_incorrect=2)
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="edge",
            confidence=0.85,
            edge_type="CAUSES",
            agent="alice",
            store=store,
        )
        types = [n["type"] for n in nudges]
        assert "overconfidence_divergence" not in types

    def test_does_not_fire_with_few_outcomes(self, test_db):
        _seed_outcomes(test_db, "alice", n_correct=1, n_incorrect=0)
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="edge",
            confidence=0.99,
            edge_type="CAUSES",
            agent="alice",
            store=store,
        )
        types = [n["type"] for n in nudges]
        assert "overconfidence_divergence" not in types

    def test_does_not_fire_without_agent(self, test_db):
        _seed_outcomes(test_db, "alice", n_correct=5, n_incorrect=5)
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="edge",
            confidence=0.9,
            edge_type="CAUSES",
            store=store,
        )
        types = [n["type"] for n in nudges]
        assert "overconfidence_divergence" not in types

    def test_does_not_fire_for_non_edge_action(self, test_db):
        _seed_outcomes(test_db, "alice", n_correct=5, n_incorrect=5)
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="node",
            confidence=0.9,
            agent="alice",
            store=store,
        )
        types = [n["type"] for n in nudges]
        assert "overconfidence_divergence" not in types


class TestHighReliabilityChallengeNudge:
    """high_reliability_challenge nudge: challenging a >90% reliable source."""

    def test_fires_when_challenging_high_reliability_source(self, test_db):
        from ohm.queries import create_edge, create_node

        _seed_outcomes(test_db, "reliable-src", n_correct=19, n_incorrect=1)
        src = create_node(test_db, label="src", node_type="concept", created_by="reliable-src")
        dst = create_node(test_db, label="dst", node_type="concept", created_by="reliable-src")
        edge = create_edge(
            test_db,
            from_node=src["id"],
            to_node=dst["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="reliable-src",
            confidence=0.8,
        )
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="challenge",
            challenge_edge_id=edge["id"],
            store=store,
            agent="challenger",
        )
        types = [n["type"] for n in nudges]
        assert "high_reliability_challenge" in types
        n = next(n for n in nudges if n["type"] == "high_reliability_challenge")
        assert n["severity"] == "info"
        assert n["data"]["source_agent"] == "reliable-src"
        assert n["data"]["p_accurate"] == pytest.approx(0.95, abs=1e-4)
        assert n["data"]["n_outcomes"] == 20

    def test_does_not_fire_for_low_reliability_source(self, test_db):
        from ohm.queries import create_edge, create_node

        _seed_outcomes(test_db, "flaky-src", n_correct=5, n_incorrect=5)
        src = create_node(test_db, label="s", node_type="concept", created_by="flaky-src")
        dst = create_node(test_db, label="d", node_type="concept", created_by="flaky-src")
        edge = create_edge(
            test_db,
            from_node=src["id"],
            to_node=dst["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="flaky-src",
            confidence=0.5,
        )
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="challenge",
            challenge_edge_id=edge["id"],
            store=store,
            agent="challenger",
        )
        types = [n["type"] for n in nudges]
        assert "high_reliability_challenge" not in types

    def test_does_not_fire_with_few_outcomes(self, test_db):
        from ohm.queries import create_edge, create_node

        _seed_outcomes(test_db, "new-src", n_correct=1, n_incorrect=0)
        src = create_node(test_db, label="s", node_type="concept", created_by="new-src")
        dst = create_node(test_db, label="d", node_type="concept", created_by="new-src")
        edge = create_edge(
            test_db,
            from_node=src["id"],
            to_node=dst["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="new-src",
            confidence=0.5,
        )
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="challenge",
            challenge_edge_id=edge["id"],
            store=store,
            agent="challenger",
        )
        types = [n["type"] for n in nudges]
        assert "high_reliability_challenge" not in types

    def test_does_not_fire_without_challenge_edge_id(self, test_db):
        _seed_outcomes(test_db, "reliable-src", n_correct=19, n_incorrect=1)
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="challenge",
            store=store,
            agent="challenger",
        )
        types = [n["type"] for n in nudges]
        assert "high_reliability_challenge" not in types

    def test_does_not_fire_for_non_challenge_action(self, test_db):
        from ohm.queries import create_edge, create_node

        _seed_outcomes(test_db, "reliable-src", n_correct=19, n_incorrect=1)
        src = create_node(test_db, label="s", node_type="concept", created_by="reliable-src")
        dst = create_node(test_db, label="d", node_type="concept", created_by="reliable-src")
        edge = create_edge(
            test_db,
            from_node=src["id"],
            to_node=dst["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="reliable-src",
            confidence=0.8,
        )
        store = _FakeStore()
        store.conn = test_db
        nudges = generate_nudges(
            action="edge",
            challenge_edge_id=edge["id"],
            store=store,
            agent="challenger",
        )
        types = [n["type"] for n in nudges]
        assert "high_reliability_challenge" not in types


# ── Nudge HTTP integration ───────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestOverconfidenceNudgeHTTP:
    """overconfidence_divergence nudge fires via POST /edge."""

    def test_nudge_in_edge_response(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        _seed_outcomes(store.conn, "ohm", n_correct=5, n_incorrect=5)
        _request("POST", port, "/node", body={"id": "oc-src", "label": "src", "type": "concept"})
        _request("POST", port, "/node", body={"id": "oc-dst", "label": "dst", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "oc-src",
                "to": "oc-dst",
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.95,
            },
        )
        assert status == 201
        nudge_types = [n["type"] for n in data.get("nudges", [])]
        assert "overconfidence_divergence" in nudge_types


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestHighReliabilityChallengeNudgeHTTP:
    """high_reliability_challenge nudge fires via POST /challenge."""

    def test_nudge_in_challenge_response(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        from ohm.queries import create_edge, create_node

        _seed_outcomes(store.conn, "reliable-src", n_correct=19, n_incorrect=1)
        src = create_node(store.conn, label="rsrc", node_type="concept", created_by="reliable-src")
        dst = create_node(store.conn, label="rdst", node_type="concept", created_by="reliable-src")
        edge = create_edge(
            store.conn,
            from_node=src["id"],
            to_node=dst["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="reliable-src",
            confidence=0.8,
        )
        status, data = _request(
            "POST",
            port,
            f"/challenge/{edge['id']}",
            body={"reason": "I think this is wrong", "confidence": 0.6},
        )
        assert status == 201
        nudge_types = [n["type"] for n in data.get("nudges", [])]
        assert "high_reliability_challenge" in nudge_types
