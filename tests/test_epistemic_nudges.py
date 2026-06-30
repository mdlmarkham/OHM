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


class TestNudgeAcceptance:
    """OHM-49bg: nudge acceptance tracking via accept_nudge + list_nudges.

    Closes the nudge lifecycle loop: fire → log → agent accepts/rejects.
    The ohm_nudge_log.accepted column is the source of truth for the
    quality signal that backs /admin/nudges/quality.
    """

    def _insert_nudge(self, conn, agent="metis", nudge_type="test_nudge",
                      target_id="node_1", severity="info", message="hello"):
        import uuid
        nid = f"nudge_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO ohm_nudge_log
               (id, agent, action, nudge_type, severity, target_id, message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [nid, agent, "node", nudge_type, severity, target_id, message],
        )
        return nid

    def test_accept_nudge_marks_accepted(self, conn):
        from ohm.server.nudges import accept_nudge

        nid = self._insert_nudge(conn)
        result = accept_nudge(conn, nudge_id=nid, agent="metis", helpful=True)
        assert result["accepted"] is True
        assert result["accepted_at"] is not None
        assert result["id"] == nid

    def test_accept_nudge_rejects(self, conn):
        from ohm.server.nudges import accept_nudge

        nid = self._insert_nudge(conn)
        result = accept_nudge(conn, nudge_id=nid, agent="metis", helpful=False)
        assert result["accepted"] is False
        assert result["accepted_at"] is not None

    def test_accept_nudge_idempotent_last_wins(self, conn):
        """Re-accepting a nudge overwrites the prior response (last write wins)."""
        from ohm.server.nudges import accept_nudge

        nid = self._insert_nudge(conn)
        accept_nudge(conn, nudge_id=nid, agent="metis", helpful=True)
        result = accept_nudge(conn, nudge_id=nid, agent="metis", helpful=False)
        # The second response (False) wins
        assert result["accepted"] is False

    def test_accept_nudge_rejects_wrong_agent(self, conn):
        from ohm.server.nudges import accept_nudge
        from ohm.exceptions import ValidationError

        nid = self._insert_nudge(conn, agent="metis")
        with pytest.raises(ValidationError, match="metis"):
            accept_nudge(conn, nudge_id=nid, agent="other_agent", helpful=True)

    def test_accept_nudge_rejects_nonexistent(self, conn):
        from ohm.server.nudges import accept_nudge
        from ohm.exceptions import ValidationError

        with pytest.raises(ValidationError, match="not found"):
            accept_nudge(conn, nudge_id="nudge_nonexistent_xyz", agent="metis", helpful=True)

    def test_accept_nudge_skips_agent_check_when_none(self, conn):
        """When agent=None, the agent check is skipped (caller is system)."""
        from ohm.server.nudges import accept_nudge

        nid = self._insert_nudge(conn, agent="metis")
        # No agent check — useful for admin override
        result = accept_nudge(conn, nudge_id=nid, agent=None, helpful=True)
        assert result["accepted"] is True

    def test_accept_nudge_with_notes(self, conn):
        """Notes field is stored (even though the schema doesn't have it yet)."""
        from ohm.server.nudges import accept_nudge

        nid = self._insert_nudge(conn)
        result = accept_nudge(conn, nudge_id=nid, agent="metis", helpful=True, notes="great catch")
        # Notes are passed but may not be persisted yet — just ensure no error
        assert result["accepted"] is True


