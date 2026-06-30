"""Tests for OHM-jdfq — proactive epistemic nudges at write time.

Three new nudge types added to generate_nudges():
- high_confidence_weak_source: confidence >= 0.8 + source_tier in {raw, unverified}
- causal_edge_missing_mechanism: CAUSES edge with no condition or metadata.mechanism
- fast_decaying_observation: observation with half_life_days + existing stale obs

Nudge log persistence: enrich_response() writes each nudge to ohm_nudge_log
for quality analytics.

Existing nudges (causal_edge_suggestion, source_citation, pert_estimation,
challenge_reminder, etc.) must still fire — backward compat.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from ohm.schema import initialize_schema
from ohm.server.nudges import generate_nudges, enrich_response, CAUSAL_EDGE_TYPES


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    initialize_schema(c)
    yield c
    c.close()


class TestHighConfidenceWeakSourceNudge:
    """Nudge: high confidence + weak source_tier → warning."""

    def test_fires_on_high_conf_raw_tier(self):
        nudges = generate_nudges(
            action="edge", confidence=0.9, source_tier="raw",
            edge_type="CAUSES",
        )
        types = [n["type"] for n in nudges]
        assert "high_confidence_weak_source" in types
        n = next(n for n in nudges if n["type"] == "high_confidence_weak_source")
        assert n["severity"] == "warning"
        assert n["data"]["ceiling"] == 0.3

    def test_fires_on_high_conf_unverified_tier(self):
        nudges = generate_nudges(
            action="node", confidence=0.85, source_tier="unverified",
        )
        types = [n["type"] for n in nudges]
        assert "high_confidence_weak_source" in types

    def test_does_not_fire_on_official_tier(self):
        nudges = generate_nudges(
            action="edge", confidence=0.9, source_tier="official",
            edge_type="SUPPORTS",
        )
        types = [n["type"] for n in nudges]
        assert "high_confidence_weak_source" not in types

    def test_does_not_fire_on_low_confidence(self):
        nudges = generate_nudges(
            action="edge", confidence=0.5, source_tier="raw",
            edge_type="SUPPORTS",
        )
        types = [n["type"] for n in nudges]
        assert "high_confidence_weak_source" not in types

    def test_does_not_fire_without_source_tier(self):
        nudges = generate_nudges(
            action="edge", confidence=0.9, edge_type="SUPPORTS",
        )
        types = [n["type"] for n in nudges]
        assert "high_confidence_weak_source" not in types


class TestCausalEdgeMissingMechanismNudge:
    """Nudge: CAUSES edge without condition or metadata.mechanism → suggestion."""

    def test_fires_on_causes_without_condition(self):
        nudges = generate_nudges(
            action="edge", edge_type="CAUSES",
        )
        types = [n["type"] for n in nudges]
        assert "causal_edge_missing_mechanism" in types

    def test_fires_on_influences_without_mechanism(self):
        nudges = generate_nudges(
            action="edge", edge_type="INFLUENCES",
        )
        types = [n["type"] for n in nudges]
        assert "causal_edge_missing_mechanism" in types

    def test_does_not_fire_when_condition_set(self):
        nudges = generate_nudges(
            action="edge", edge_type="CAUSES",
            condition="mediated by temperature increase",
        )
        types = [n["type"] for n in nudges]
        assert "causal_edge_missing_mechanism" not in types

    def test_does_not_fire_when_metadata_mechanism_set(self):
        nudges = generate_nudges(
            action="edge", edge_type="CAUSES",
            metadata={"mechanism": "catalytic reaction at 350C"},
        )
        types = [n["type"] for n in nudges]
        assert "causal_edge_missing_mechanism" not in types

    def test_does_not_fire_on_non_causal_edge(self):
        nudges = generate_nudges(
            action="edge", edge_type="SUPPORTS",
        )
        types = [n["type"] for n in nudges]
        assert "causal_edge_missing_mechanism" not in types

    def test_fires_for_all_causal_types(self):
        for et in CAUSAL_EDGE_TYPES:
            nudges = generate_nudges(action="edge", edge_type=et)
            types = [n["type"] for n in nudges]
            assert "causal_edge_missing_mechanism" in types, f"Missing for {et}"


class TestFastDecayingObservationNudge:
    """Nudge: observation with half_life_days + stale existing obs → hint."""

    def test_fires_when_existing_obs_decayed(self, conn):
        from ohm.queries import create_node, create_observation

        node = create_node(conn, label="Sensor", node_type="concept", created_by="t")
        # Create old observations that have decayed
        old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="t", value=0.8, source="t",
        )
        # Backdate it
        conn.execute(
            "UPDATE ohm_observations SET created_at = ? WHERE node_id = ?",
            [old_ts, node["id"]],
        )
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="t", value=0.7, source="t",
        )
        conn.execute(
            "UPDATE ohm_observations SET created_at = ? WHERE node_id = ? AND created_at != ?",
            [old_ts, node["id"], old_ts],
        )

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation",
            node_id=node["id"],
            store=store,
            half_life_days=30.0,
        )
        types = [n["type"] for n in nudges]
        assert "fast_decaying_observation" in types

    def test_does_not_fire_without_half_life(self, conn):
        from ohm.queries import create_node

        node = create_node(conn, label="Sensor2", node_type="concept", created_by="t")

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation",
            node_id=node["id"],
            store=store,
        )
        types = [n["type"] for n in nudges]
        assert "fast_decaying_observation" not in types

    def test_does_not_fire_with_fresh_obs(self, conn):
        from ohm.queries import create_node, create_observation

        node = create_node(conn, label="FreshSensor", node_type="concept", created_by="t")
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="t", value=0.9, source="t",
        )

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation",
            node_id=node["id"],
            store=store,
            half_life_days=30.0,
        )
        types = [n["type"] for n in nudges]
        assert "fast_decaying_observation" not in types


class TestNudgeLogPersistence:
    """enrich_response persists nudges to ohm_nudge_log."""

    def test_nudges_logged_to_table(self, conn):
        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = [
            {"type": "test_nudge", "severity": "info", "message": "test msg", "data": {"k": "v"}},
            {"type": "test_nudge_2", "severity": "warning", "message": "another"},
        ]
        response = {"id": "node_1", "label": "test"}
        enrich_response(response, nudges, store=store, agent="metis", action="node", target_id="node_1")

        rows = conn.execute(
            "SELECT agent, action, nudge_type, severity, target_id, message FROM ohm_nudge_log ORDER BY nudge_type"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "metis"
        assert rows[0][1] == "node"
        assert rows[0][2] == "test_nudge"
        assert rows[0][3] == "info"
        assert rows[0][4] == "node_1"

    def test_no_nudges_no_log(self, conn):
        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        response = {"id": "node_2"}
        enrich_response(response, [], store=store, agent="metis", action="node")
        count = conn.execute("SELECT COUNT(*) FROM ohm_nudge_log").fetchone()[0]
        assert count == 0

    def test_log_failure_does_not_break_response(self, conn):
        class BrokenStore:
            pass

        store = BrokenStore()  # no .conn attribute → will fail
        response = {"id": "node_3"}
        nudges = [{"type": "x", "severity": "info", "message": "x"}]
        # Should not raise
        result = enrich_response(response, nudges, store=store, agent="metis", action="node")
        assert result["nudges"] == nudges


class TestBackwardCompat:
    """Existing nudges must still fire after the OHM-jdfq additions."""

    def test_causal_edge_suggestion_still_fires(self):
        nudges = generate_nudges(
            action="edge", edge_type="SUPPORTS",
        )
        types = [n["type"] for n in nudges]
        assert "causal_edge_suggestion" in types

    def test_causal_edge_confirmed_still_fires(self):
        nudges = generate_nudges(
            action="edge", edge_type="CAUSES",
            condition="mechanism specified",
        )
        types = [n["type"] for n in nudges]
        assert "causal_edge_confirmed" in types

    def test_source_citation_still_fires(self):
        nudges = generate_nudges(
            action="observation",
            provenance="research",
        )
        types = [n["type"] for n in nudges]
        assert "source_citation" in types

    def test_pert_estimation_still_fires(self):
        nudges = generate_nudges(
            action="edge", edge_type="CAUSES",
            confidence=0.7,
            condition="mechanism",
        )
        types = [n["type"] for n in nudges]
        assert "pert_estimation" in types

    def test_enrich_response_without_store_still_works(self):
        """Old callers that don't pass store/agent should still get nudges in response."""
        response = {"id": "x"}
        nudges = [{"type": "test", "severity": "info", "message": "hi"}]
        result = enrich_response(response, nudges)
        assert result["nudges"] == nudges


