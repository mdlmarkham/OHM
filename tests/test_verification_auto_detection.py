"""Tests for OHM verification auto-detection and nudge pipeline.

Verifies detect_verifiable_claims, create_verification_nudge,
record_verification_outcome, and list_pending_verifications across
the query, SDK, and handler layers.
"""

from __future__ import annotations

import json

import pytest

from ohm.schema import LAYER_EDGE_TYPES, initialize_schema
from ohm.queries import (
    create_node,
    create_edge,
    query_record_outcome,
    detect_verifiable_claims,
    create_verification_nudge,
    record_verification_outcome,
    list_pending_verifications,
)


class TestSchemaEdgeType:
    def test_nudges_for_verification_in_l3(self):
        assert "NUDGES_FOR_VERIFICATION" in LAYER_EDGE_TYPES["L3"]

    def test_nudges_for_verification_not_in_other_layers(self):
        for layer in ("L0", "L1", "L2", "L4"):
            assert "NUDGES_FOR_VERIFICATION" not in LAYER_EDGE_TYPES.get(layer, frozenset())


class TestDetectVerifiableClaims:
    def test_detects_past_expected_by(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
            metadata={"expected_by": "2020-01-01"},
        )
        results = detect_verifiable_claims(test_db)
        assert len(results) == 1
        assert results[0]["edge_type"] == "PREDICTS"
        assert results[0]["expected_by"] == "2020-01-01"
        assert results[0]["days_overdue"] > 0

    def test_detects_past_window_end(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="agent1",
            confidence=0.9,
            metadata={"window_end": "2021-06-15"},
        )
        results = detect_verifiable_claims(test_db)
        assert len(results) == 1
        assert results[0]["expected_by"] == "2021-06-15"

    def test_skips_future_expected_by(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
            metadata={"expected_by": "2099-12-31"},
        )
        results = detect_verifiable_claims(test_db)
        assert len(results) == 0

    def test_skips_no_metadata(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        results = detect_verifiable_claims(test_db)
        assert len(results) == 0

    def test_skips_below_confidence_threshold(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.5,
            metadata={"expected_by": "2020-01-01"},
        )
        results = detect_verifiable_claims(test_db, confidence_threshold=0.85)
        assert len(results) == 0

    def test_skips_edges_with_outcome(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
            metadata={"expected_by": "2020-01-01"},
        )
        query_record_outcome(
            test_db,
            source_agent="agent1",
            claim_node=n1["id"],
            outcome=True,
            recorded_by="verifier",
        )
        results = detect_verifiable_claims(test_db)
        assert len(results) == 0

    def test_filters_by_agent(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
            metadata={"expected_by": "2020-01-01"},
        )
        results = detect_verifiable_claims(test_db, agent="agent2")
        assert len(results) == 0
        results = detect_verifiable_claims(test_db, agent="agent1")
        assert len(results) == 1

    def test_detects_all_verifiable_types(self, test_db):
        for etype in ["CAUSES", "PREDICTS", "EXPECTS", "EXPECTS_FROM"]:
            layer = "L4" if etype in ("EXPECTS", "EXPECTS_FROM") else "L3"
            n1 = create_node(test_db, label=f"Claim_{etype}", created_by="agent1")
            n2 = create_node(test_db, label=f"Effect_{etype}", created_by="agent1")
            create_edge(
                test_db,
                from_node=n1["id"],
                to_node=n2["id"],
                layer=layer,
                edge_type=etype,
                created_by="agent1",
                confidence=0.9,
                metadata={"expected_by": "2020-01-01"},
            )
        results = detect_verifiable_claims(test_db)
        assert len(results) == 4

    def test_respects_limit(self, test_db):
        for i in range(5):
            n1 = create_node(test_db, label=f"Claim_{i}", created_by="agent1")
            n2 = create_node(test_db, label=f"Effect_{i}", created_by="agent1")
            create_edge(
                test_db,
                from_node=n1["id"],
                to_node=n2["id"],
                layer="L3",
                edge_type="PREDICTS",
                created_by="agent1",
                confidence=0.9,
                metadata={"expected_by": "2020-01-01"},
            )
        results = detect_verifiable_claims(test_db, limit=2)
        assert len(results) == 2


class TestCreateVerificationNudge:
    def test_creates_nudge_task_and_edge(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        result = create_verification_nudge(
            test_db, edge_id=e["id"], created_by="system", reason="Past due"
        )
        assert result["nudge_node"]["type"] == "task"
        assert result["nudge_edge"]["edge_type"] == "NUDGES_FOR_VERIFICATION"
        assert result["nudge_edge"]["layer"] == "L3"
        assert result["nudge_edge"]["to_node"] == n1["id"]

    def test_idempotent(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        r1 = create_verification_nudge(test_db, edge_id=e["id"], created_by="system")
        r2 = create_verification_nudge(test_db, edge_id=e["id"], created_by="system")
        assert r1["nudge_edge"]["id"] == r2["nudge_edge"]["id"]

    def test_raises_on_missing_edge(self, test_db):
        from ohm.exceptions import EdgeNotFoundError

        with pytest.raises(EdgeNotFoundError):
            create_verification_nudge(test_db, edge_id="nonexistent", created_by="system")


class TestRecordVerificationOutcome:
    def test_true_outcome(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        result = record_verification_outcome(
            test_db, edge_id=e["id"], outcome="true", recorded_by="verifier"
        )
        assert result["outcome"] == "true"
        assert result["confidence"] == 1.0
        assert "outcome_record" in result

    def test_false_outcome(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        result = record_verification_outcome(
            test_db, edge_id=e["id"], outcome="false", recorded_by="verifier"
        )
        assert result["outcome"] == "false"
        assert result["confidence"] == 0.0

    def test_ambiguous_outcome(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        result = record_verification_outcome(
            test_db, edge_id=e["id"], outcome="ambiguous", recorded_by="verifier"
        )
        assert result["outcome"] == "ambiguous"
        assert result["confidence"] == 0.5

    def test_deferred_outcome(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        create_verification_nudge(test_db, edge_id=e["id"], created_by="system")
        result = record_verification_outcome(
            test_db, edge_id=e["id"], outcome="deferred", recorded_by="verifier"
        )
        assert result["deferred"] is True
        assert len(result["nudges_resolved"]) == 1

    def test_resolves_nudges(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        create_verification_nudge(test_db, edge_id=e["id"], created_by="system")
        pending_before = list_pending_verifications(test_db)
        assert len(pending_before) == 1
        record_verification_outcome(
            test_db, edge_id=e["id"], outcome="true", recorded_by="verifier"
        )
        pending_after = list_pending_verifications(test_db)
        assert len(pending_after) == 0

    def test_invalid_outcome_rejected(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        from ohm.exceptions import ValidationError

        with pytest.raises(ValidationError):
            record_verification_outcome(
                test_db, edge_id=e["id"], outcome="maybe", recorded_by="verifier"
            )

    def test_raises_on_missing_edge(self, test_db):
        from ohm.exceptions import EdgeNotFoundError

        with pytest.raises(EdgeNotFoundError):
            record_verification_outcome(
                test_db, edge_id="nonexistent", outcome="true", recorded_by="verifier"
            )


class TestListPendingVerifications:
    def test_lists_pending_nudges(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        create_verification_nudge(test_db, edge_id=e["id"], created_by="system")
        results = list_pending_verifications(test_db)
        assert len(results) == 1
        assert results[0]["edge_type"] == "NUDGES_FOR_VERIFICATION"

    def test_excludes_resolved_nudges(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        create_verification_nudge(test_db, edge_id=e["id"], created_by="system")
        record_verification_outcome(
            test_db, edge_id=e["id"], outcome="true", recorded_by="verifier"
        )
        results = list_pending_verifications(test_db)
        assert len(results) == 0

    def test_filters_by_agent(self, test_db):
        n1 = create_node(test_db, label="Claim", created_by="agent1")
        n2 = create_node(test_db, label="Effect", created_by="agent1")
        e = create_edge(
            test_db,
            from_node=n1["id"],
            to_node=n2["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="agent1",
            confidence=0.9,
        )
        create_verification_nudge(test_db, edge_id=e["id"], created_by="agent1")
        results = list_pending_verifications(test_db, agent="agent2")
        assert len(results) == 0
        results = list_pending_verifications(test_db, agent="agent1")
        assert len(results) == 1


class TestSDKMethods:
    def test_sdk_detect_verifiable_claims(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test")
        n1 = g.create_node("Claim")
        n2 = g.create_node("Effect")
        g.create_edge(
            from_node=n1["id"],
            to_node=n2["id"],
            edge_type="PREDICTS",
            layer="L3",
            confidence=0.9,
            metadata={"expected_by": "2020-01-01"},
        )
        claims = g.detect_verifiable_claims()
        assert len(claims) == 1

    def test_sdk_create_verification_nudge(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test")
        n1 = g.create_node("Claim")
        n2 = g.create_node("Effect")
        e = g.create_edge(
            from_node=n1["id"],
            to_node=n2["id"],
            edge_type="PREDICTS",
            layer="L3",
            confidence=0.9,
        )
        nudge = g.create_verification_nudge(edge_id=e["id"], reason="Past due")
        assert nudge["nudge_edge"]["edge_type"] == "NUDGES_FOR_VERIFICATION"

    def test_sdk_record_verification_outcome(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test")
        n1 = g.create_node("Claim")
        n2 = g.create_node("Effect")
        e = g.create_edge(
            from_node=n1["id"],
            to_node=n2["id"],
            edge_type="PREDICTS",
            layer="L3",
            confidence=0.9,
        )
        result = g.record_verification_outcome(edge_id=e["id"], outcome="true")
        assert result["outcome"] == "true"
        assert result["confidence"] == 1.0

    def test_sdk_list_pending_verifications(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test")
        n1 = g.create_node("Claim")
        n2 = g.create_node("Effect")
        e = g.create_edge(
            from_node=n1["id"],
            to_node=n2["id"],
            edge_type="PREDICTS",
            layer="L3",
            confidence=0.9,
        )
        g.create_verification_nudge(edge_id=e["id"])
        pending = g.list_pending_verifications()
        assert len(pending) == 1


class TestEndToEndWorkflow:
    def test_full_detect_nudge_verify_workflow(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="agent1")
        n1 = g.create_node("Claim X")
        n2 = g.create_node("Effect Y")
        g.create_edge(
            from_node=n1["id"],
            to_node=n2["id"],
            edge_type="PREDICTS",
            layer="L3",
            confidence=0.9,
            metadata={"expected_by": "2020-01-01"},
        )
        claims = g.detect_verifiable_claims()
        assert len(claims) == 1
        edge_id = claims[0]["id"]
        nudge = g.create_verification_nudge(edge_id=edge_id, reason="Past due")
        assert nudge["nudge_edge"]["edge_type"] == "NUDGES_FOR_VERIFICATION"
        pending = g.list_pending_verifications()
        assert len(pending) == 1
        result = g.record_verification_outcome(
            edge_id=edge_id, outcome="false", reason="Disproved"
        )
        assert result["outcome"] == "false"
        assert result["confidence"] == 0.0
        assert len(result["nudges_resolved"]) == 1
        pending_after = g.list_pending_verifications()
        assert len(pending_after) == 0
        claims_after = g.detect_verifiable_claims()
        assert len(claims_after) == 0

    def test_deferred_workflow(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="agent1")
        n1 = g.create_node("Claim Z")
        n2 = g.create_node("Effect W")
        g.create_edge(
            from_node=n1["id"],
            to_node=n2["id"],
            edge_type="EXPECTS",
            layer="L4",
            confidence=0.9,
            metadata={"expected_by": "2020-01-01"},
        )
        claims = g.detect_verifiable_claims()
        assert len(claims) == 1
        edge_id = claims[0]["id"]
        g.create_verification_nudge(edge_id=edge_id)
        result = g.record_verification_outcome(
            edge_id=edge_id, outcome="deferred", reason="Need more data"
        )
        assert result["deferred"] is True
        pending = g.list_pending_verifications()
        assert len(pending) == 0