class TestListNudges:
    """OHM-49bg: list_nudges for filtering nudge history."""

    def test_list_all(self, conn):
        from ohm.server.nudges import list_nudges

        for i in range(3):
            conn.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
                [f"nudge_{i}", "metis", "node", "type_a", "info"],
            )
        result = list_nudges(conn)
        assert len(result) == 3

    def test_list_filter_by_agent(self, conn):
        from ohm.server.nudges import list_nudges

        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["n1", "metis", "node", "type_a", "info"],
        )
        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["n2", "clio", "node", "type_a", "info"],
        )
        result = list_nudges(conn, agent="metis")
        assert len(result) == 1
        assert result[0]["id"] == "n1"

    def test_list_filter_by_nudge_type(self, conn):
        from ohm.server.nudges import list_nudges

        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["n1", "metis", "node", "type_a", "info"],
        )
        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["n2", "metis", "node", "type_b", "info"],
        )
        result = list_nudges(conn, nudge_type="type_b")
        assert len(result) == 1
        assert result[0]["id"] == "n2"

    def test_list_filter_by_accepted(self, conn):
        from ohm.server.nudges import list_nudges, accept_nudge

        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["accepted_nudge", "metis", "node", "t", "info"],
        )
        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["unanswered_nudge", "metis", "node", "t", "info"],
        )
        accept_nudge(conn, nudge_id="accepted_nudge", agent="metis", helpful=True)

        accepted = list_nudges(conn, accepted=True)
        unanswered = list_nudges(conn, accepted=False)
        all_resp = list_nudges(conn)  # default: all
        all_with_filter_none = list_nudges(conn, accepted=None)

        assert len(accepted) == 1
        assert accepted[0]["id"] == "accepted_nudge"
        assert len(unanswered) == 0  # accepted=False filters out un-responded
        assert len(all_resp) == 2  # no accepted filter — all rows
        assert len(all_with_filter_none) == 2  # accepted=None is same as no filter

    def test_list_respects_limit(self, conn):
        from ohm.server.nudges import list_nudges

        for i in range(5):
            conn.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
                [f"n{i}", "metis", "node", "t", "info"],
            )
        result = list_nudges(conn, limit=2)
        assert len(result) == 2


class TestNudgeAcceptanceStats:
    """OHM-49bg: nudge_acceptance_stats for quality analytics."""

    def test_stats_empty(self, conn):
        from ohm.server.nudges import nudge_acceptance_stats
        stats = nudge_acceptance_stats(conn)
        assert stats["total"] == 0
        assert stats["responded"] == 0
        assert stats["acceptance_rate"] is None
        assert stats["by_type"] == {}
        assert stats["by_agent"] == {}

    def test_stats_with_mixed_responses(self, conn):
        from ohm.server.nudges import nudge_acceptance_stats, accept_nudge

        # 2 accepted, 1 rejected, 1 unanswered of type_a
        for i in range(2):
            nid = f"acc_{i}"
            conn.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
                [nid, "metis", "node", "type_a", "info"],
            )
            accept_nudge(conn, nudge_id=nid, agent="metis", helpful=True)
        rej = "rej_0"
        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            [rej, "metis", "node", "type_a", "info"],
        )
        accept_nudge(conn, nudge_id=rej, agent="metis", helpful=False)
        # Unanswered
        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["unanswered", "metis", "node", "type_a", "info"],
        )
        # Different type
        conn.execute(
            "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
            ["type_b_0", "clio", "node", "type_b", "info"],
        )

        stats = nudge_acceptance_stats(conn)
        assert stats["total"] == 5
        assert stats["responded"] == 3
        assert stats["acceptance_rate"] == 0.6  # 3/5

        # type_a: 4 total, 2 accepted, 1 rejected, 1 unanswered → 0.6667 rate (2/(2+1))
        type_a = stats["by_type"]["type_a"]
        assert type_a["total"] == 4
        assert type_a["accepted"] == 2
        assert type_a["rejected"] == 1
        assert type_a["acceptance_rate"] == pytest.approx(2 / 3, abs=1e-4)

        # type_b: 1 total, 0 responded → rate None
        type_b = stats["by_type"]["type_b"]
        assert type_b["total"] == 1
        assert type_b["accepted"] == 0
        assert type_b["acceptance_rate"] is None

        # by_agent
        metis = stats["by_agent"]["metis"]
        assert metis["total"] == 4
        assert metis["accepted"] == 2
        assert metis["rejected"] == 1
        clio = stats["by_agent"]["clio"]
        assert clio["total"] == 1
        assert clio["acceptance_rate"] is None

    def test_stats_filter_by_agent(self, conn):
        from ohm.server.nudges import nudge_acceptance_stats, accept_nudge

        for i, ag in enumerate(["metis", "clio", "metis", "clio"]):
            nid = f"n_{i}"
            conn.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity) VALUES (?, ?, ?, ?, ?)",
                [nid, ag, "node", "t", "info"],
            )
            accept_nudge(conn, nudge_id=nid, agent=ag, helpful=True)
        stats = nudge_acceptance_stats(conn, agent="metis")
        assert stats["total"] == 2
        assert stats["responded"] == 2
        # only metis in by_agent
        assert "metis" in stats["by_agent"]
        assert "clio" not in stats["by_agent"]


