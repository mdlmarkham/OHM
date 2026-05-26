"""Tests for network topology methods — centrality, communities, bridges."""

from __future__ import annotations

import pytest

from tests.conftest import create_sample_edge, create_sample_node

try:
    import networkx as nx
    import networkx.algorithms.community as nx_community

    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not NETWORKX_AVAILABLE, reason="networkx not installed"
)


def test_centrality_empty_graph(test_db):
    """Test centrality on empty graph."""
    from ohm.methods import compute_centrality

    result = compute_centrality(test_db)
    assert result["method"] == "compute_centrality"
    assert result["nodes"] == []
    assert result["n_nodes"] == 0


def test_centrality_simple_chain(test_db):
    """Test centrality on a simple chain A->B->C."""
    from ohm.methods import compute_centrality

    node_a = create_sample_node(test_db, label="a")
    node_b = create_sample_node(test_db, label="b")
    node_c = create_sample_node(test_db, label="c")

    create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="CAUSES", probability=0.8)

    result = compute_centrality(test_db, edge_types=["CAUSES"])

    assert result["method"] == "compute_centrality"
    assert result["n_nodes"] == 3
    assert len(result["nodes"]) == 3
    assert all("centrality" in n for n in result["nodes"])
    assert all(n["centrality"] > 0 for n in result["nodes"])


def test_centrality_with_cycle(test_db):
    """Test centrality on graph with cycle (A->B, B->C, C->A)."""
    from ohm.methods import compute_centrality

    node_a = create_sample_node(test_db, label="a")
    node_b = create_sample_node(test_db, label="b")
    node_c = create_sample_node(test_db, label="c")

    create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_c, to_node=node_a, edge_type="CAUSES", probability=0.8)

    result = compute_centrality(test_db, edge_types=["CAUSES"])

    assert result["method"] == "compute_centrality"
    assert result["n_nodes"] == 3
    centralities = [n["centrality"] for n in result["nodes"]]
    assert len(set(centralities)) == 1


def test_communities_empty_graph(test_db):
    """Test community detection on empty graph."""
    from ohm.methods import compute_communities

    result = compute_communities(test_db)
    assert result["method"] == "compute_communities"
    assert result["communities"] == []
    assert result["n_nodes"] == 0


def test_communities_two_clusters(test_db):
    """Test community detection finds two separate clusters."""
    from ohm.methods import compute_communities

    node_a = create_sample_node(test_db, label="a")
    node_b = create_sample_node(test_db, label="b")
    node_c = create_sample_node(test_db, label="c")
    node_x = create_sample_node(test_db, label="x")
    node_y = create_sample_node(test_db, label="y")

    create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_x, to_node=node_y, edge_type="CAUSES", probability=0.8)

    result = compute_communities(test_db, edge_types=["CAUSES"])

    assert result["method"] == "compute_communities"
    assert result["n_nodes"] == 5
    assert result["n_communities"] >= 1


def test_find_bridges_empty_graph(test_db):
    """Test bridge detection on empty graph."""
    from ohm.methods import find_bridges

    result = find_bridges(test_db)
    assert result["method"] == "find_bridges"
    assert result["bridges"] == []
    assert result["articulation_points"] == []
    assert result["n_nodes"] == 0


def test_find_bridges_chain(test_db):
    """Test bridge detection on a chain A->B->C has no bridges (undirected)."""
    from ohm.methods import find_bridges

    node_a = create_sample_node(test_db, label="a")
    node_b = create_sample_node(test_db, label="b")
    node_c = create_sample_node(test_db, label="c")

    create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="CAUSES", probability=0.8)

    result = find_bridges(test_db, edge_types=["CAUSES"])

    assert result["method"] == "find_bridges"
    assert result["n_nodes"] == 3


def test_find_bridges_with_bridge_edge(test_db):
    """Test bridge detection finds the bridge edge in A-B-C where B has other connections."""
    from ohm.methods import find_bridges

    node_a = create_sample_node(test_db, label="a")
    node_b = create_sample_node(test_db, label="b")
    node_c = create_sample_node(test_db, label="c")
    node_d = create_sample_node(test_db, label="d")

    create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="CAUSES", probability=0.8)
    create_sample_edge(test_db, from_node=node_b, to_node=node_d, edge_type="CAUSES", probability=0.8)

    result = find_bridges(test_db, edge_types=["CAUSES"])

    assert result["method"] == "find_bridges"
    assert result["n_nodes"] == 4
