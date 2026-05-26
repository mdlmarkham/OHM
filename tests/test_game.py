"""Tests for OHM Game-Theoretic Analysis (OHM-od01.2)."""

from __future__ import annotations

import pytest

from ohm.game import compute_nash, extract_game, game_to_matrix


class MockNode:
    def __init__(self, id, type, utility_scale=None, utility_usd_per_day=None, utility_currency=None):
        self.id = id
        self.type = type
        self.utility_scale = utility_scale
        self.utility_usd_per_day = utility_usd_per_day
        self.utility_currency = utility_currency


class MockEdge:
    def __init__(self, from_node, to_node, edge_type, confidence=0.5, probability=0.5):
        self.from_node = from_node
        self.to_node = to_node
        self.edge_type = edge_type
        self.confidence = confidence
        self.probability = probability


class MockReader:
    def __init__(self, nodes, edges):
        self._nodes = {n.id: n for n in nodes}
        self._edges = edges

    def get_node(self, id):
        return self._nodes.get(id)

    def get_nodes(self, node_type=None):
        if node_type:
            return [n for n in self._nodes.values() if n.type == node_type]
        return list(self._nodes.values())

    def get_edges(self, edge_types=None, layers=None):
        if edge_types:
            return [e for e in self._edges if e.edge_type in edge_types]
        return self._edges


class TestComputeNash:
    def test_matching_pennies_has_mixed_equilibrium(self):
        payoff_matrices: list[list[list[float]]] = [
            [[1.0, -1.0], [-1.0, 1.0]],
            [[-1.0, 1.0], [1.0, -1.0]],
        ]
        result = compute_nash(payoff_matrices, ["player0", "player1"])
        assert len(result["equilibria"]) >= 1
        eq = result["equilibria"][0]
        assert eq["equilibrium_type"] in ("mixed_strategy_gradient", "pure_strategy")
        assert abs(eq["expected_payoffs"]["player0"] - 0.0) < 0.01

    def test_coordination_game_pure_equilibria(self):
        coordination: list[list[list[float]]] = [
            [[3.0, 0.0], [0.0, 1.0]],
            [[3.0, 0.0], [0.0, 1.0]],
        ]
        result = compute_nash(coordination, ["player0", "player1"])
        eq_types = {e["equilibrium_type"] for e in result["equilibria"]}
        assert "pure_strategy" in eq_types
        assert len(result["equilibria"]) == 2

    def test_n_players_returns_error(self):
        payoff_3d: list[list[list[float]]] = [
            [[1.0, 2.0], [3.0, 4.0]],
            [[2.0, 1.0], [4.0, 3.0]],
            [[1.0, 1.0]],
        ]
        result = compute_nash(payoff_3d, ["p0", "p1", "p2"])
        assert result["n_players"] == 3
        assert result["solution_method"] == "indeterminate"


class TestExtractGame:
    def test_extract_game_finds_decision_nodes(self):
        nodes = [
            MockNode("target", "concept", utility_scale=0.8),
            MockNode("dec1", "decision", utility_scale=0.9),
            MockNode("dec2", "decision", utility_scale=0.7),
        ]
        edges = [
            MockEdge("dec1", "target", "CAUSES", confidence=0.8, probability=0.7),
            MockEdge("dec2", "target", "BLOCKS", confidence=0.6, probability=0.5),
        ]
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "target")
        assert "error" not in result
        assert result["players"] == ["dec1", "dec2"]
        assert result["n_players"] == 2
        assert result["game_type"] == "normal_form"

    def test_extract_game_target_not_found(self):
        nodes = [MockNode("dec1", "decision", utility_scale=0.9)]
        edges = []
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "nonexistent")
        assert "error" in result

    def test_extract_game_no_decision_nodes(self):
        nodes = [MockNode("target", "concept", utility_scale=0.8)]
        edges = []
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "target")
        assert "error" in result

    def test_extract_game_with_explicit_players(self):
        nodes = [
            MockNode("target", "concept", utility_scale=0.8),
            MockNode("dec1", "decision", utility_scale=0.9),
            MockNode("dec2", "decision", utility_scale=0.7),
        ]
        edges = [
            MockEdge("dec1", "target", "CAUSES", confidence=0.8, probability=0.7),
        ]
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "target", players=["dec1"])
        assert result["players"] == ["dec1"]
        assert result["n_players"] == 1

    def test_extract_game_blocks_edge_adversarial(self):
        nodes = [
            MockNode("target", "concept", utility_scale=0.8),
            MockNode("player_a", "decision", utility_scale=0.9),
            MockNode("player_b", "decision", utility_scale=0.7),
        ]
        edges = [
            MockEdge("player_a", "player_b", "BLOCKS", confidence=0.9, probability=0.8),
            MockEdge("player_a", "target", "CAUSES", confidence=0.8, probability=0.7),
            MockEdge("player_b", "target", "CAUSES", confidence=0.6, probability=0.5),
        ]
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "target")
        assert "error" not in result
        assert len(result["payoff_matrices"]) == 2


class TestGameToMatrix:
    def test_game_to_matrix_basic(self):
        result = game_to_matrix(
            ["p1", "p2"],
            [["a0", "a1"], ["b0", "b1"]],
            [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]],
        )
        assert result["n_players"] == 2
        assert len(result["payoff_matrices"]) == 2


class TestUtilityUsdPerDay:
    def test_extract_game_with_usd_payoffs(self):
        nodes = [
            MockNode("target", "concept", utility_scale=0.8),
            MockNode("dec1", "decision", utility_scale=0.9, utility_usd_per_day=5_000_000, utility_currency="USD"),
            MockNode("dec2", "decision", utility_scale=0.7, utility_usd_per_day=500_000, utility_currency="USD"),
        ]
        edges = [
            MockEdge("dec1", "target", "CAUSES", confidence=0.8, probability=0.7),
            MockEdge("dec2", "target", "CAUSES", confidence=0.6, probability=0.5),
        ]
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "target")
        assert "error" not in result
        assert result["decision_utilities"]["dec1"]["utility_usd"] == 5_000_000
        assert result["decision_utilities"]["dec2"]["utility_usd"] == 500_000

    def test_extract_game_falls_back_to_utility_scale(self):
        nodes = [
            MockNode("target", "concept", utility_scale=0.8),
            MockNode("dec1", "decision", utility_scale=0.9),
            MockNode("dec2", "decision", utility_scale=0.7),
        ]
        edges = [
            MockEdge("dec1", "target", "CAUSES", confidence=0.8, probability=0.7),
            MockEdge("dec2", "target", "CAUSES", confidence=0.6, probability=0.5),
        ]
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "target")
        assert "error" not in result
        assert result["decision_utilities"]["dec1"]["utility_usd"] is None
        assert result["decision_utilities"]["dec2"]["utility_usd"] is None

    def test_usd_payoff_normalizes_to_millions(self):
        nodes = [
            MockNode("target", "concept", utility_scale=0.8),
            MockNode("dec1", "decision", utility_scale=0.9, utility_usd_per_day=5_000_000),
        ]
        edges = [
            MockEdge("dec1", "target", "CAUSES", confidence=0.8, probability=0.7),
        ]
        reader = MockReader(nodes, edges)
        result = extract_game(reader, "target")
        assert "error" not in result
        payoffs = result["payoff_matrices"][0][0]
        assert all(abs(p - 5.0) < 0.01 for p in payoffs)