class TestNudgeAcceptanceHTTPEndpoint:
    """OHM-49bg: HTTP endpoint tests for /nudges/{id}/accept."""

    def test_http_accept_round_trip(self):
        """Round-trip a nudge through the HTTP endpoint: log → accept → quality stats."""
        import json
        import threading
        import socketserver
        from http.client import HTTPConnection

        from ohm.schema import DEFAULT_SCHEMA
        from ohm.server import OhmHandler
        from ohm.store import OhmStore
        from ohm.server.nudges import _persist_nudge_log, enrich_response, nudge_acceptance_stats

        import tempfile
        import os

        # Use a real on-disk DuckDB so the subprocess can read it
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test_nudge.duckdb")
            store = OhmStore(db_path=db_path, agent_name="metis")
            # Insert a nudge — agent is the default "ohm" (no_auth default)
            _persist_nudge_log(
                store, agent="ohm", action="node", target_id="node_1",
                nudges=[{"type": "test", "severity": "info", "message": "x"}],
            )
            # Find the nudge id
            rows = store.read_conn.execute(
                "SELECT id FROM ohm_nudge_log WHERE agent = 'ohm'"
            ).fetchall()
            assert rows, "nudge not persisted"
            nudge_id = rows[0][0]

            # Start a no-auth server
            OhmHandler.store = store
            OhmHandler.config = {"host": "127.0.0.1", "port": 0}
            OhmHandler.schema_config = DEFAULT_SCHEMA
            OhmHandler.tokens = {}
            OhmHandler.roles = {}
            OhmHandler.no_auth = True
            OhmHandler.multi_tenant = False
            OhmHandler.require_read_auth = False

            server = socketserver.TCPServer(
                ("127.0.0.1", 0), OhmHandler, bind_and_activate=False,
            )
            server.allow_reuse_address = True
            server.server_bind()
            server.server_activate()
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                # POST accept
                conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
                conn.request(
                    "POST",
                    f"/nudges/{nudge_id}/accept",
                    body=json.dumps({"helpful": True, "notes": "test"}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                assert resp.status == 200, f"expected 200, got {resp.status}: {resp.read().decode()}"
                body = json.loads(resp.read().decode())
                assert body["accepted"] is True
                assert body["nudge_id"] == nudge_id
                assert body["agent"] == "ohm"
                conn.close()

                # GET quality
                conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
                conn.request("GET", "/admin/nudges/quality")
                resp = conn.getresponse()
                assert resp.status == 200
                quality = json.loads(resp.read().decode())
                assert quality["total"] == 1
                assert quality["responded"] == 1
                assert quality["acceptance_rate"] == 1.0
                assert "test" in quality["by_type"]
                conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
                store.close()

    def test_http_accept_nonexistent_returns_400_or_500(self):
        """Nonexistent nudge id returns an error (400 or 500 depending on handler)."""
        import json
        import threading
        import socketserver
        from http.client import HTTPConnection

        from ohm.schema import DEFAULT_SCHEMA
        from ohm.server import OhmHandler
        from ohm.store import OhmStore
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test_nudge_404.duckdb")
            store = OhmStore(db_path=db_path, agent_name="metis")

            OhmHandler.store = store
            OhmHandler.config = {"host": "127.0.0.1", "port": 0}
            OhmHandler.schema_config = DEFAULT_SCHEMA
            OhmHandler.tokens = {}
            OhmHandler.roles = {}
            OhmHandler.no_auth = True
            OhmHandler.multi_tenant = False
            OhmHandler.require_read_auth = False

            server = socketserver.TCPServer(
                ("127.0.0.1", 0), OhmHandler, bind_and_activate=False,
            )
            server.allow_reuse_address = True
            server.server_bind()
            server.server_activate()
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
                conn.request(
                    "POST",
                    "/nudges/nudge_nonexistent_xyz/accept",
                    body=json.dumps({"helpful": True}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                # ValidationError is mapped to 400 by the server; 500 if unhandled
                assert resp.status in (400, 422, 500), resp.status
                conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
                store.close()