class TestValueContradictionNudge:
    """OHM-ag92: value_contradiction nudge when new obs disagrees with prior obs.

    The previous 'contradiction_alert' just counted CHALLENGED_BY edges.
    This new nudge compares the new observation's numeric value against the
    most recent prior observations on the same node and fires when the
    difference exceeds the threshold.
    """

    def test_fires_when_new_value_far_from_prior(self, conn):
        from ohm.queries import create_node, create_observation

        node = create_node(conn, label="Sensor", node_type="concept", created_by="alice")
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="bob", value=0.8, source="bob",
        )

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation", node_id=node["id"], store=store, value=0.1,
        )
        types = [n["type"] for n in nudges]
        assert "value_contradiction" in types
        n = next(n for n in nudges if n["type"] == "value_contradiction")
        assert n["severity"] == "warning"
        assert n["data"]["new_value"] == pytest.approx(0.1, abs=1e-6)
        assert n["data"]["prior_value"] == pytest.approx(0.8, abs=1e-6)
        assert n["data"]["gap"] == pytest.approx(0.7, abs=0.01)
        assert n["data"]["prior_agent"] == "bob"

    def test_does_not_fire_when_values_within_threshold(self, conn):
        from ohm.queries import create_node, create_observation

        node = create_node(conn, label="Sensor", node_type="concept", created_by="alice")
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="bob", value=0.8, source="bob",
        )

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation", node_id=node["id"], store=store, value=0.85,
            value_contradiction_threshold=0.3,
        )
        types = [n["type"] for n in nudges]
        assert "value_contradiction" not in types

    def test_does_not_fire_when_no_prior_observations(self, conn):
        from ohm.queries import create_node

        node = create_node(conn, label="Fresh", node_type="concept", created_by="alice")

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation", node_id=node["id"], store=store, value=0.5,
        )
        types = [n["type"] for n in nudges]
        assert "value_contradiction" not in types

    def test_does_not_fire_when_value_is_none(self, conn):
        from ohm.queries import create_node

        node = create_node(conn, label="Sensor", node_type="concept", created_by="alice")

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation", node_id=node["id"], store=store, value=None,
        )
        types = [n["type"] for n in nudges]
        assert "value_contradiction" not in types

    def test_suppressed_when_recent_challenge_exists(self, conn):
        from ohm.queries import create_node, create_observation, create_edge

        node = create_node(conn, label="Disputed", node_type="concept", created_by="alice")
        prior = create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="bob", value=0.8, source="bob",
        )
        # Create a CHALLENGED_BY edge from the prior obs (as if it was already challenged)
        create_edge(
            conn, from_node=node["id"], to_node=prior["id"],
            layer="L3", edge_type="CHALLENGED_BY", created_by="alice", confidence=0.7,
        )

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation", node_id=node["id"], store=store, value=0.1,
        )
        types = [n["type"] for n in nudges]
        # Disagreement exists but a challenge has been recorded already
        # (still fires contradiction_alert because count > 0, but NOT
        # value_contradiction since it's been addressed)
        assert "value_contradiction" not in types

    def test_custom_threshold_respected(self, conn):
        from ohm.queries import create_node, create_observation

        node = create_node(conn, label="Sensor", node_type="concept", created_by="alice")
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="bob", value=0.8, source="bob",
        )

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        # Tight threshold — small disagreement fires
        nudges = generate_nudges(
            action="observation", node_id=node["id"], store=store, value=0.78,
            value_contradiction_threshold=0.01,
        )
        types = [n["type"] for n in nudges]
        assert "value_contradiction" in types

    def test_fires_only_once_per_write(self, conn):
        """If multiple prior obs disagree, only the most recent one triggers."""
        from ohm.queries import create_node, create_observation

        node = create_node(conn, label="Multi", node_type="concept", created_by="alice")
        # Create 2 prior obs with different values, both far from new value
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="bob", value=0.9, source="bob",
        )
        create_observation(
            conn, node_id=node["id"], obs_type="measurement",
            created_by="charlie", value=0.1, source="charlie",
        )

        class FakeStore:
            pass

        store = FakeStore()
        store.conn = conn

        nudges = generate_nudges(
            action="observation", node_id=node["id"], store=store, value=0.5,
        )
        vc_nudges = [n for n in nudges if n["type"] == "value_contradiction"]
        # The most recent prior is charlie (0.1), gap = 0.4 → fires.
        # bob (0.9) is older — we should NOT iterate further (the break
        # statement in generate_nudges limits to one).
        assert len(vc_nudges) == 1
        assert vc_nudges[0]["data"]["prior_value"] == pytest.approx(0.1, abs=1e-6)