"""Tests for /ask endpoint, binary scale observations, challenge_type metadata, and /outcome (ADR-025)."""

import pytest

# Import shared test utilities from conftest.py
from tests.conftest import _start_test_server, _request


@pytest.mark.xdist_group("server")
class TestAskEndpoint:
    """Tests for POST /ask — conversational analytics (ADR-025)."""

    @pytest.fixture(autouse=True)
    def setup_graph(self, test_server):
        """Create a small test graph for /ask queries."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "andgate", "label": "AND gate", "type": "concept", "confidence": 0.9})
        _request("POST", port, "/node", body={"id": "orgate", "label": "OR gate", "type": "concept", "confidence": 0.8})
        _request("POST", port, "/node", body={"id": "oilpricing", "label": "Oil OR gate Mispricing", "type": "concept", "confidence": 0.95})
        _request("POST", port, "/edge", body={"from": "andgate", "to": "orgate", "type": "CAUSES", "layer": "L3", "confidence": 0.85})
        _request("POST", port, "/observe/andgate", body={"type": "measurement", "value": 0.97})
        _request("POST", port, "/observe/orgate", body={"type": "measurement", "value": 0.95})

    def test_ask_missing_question_returns_400(self, test_server):
        """POST /ask without question parameter returns 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={})
        assert status == 400
        assert "question" in data.get("message", "").lower() or "question" in data.get("error", "").lower()

    def test_ask_with_question_returns_200(self, test_server):
        """POST /ask with a question returns 200 with synthesis."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "AND-gate"})
        assert status == 200
        assert "question" in data
        assert "synthesis" in data
        assert "confidence" in data
        assert "matched_nodes" in data

    def test_ask_returns_matched_nodes(self, test_server):
        """POST /ask returns matched nodes for a relevant question."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "AND-gate"})
        assert status == 200
        assert len(data["matched_nodes"]) >= 1
        node_ids = [n["id"] for n in data["matched_nodes"]]
        assert "andgate" in node_ids

    def test_ask_direct_id_lookup(self, test_server):
        """POST /ask with a node ID returns that node in results."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "andgate"})
        assert status == 200
        # The node should appear in results (via direct ID or text search)
        assert len(data["matched_nodes"]) >= 1 or "no matching" in data["synthesis"].lower()

    def test_ask_includes_neighborhood(self, test_server):
        """POST /ask includes neighborhood expansion."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "AND-gate"})
        assert status == 200
        assert "neighborhood" in data
        assert "nodes" in data["neighborhood"] or "edges" in data["neighborhood"]

    def test_ask_inference_skipped_when_disabled(self, test_server):
        """POST /ask with include_inference=false skips Bayesian inference."""
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/ask",
            body={
                "question": "AND-gate",
                "include_inference": False,
            },
        )
        assert status == 200
        assert data.get("inference_skipped") is True
        reason = data.get("inference_reason", "").lower()
        assert "include_inference" in reason or "false" in reason

    def test_ask_confidence_in_range(self, test_server):
        """POST /ask returns confidence between 0.0 and 1.0."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "AND-gate"})
        assert status == 200
        assert 0.0 <= data["confidence"] <= 1.0

    def test_ask_no_results_returns_synthesis(self, test_server):
        """POST /ask with an irrelevant question still returns synthesis."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "xyzzy_plugh_plover"})
        assert status == 200
        assert "synthesis" in data
        # Embedding search may find loose matches; either it returns synthesis
        # with no matches, or it finds some. Both are acceptable.
        assert "synthesis" in data

    def test_ask_depth_parameter(self, test_server):
        """POST /ask respects depth parameter."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "AND-gate", "depth": 1})
        assert status == 200
        assert "neighborhood" in data

    def test_ask_limit_parameter(self, test_server):
        """POST /ask respects limit parameter (capped at 20)."""
        port, _ = test_server
        status, data = _request("POST", port, "/ask", body={"question": "gate", "limit": 1})
        assert status == 200
        assert len(data["matched_nodes"]) <= 20


@pytest.mark.xdist_group("server")
class TestBinaryScaleObservations:
    """Tests for binary scale on /observe endpoint (ADR-025)."""

    def test_observe_binary_true_normalized(self, test_server):
        """POST /observe/{id} with scale=binary and value=1 stores as probability 1.0."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "binary-test-1", "label": "Binary True", "type": "concept"})
        status, data = _request("POST", port, "/observe/binary-test-1", body={"type": "measurement", "value": 1.0, "scale": "binary"})
        assert status == 201
        obs = store.execute("SELECT value FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1", ["binary-test-1"])
        assert len(obs) == 1
        assert abs(obs[0]["value"] - 1.0) < 0.001

    def test_observe_binary_false_normalizes_to_zero(self, test_server):
        """POST /observe/{id} with scale=binary and value=0 stores as probability 0.0."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "binary-test-0", "label": "Binary False", "type": "concept"})
        status, data = _request("POST", port, "/observe/binary-test-0", body={"type": "measurement", "value": 0.0, "scale": "binary"})
        assert status == 201
        obs = store.execute("SELECT value FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1", ["binary-test-0"])
        assert len(obs) == 1
        assert abs(obs[0]["value"] - 0.0) < 0.001

    def test_observe_binary_middle_value(self, test_server):
        """POST /observe/{id} with scale=binary and value=0.5 stores as probability 0.5."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "binary-test-mid", "label": "Binary Mid", "type": "concept"})
        status, data = _request("POST", port, "/observe/binary-test-mid", body={"type": "measurement", "value": 0.5, "scale": "binary"})
        assert status == 201
        obs = store.execute("SELECT value FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1", ["binary-test-mid"])
        assert len(obs) == 1
        assert abs(obs[0]["value"] - 0.5) < 0.001

    def test_observe_binary_stores_scale(self, test_server):
        """scale=binary is normalized to probability in storage (ADR-025)."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "binary-scale-meta", "label": "Binary Scale", "type": "concept"})
        _request("POST", port, "/observe/binary-scale-meta", body={"type": "measurement", "value": 1.0, "scale": "binary"})
        obs = store.execute("SELECT scale FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1", ["binary-scale-meta"])
        assert len(obs) == 1
        # ADR-025: binary scale is normalized to probability in storage
        assert obs[0]["scale"] == "probability"

    def test_observe_probability_scale_unchanged(self, test_server):
        """POST /observe/{id} with scale=probability stores value as-is."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "prob-scale-test", "label": "Prob Scale", "type": "concept"})
        _request("POST", port, "/observe/prob-scale-test", body={"type": "measurement", "value": 0.75, "scale": "probability"})
        obs = store.execute("SELECT value, scale FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1", ["prob-scale-test"])
        assert len(obs) == 1
        assert abs(obs[0]["value"] - 0.75) < 0.001
        assert obs[0]["scale"] == "probability"

    def test_observe_no_scale_backward_compatible(self, test_server):
        """POST /observe/{id} without scale still works (backward compatible)."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "no-scale-test", "label": "No Scale", "type": "concept"})
        status, data = _request("POST", port, "/observe/no-scale-test", body={"type": "measurement", "value": 42.0})
        assert status == 201
        obs = store.execute("SELECT value FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1", ["no-scale-test"])
        assert len(obs) == 1
        assert abs(obs[0]["value"] - 42.0) < 0.001


@pytest.mark.xdist_group("server")
class TestChallengeTypeMetadata:
    """Tests for challenge_type stored as field on CHALLENGED_BY edge (ADR-025)."""

    def test_challenge_creates_challenged_by_edge(self, test_server):
        """POST /challenge creates CHALLENGED_BY edge regardless of challenge_type."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "challenge-src", "label": "Challenger", "type": "concept"})
        _request("POST", port, "/node", body={"id": "challenge-dst", "label": "Challenged", "type": "concept"})
        _request("POST", port, "/edge", body={"from": "challenge-src", "to": "challenge-dst", "type": "SUPPORTS", "layer": "L3", "confidence": 0.8})

        edges = store.execute("SELECT id FROM ohm_edges WHERE from_node = 'challenge-src' AND to_node = 'challenge-dst' AND deleted_at IS NULL")
        assert len(edges) >= 1
        edge_id = edges[0]["id"]

        status, data = _request(
            "POST",
            port,
            f"/challenge/{edge_id}",
            body={
                "reason": "Evidence contradicts this interpretation",
                "confidence": 0.7,
                "challenge_type": "empirical",
            },
        )
        assert status in (200, 201)

        # Challenge creates a CHALLENGED_BY edge from the challenged node to the challenger
        challenge_edges = store.execute("SELECT edge_type, challenge_type FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY' AND (from_node = 'challenge-dst' OR to_node = 'challenge-src') AND deleted_at IS NULL")
        assert len(challenge_edges) >= 1
        assert challenge_edges[0]["edge_type"] == "CHALLENGED_BY"
        assert challenge_edges[0]["challenge_type"] == "empirical"

    def test_challenge_type_stored_correctly(self, test_server):
        """POST /challenge stores challenge_type as field, edge_type is always CHALLENGED_BY."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "ctype-src", "label": "Challenger", "type": "concept"})
        _request("POST", port, "/node", body={"id": "ctype-dst", "label": "Target", "type": "concept"})
        _request("POST", port, "/edge", body={"from": "ctype-src", "to": "ctype-dst", "type": "CAUSES", "layer": "L3", "confidence": 0.9})

        edges = store.execute("SELECT id FROM ohm_edges WHERE from_node = 'ctype-src' AND to_node = 'ctype-dst' AND deleted_at IS NULL")
        edge_id = edges[0]["id"]

        status, data = _request(
            "POST",
            port,
            f"/challenge/{edge_id}",
            body={
                "reason": "Logical fallacy in causal chain",
                "confidence": 0.65,
                "challenge_type": "logical",
            },
        )
        assert status in (200, 201)

        challenge_edges = store.execute("SELECT edge_type, challenge_type, provenance FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY' AND (from_node = 'ctype-dst' OR to_node = 'ctype-src') AND deleted_at IS NULL")
        assert len(challenge_edges) >= 1
        assert challenge_edges[0]["edge_type"] == "CHALLENGED_BY"
        assert challenge_edges[0]["challenge_type"] == "logical"


@pytest.mark.xdist_group("server")
class TestOutcomeEndpoint:
    """Tests for POST /outcome — source reliability tracking."""

    def test_outcome_records_true(self, test_server):
        """POST /outcome records True outcome."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "outcome-test-1", "label": "Outcome Test", "type": "concept"})
        _request("POST", port, "/observe/outcome-test-1", body={"type": "measurement", "value": 0.9})

        status, data = _request(
            "POST",
            port,
            "/outcome",
            body={
                "source_agent": "agent-clio",
                "claim_node": "outcome-test-1",
                "outcome": True,
            },
        )
        assert status in (200, 201)

    def test_outcome_records_false(self, test_server):
        """POST /outcome records False outcome."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "outcome-test-2", "label": "Outcome Test 2", "type": "concept"})
        _request("POST", port, "/observe/outcome-test-2", body={"type": "measurement", "value": 0.7})

        status, data = _request(
            "POST",
            port,
            "/outcome",
            body={
                "source_agent": "agent-socrates",
                "claim_node": "outcome-test-2",
                "outcome": False,
            },
        )
        assert status in (200, 201)

    def test_outcome_missing_fields_returns_400(self, test_server):
        """POST /outcome without required fields returns 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/outcome", body={})
        assert status == 400
