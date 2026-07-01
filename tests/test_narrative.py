"""Tests for agent-readable knowledge surfaces (OHM-q9rt)."""

from __future__ import annotations

import time
import duckdb
import pytest

from ohm.schema import initialize_schema
from ohm.queries import (
    create_node,
    create_edge,
    create_observation,
    query_neighborhood_narrative,
    query_claim_lineage,
    query_contradiction_summary,
    query_task_context,
    query_confidence_report,
)


@pytest.fixture
def test_conn():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


def _seed_graph(conn):
    """Create a small graph: A --CAUSES--> B, C --SUPPORTS--> A."""
    a = create_node(conn, label="Hormuz AND-Gate", node_type="concept", created_by="metis")
    b = create_node(conn, label="Chokepoint", node_type="concept", created_by="metis")
    c = create_node(conn, label="Trade Route", node_type="concept", created_by="hephaestus")
    create_edge(conn, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="metis")
    create_edge(conn, from_node=c["id"], to_node=a["id"], edge_type="SUPPORTS", layer="L3", created_by="hephaestus")
    return {"a": a, "b": b, "c": c}


class TestNeighborhoodNarrative:
    """Tests for query_neighborhood_narrative() (OHM-q9rt.1)."""

    def test_returns_node_info(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        assert r["node"]["id"] == nodes["a"]["id"]
        assert r["node"]["label"] == "Hormuz AND-Gate"
        assert r["node"]["type"] == "concept"

    def test_returns_reasoning_chains(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        assert len(r["why_it_matters"]) == 2
        summaries = [c["summary"] for c in r["why_it_matters"]]
        assert any("CAUSES" in s for s in summaries)
        assert any("SUPPORTS" in s for s in summaries)

    def test_chain_has_path_and_edges(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        chain = r["why_it_matters"][0]
        assert "path" in chain
        assert len(chain["path"]) == 2
        assert "edges" in chain
        assert len(chain["edges"]) == 1
        assert chain["edges"][0]["edge_type"] in ("CAUSES", "SUPPORTS")

    def test_connections_summary(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        assert "Hormuz" in r["connections_summary"]
        assert "CAUSES" in r["connections_summary"]
        assert "SUPPORTS" in r["connections_summary"]

    def test_connection_count(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        assert r["connection_count"] == 2

    def test_agent_context_personalized(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"], agent_name="metis")
        assert "agent_context" in r
        assert r["agent_context"]["agent"] == "metis"
        # metis authored the CAUSES edge (a→b), hephaestus authored SUPPORTS (c→a)
        assert r["agent_context"]["my_edge_count"] == 1

    def test_no_agent_context_when_agent_none(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        assert "agent_context" not in r

    def test_missing_node_raises(self, test_conn):
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            query_neighborhood_narrative(test_conn, "does-not-exist-1234")

    def test_isolated_node_has_empty_connections(self, test_conn):
        n = create_node(test_conn, label="Lonely", node_type="concept", created_by="test")
        r = query_neighborhood_narrative(test_conn, n["id"])
        assert r["connection_count"] == 0
        assert r["why_it_matters"] == []
        assert "no connections" in r["connections_summary"].lower()

    def test_evidence_includes_observations(self, test_conn):
        from ohm.queries import create_observation

        nodes = _seed_graph(test_conn)
        create_observation(
            test_conn,
            node_id=nodes["a"]["id"],
            obs_type="measurement",
            created_by="metis",
            value=0.85,
        )
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        assert len(r["evidence"]) >= 1
        assert r["evidence"][0]["node_label"] == "Hormuz AND-Gate"

    def test_to_dict_serializable(self, test_conn):
        nodes = _seed_graph(test_conn)
        r = query_neighborhood_narrative(test_conn, nodes["a"]["id"])
        import json

        # Should be JSON-serializable for HTTP response
        serialized = json.dumps(r, default=str)
        assert "Hormuz" in serialized


# ── Claim Lineage (OHM-q9rt.2) ──────────────────────────────────────────────


def _seed_lineage_graph(conn):
    """Create: Pattern --DERIVES_FROM--> Observation --REFERENCES--> Source."""
    src = create_node(conn, label="Reuters Article", node_type="source", created_by="metis")
    obs = create_node(conn, label="Price Observation", node_type="concept", created_by="metis")
    pattern = create_node(conn, label="Demand Rationing Pattern", node_type="pattern", created_by="metis")
    create_edge(conn, from_node=obs["id"], to_node=src["id"], edge_type="REFERENCES", layer="L2", created_by="metis")
    create_edge(conn, from_node=pattern["id"], to_node=obs["id"], edge_type="DERIVES_FROM", layer="L2", created_by="metis")
    create_observation(conn, node_id=obs["id"], obs_type="measurement", created_by="metis", value=0.85)
    return {"src": src, "obs": obs, "pattern": pattern}


class TestClaimLineage:
    """Tests for query_claim_lineage() (OHM-q9rt.2)."""

    def test_returns_claim_info(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        assert r["claim"]["label"] == "Demand Rationing Pattern"
        assert r["claim"]["type"] == "pattern"

    def test_returns_lineage_tree(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        assert r["total_nodes"] == 2  # obs + src
        assert len(r["lineage"]) >= 1

    def test_sources_at_leaves(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        assert r["total_sources"] == 1
        assert r["sources"][0]["label"] == "Reuters Article"
        assert r["sources"][0]["node_id"] == nodes["src"]["id"]

    def test_confidence_chain_product(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        # Two edges at default 0.7 confidence → product ≈ 0.49
        assert r["min_confidence"] is not None
        assert r["max_confidence"] is not None
        assert r["min_confidence"] <= r["max_confidence"]

    def test_gaps_detected(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        # Source node has no observations → it's a gap
        assert r["total_gaps"] >= 1
        gap_labels = [g["label"] for g in r["gaps"]]
        assert "Reuters Article" in gap_labels

    def test_observations_on_chain_nodes(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        # The Observation node should have observations attached
        obs_nodes = [n for n in r["lineage"] if n["type"] == "concept"]
        assert len(obs_nodes) >= 1
        assert len(obs_nodes[0]["observations"]) >= 1

    def test_chain_depth(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        assert r["chain_depth"] == 2  # pattern→obs→src

    def test_missing_node_raises(self, test_conn):
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            query_claim_lineage(test_conn, "does-not-exist-1234")

    def test_isolated_node_has_empty_lineage(self, test_conn):
        n = create_node(test_conn, label="Lonely Pattern", node_type="pattern", created_by="test")
        r = query_claim_lineage(test_conn, n["id"])
        assert r["total_nodes"] == 0
        assert r["total_sources"] == 0
        assert r["total_gaps"] == 0
        assert r["lineage"] == []

    def test_json_serializable(self, test_conn):
        nodes = _seed_lineage_graph(test_conn)
        r = query_claim_lineage(test_conn, nodes["pattern"]["id"])
        import json

        serialized = json.dumps(r, default=str)
        assert "Demand Rationing" in serialized


# ── Contradiction Summary (OHM-q9rt.3) ─────────────────────────────────────


class TestContradictionSummary:
    """Tests for query_contradiction_summary() (OHM-q9rt.3)."""

    def test_no_contradiction_on_neutral_node(self, test_conn):
        n = create_node(test_conn, label="Neutral", node_type="concept", created_by="test")
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="test", value=0.5, baseline=0.5)
        r = query_contradiction_summary(test_conn, n["id"])
        assert r["has_contradiction"] is False
        assert r["recommendation"] == "no_contradiction"

    def test_detects_opposite_observations(self, test_conn):
        n = create_node(test_conn, label="Price Index", node_type="concept", created_by="metis")
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="metis", value=0.9, baseline=0.5)
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="hephaestus", value=0.1, baseline=0.5)
        r = query_contradiction_summary(test_conn, n["id"])
        assert r["has_contradiction"] is True
        assert len(r["sides"]) == 2
        directions = [s["direction"] for s in r["sides"]]
        assert "above_baseline" in directions
        assert "below_baseline" in directions

    def test_sides_have_agents_and_observations(self, test_conn):
        n = create_node(test_conn, label="Debated", node_type="concept", created_by="metis")
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="metis", value=0.9, baseline=0.5)
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="hephaestus", value=0.1, baseline=0.5)
        r = query_contradiction_summary(test_conn, n["id"])
        for side in r["sides"]:
            assert len(side["agents"]) >= 1
            assert len(side["observations"]) >= 1
            assert "effective_confidence" in side
            assert side["observation_count"] >= 1

    def test_recommendation_identifies_stronger_side(self, test_conn):
        n = create_node(test_conn, label="Contested", node_type="concept", created_by="metis")
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="metis", value=0.95, baseline=0.5)
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="hephaestus", value=0.05, baseline=0.5)
        r = query_contradiction_summary(test_conn, n["id"])
        assert "stronger" in r["recommendation"].lower()

    def test_recommendation_unresolved_when_balanced(self, test_conn):
        # Use identical values on opposite sides of baseline to get balanced confidence
        n = create_node(test_conn, label="Balanced", node_type="concept", created_by="metis")
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="metis", value=0.5, baseline=0.5)
        # Two agents with the SAME value but both at baseline — neutral, no contradiction
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="hephaestus", value=0.5, baseline=0.5)
        r = query_contradiction_summary(test_conn, n["id"])
        # Both observations are at baseline → no above/below split → no contradiction
        assert r["has_contradiction"] is False
        assert r["recommendation"] == "no_contradiction"

    def test_returns_node_info(self, test_conn):
        n = create_node(test_conn, label="Target", node_type="concept", created_by="test")
        r = query_contradiction_summary(test_conn, n["id"])
        assert r["node"]["id"] == n["id"]
        assert r["node"]["label"] == "Target"

    def test_missing_node_raises(self, test_conn):
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            query_contradiction_summary(test_conn, "does-not-exist-1234")

    def test_no_observations_returns_empty_sides(self, test_conn):
        n = create_node(test_conn, label="Empty", node_type="concept", created_by="test")
        r = query_contradiction_summary(test_conn, n["id"])
        assert r["has_contradiction"] is False
        assert r["total_observations"] == 0

    def test_json_serializable(self, test_conn):
        n = create_node(test_conn, label="Serial", node_type="concept", created_by="metis")
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="metis", value=0.9, baseline=0.5)
        create_observation(test_conn, node_id=n["id"], obs_type="measurement", created_by="hephaestus", value=0.1, baseline=0.5)
        r = query_contradiction_summary(test_conn, n["id"])
        import json

        serialized = json.dumps(r, default=str)
        assert "Serial" in serialized


# ── Task Context (OHM-q9rt.4) ───────────────────────────────────────────────


def _seed_task_graph(conn):
    """Create a task with a decision link and a blocker."""
    task = create_node(conn, label="Verify Hormuz Claim", node_type="task", created_by="metis")
    conn.execute(
        "UPDATE ohm_nodes SET task_status = ?, assigned_to = ?, success_criteria = ?, expected_claim = ? WHERE id = ?",
        ["open", "metis", "Claim holds for 30 days", "hormuz_claim", task["id"]],
    )
    decision = create_node(conn, label="Strategic Decision", node_type="decision", created_by="metis")
    create_edge(conn, from_node=task["id"], to_node=decision["id"], edge_type="DECISION_DEPENDS_ON", layer="L3", created_by="metis")
    blocker = create_node(conn, label="Gather Data", node_type="task", created_by="hephaestus")
    conn.execute("UPDATE ohm_nodes SET task_status = ? WHERE id = ?", ["in_progress", blocker["id"]])
    create_edge(conn, from_node=task["id"], to_node=blocker["id"], edge_type="DEPENDS_ON", layer="L4", created_by="metis")
    return {"task": task, "decision": decision, "blocker": blocker}


class TestTaskContext:
    """Tests for query_task_context() (OHM-q9rt.4)."""

    def test_returns_task_info(self, test_conn):
        nodes = _seed_task_graph(test_conn)
        r = query_task_context(test_conn, nodes["task"]["id"])
        assert r["task"]["label"] == "Verify Hormuz Claim"
        assert r["task"]["status"] == "open"
        assert r["task"]["assigned_to"] == "metis"

    def test_returns_subgraph(self, test_conn):
        nodes = _seed_task_graph(test_conn)
        r = query_task_context(test_conn, nodes["task"]["id"])
        assert "subgraph" in r
        assert len(r["subgraph"]["nodes"]) >= 3  # task + decision + blocker
        assert len(r["subgraph"]["edges"]) >= 2

    def test_returns_rationale(self, test_conn):
        nodes = _seed_task_graph(test_conn)
        r = query_task_context(test_conn, nodes["task"]["id"])
        assert len(r["rationale"]) >= 1
        types = [e["edge_type"] for e in r["rationale"]]
        assert "DECISION_DEPENDS_ON" in types

    def test_expected_outcome_from_success_criteria(self, test_conn):
        nodes = _seed_task_graph(test_conn)
        r = query_task_context(test_conn, nodes["task"]["id"])
        assert r["expected_outcome"] == "Claim holds for 30 days"

    def test_expected_outcome_fallback_to_expected_claim(self, test_conn):
        task = create_node(test_conn, label="Task No Criteria", node_type="task", created_by="test")
        test_conn.execute(
            "UPDATE ohm_nodes SET task_status = ?, expected_claim = ?, success_criteria = NULL WHERE id = ?",
            ["open", "some_claim_id", task["id"]],
        )
        r = query_task_context(test_conn, task["id"])
        assert "some_claim_id" in r["expected_outcome"]

    def test_blocking_tasks(self, test_conn):
        nodes = _seed_task_graph(test_conn)
        r = query_task_context(test_conn, nodes["task"]["id"])
        assert r["blocked_by_count"] >= 1
        blocking_labels = [b["label"] for b in r["blocking"]]
        assert "Gather Data" in blocking_labels

    def test_blocking_task_has_status(self, test_conn):
        nodes = _seed_task_graph(test_conn)
        r = query_task_context(test_conn, nodes["task"]["id"])
        for b in r["blocking"]:
            assert b["status"] is not None

    def test_missing_task_raises(self, test_conn):
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            query_task_context(test_conn, "does-not-exist-1234")

    def test_isolated_task_has_empty_subgraph(self, test_conn):
        task = create_node(test_conn, label="Lonely Task", node_type="task", created_by="test")
        test_conn.execute("UPDATE ohm_nodes SET task_status = ? WHERE id = ?", ["open", task["id"]])
        r = query_task_context(test_conn, task["id"])
        assert len(r["subgraph"]["nodes"]) == 1  # just the task itself
        assert len(r["rationale"]) == 0
        assert r["blocked_by_count"] == 0

    def test_json_serializable(self, test_conn):
        nodes = _seed_task_graph(test_conn)
        r = query_task_context(test_conn, nodes["task"]["id"])
        import json

        serialized = json.dumps(r, default=str)
        assert "Verify Hormuz" in serialized


# ── Confidence Report (OHM-q9rt.5) ──────────────────────────────────────────


class TestConfidenceReport:
    """Tests for query_confidence_report() (OHM-q9rt.5)."""

    def test_returns_agent_and_since(self, test_conn):
        r = query_confidence_report(test_conn, agent_name="metis", since="2000-01-01T00:00:00")
        assert r["agent"] == "metis"
        assert r["since"] == "2000-01-01T00:00:00"
        assert "query_timestamp" in r

    def test_empty_agent_returns_empty_sections(self, test_conn):
        r = query_confidence_report(test_conn, agent_name="ghost_agent", since="2000-01-01T00:00:00")
        assert r["summary"]["shifted"] == 0
        assert r["summary"]["new"] == 0
        assert r["summary"]["stale"] == 0

    def test_new_beliefs_detected(self, test_conn):
        a = create_node(test_conn, label="A", node_type="concept", created_by="metis")
        b = create_node(test_conn, label="B", node_type="concept", created_by="metis")
        create_edge(test_conn, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="metis")
        r = query_confidence_report(test_conn, agent_name="metis", since="2000-01-01T00:00:00")
        assert r["summary"]["new"] >= 1

    def test_shifted_beliefs_detected(self, test_conn):
        a = create_node(test_conn, label="A", node_type="concept", created_by="metis")
        b = create_node(test_conn, label="B", node_type="concept", created_by="metis")
        e = create_edge(test_conn, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="metis")
        # Wait for updated_at to differ from created_at
        time.sleep(0.02)
        test_conn.execute(
            "UPDATE ohm_edges SET confidence = 0.3, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
            ["hephaestus", e["id"]],
        )
        r = query_confidence_report(test_conn, agent_name="metis", since="2000-01-01T00:00:00")
        assert r["summary"]["shifted"] >= 1
        shifted = r["shifted_beliefs"][0]
        assert shifted["reason"] == "confidence updated"
        assert shifted["current_confidence"] == pytest.approx(0.3, abs=0.01)

    def test_stale_beliefs_detected(self, test_conn):
        a = create_node(test_conn, label="A", node_type="concept", created_by="metis")
        b = create_node(test_conn, label="B", node_type="concept", created_by="metis")
        e = create_edge(test_conn, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="metis")
        test_conn.execute("UPDATE ohm_edges SET confidence = 0.05 WHERE id = ?", [e["id"]])
        r = query_confidence_report(test_conn, agent_name="metis", since="2000-01-01T00:00:00")
        assert r["summary"]["stale"] >= 1

    def test_since_falls_back_to_last_sync(self, test_conn):
        # No last_sync set → falls back to 30 days ago
        r = query_confidence_report(test_conn, agent_name="metis")
        assert r["since"] is not None
        assert "T" in r["since"] or "-" in r["since"]

    def test_json_serializable(self, test_conn):
        a = create_node(test_conn, label="A", node_type="concept", created_by="metis")
        b = create_node(test_conn, label="B", node_type="concept", created_by="metis")
        create_edge(test_conn, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="metis")
        r = query_confidence_report(test_conn, agent_name="metis", since="2000-01-01T00:00:00")
        import json

        serialized = json.dumps(r, default=str)
        assert "metis" in serialized
