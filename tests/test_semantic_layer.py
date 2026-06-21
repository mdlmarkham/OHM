"""Tests for the OHM semantic-layer module.

Covers:
- YAML metric loading
- SQL execution against in-memory DuckDB
- HTTP endpoint integration
- Metric correctness on a small constructed graph
"""

from __future__ import annotations

import pytest

from ohm.semantic_layer import list_metrics, load_metrics, run_metrics
from ohm.queries import create_node, create_edge, create_challenge, query_record_outcome


class TestSemanticLayerMetrics:
    """Unit tests for the semantic-layer engine."""

    def test_load_metrics_returns_three_definitions(self):
        metrics = load_metrics()
        assert set(metrics) == {"verification_rate", "challenge_ratio", "source_reliability_avg"}
        for name, definition in metrics.items():
            assert "sql" in definition
            assert definition["sql"].strip().upper().startswith("SELECT")

    def test_list_metrics_has_descriptions(self):
        descriptions = list_metrics()
        assert set(descriptions) == {"verification_rate", "challenge_ratio", "source_reliability_avg"}
        assert all(isinstance(d, str) for d in descriptions.values())

    def test_run_metrics_on_empty_graph(self, test_db):
        values = run_metrics(test_db, use_ibis=False)
        assert values["verification_rate"] is None
        assert values["challenge_ratio"] is None
        assert values["source_reliability_avg"] is None

    def test_verification_rate_and_challenge_ratio(self, test_db):
        # Three concepts
        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")
        c = create_node(test_db, label="C", node_type="concept", created_by="test_agent")
        d = create_node(test_db, label="D", node_type="concept", created_by="test_agent")

        # Two causal L3 edges; sign one to mark it "verified"
        e1 = create_edge(
            test_db,
            from_node=a["id"],
            to_node=b["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="test_agent",
        )
        e2 = create_edge(
            test_db,
            from_node=b["id"],
            to_node=c["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="test_agent",
        )
        # Non-causal L3 edge: counted in challenge-ratio denominator only
        create_edge(
            test_db,
            from_node=c["id"],
            to_node=d["id"],
            layer="L3",
            edge_type="SUPPORTS",
            created_by="test_agent",
        )

        # Sign one causal edge -> write_signature not NULL
        test_db.execute(
            "UPDATE ohm_edges SET write_signature = 'sig1' WHERE id = ?",
            [e1["id"]],
        )

        # Add a CHALLENGED_BY edge against e2 (challenge = 1, total L3 = 4)
        create_challenge(
            test_db,
            edge_id=e2["id"],
            reason="counter-evidence",
            created_by="critic_agent",
            confidence=0.4,
        )

        values = run_metrics(test_db, use_ibis=False)
        assert values["verification_rate"] == pytest.approx(1 / 2)
        assert values["challenge_ratio"] == pytest.approx(1 / 4)

    def test_source_reliability_avg(self, test_db):
        claim = create_node(test_db, label="Claim", node_type="concept", created_by="test_agent")

        query_record_outcome(
            test_db,
            source_agent="source_agent_1",
            claim_node=claim["id"],
            outcome=True,
            recorded_by="test_agent",
        )
        query_record_outcome(
            test_db,
            source_agent="source_agent_1",
            claim_node=claim["id"],
            outcome=False,
            recorded_by="test_agent",
        )
        query_record_outcome(
            test_db,
            source_agent="source_agent_2",
            claim_node=claim["id"],
            outcome=True,
            recorded_by="test_agent",
        )

        values = run_metrics(test_db, use_ibis=False)
        # (1 + 0 + 1) / 3 = 2/3
        assert values["source_reliability_avg"] == pytest.approx(2 / 3)


class TestSemanticLayerEndpoint:
    """HTTP integration tests for the semantic-layer endpoint."""

    def test_get_metrics_semantic_json(self, test_server):
        port, _store = test_server
        from tests.conftest import _request

        status, body = _request("GET", port, "/metrics/semantic")
        assert status == 200
        assert body["count"] == 3
        assert set(body["metrics"]) == {"verification_rate", "challenge_ratio", "source_reliability_avg"}
        assert body["metrics"]["verification_rate"] is None

    def test_get_metrics_semantic_prometheus(self, test_server):
        port, _store = test_server
        from tests.conftest import _request

        status, body = _request("GET", port, "/metrics/semantic?format=prometheus")
        assert status == 200
        assert "ohm_semantic_layer_metrics" in body
        assert "verification_rate" in body
