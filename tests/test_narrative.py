"""Tests for agent-readable knowledge surfaces (OHM-q9rt)."""

from __future__ import annotations

import duckdb
import pytest

from ohm.schema import initialize_schema
from ohm.queries import create_node, create_edge, query_neighborhood_narrative


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
            test_conn, node_id=nodes["a"]["id"], obs_type="measurement",
            created_by="metis", value=0.85,
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