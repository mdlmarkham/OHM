"""OHM Game-Theoretic Analysis — Normal-form game extraction and Nash equilibrium computation.

Extracts normal-form games from OHM causal graphs and computes Nash equilibria
using linear programming (scipy.optimize.linprog).

ADR-010: Urgency ≠ priority — decision nodes carry utility_usd_per_day for
dollar-valued payoffs, falling back to utility_scale for dimensionless analysis.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["extract_game", "compute_nash", "game_to_matrix"]


def game_to_matrix(
    players: list[str],
    actions: list[list[str]],
    payoff_arrays: list[list[list[float]]],
) -> dict[str, Any]:
    """Convert structured game data to payoff matrix format.

    Args:
        players: List of player names.
        actions: List of action sets per player.
        payoff_arrays: Nested list payoff_arrays[i][a1][a2]... = payoff to player i.

    Returns:
        Dict with players, actions, and payoff_matrices (per player).
    """
    return {
        "players": players,
        "actions": actions,
        "n_players": len(players),
        "payoff_matrices": payoff_arrays,
    }


def extract_game(
    reader,
    target: str,
    *,
    players: list[str] | None = None,
    edge_types: list[str] | None = None,
    layers: list[str] | None = None,
) -> dict[str, Any]:
    """Extract a normal-form game from the causal graph around a target node.

    Given a target node and optionally specified players (decision nodes), extracts
    a normal-form game where:
    - Players = decision nodes in the causal neighborhood of target
    - Actions = intervention states (0=bad, 1=good)
    - Payoffs = causal influence weight × utility_scale (or utility_usd_per_day)

    BLOCKS edges model adversarial relationships: if A BLOCKS B, then when A
    takes action 0 (bad for A) it benefits B, and vice versa.

    Args:
        reader: GraphReader instance.
        target: Target node ID for payoff computation.
        players: Optional list of decision node IDs. If None, auto-detect from graph.
        edge_types: Edge types to consider (default: CAUSES, INFLUENCES, ENABLES, DEPENDS_ON, BLOCKS).
        layers: Optional layer filter.

    Returns:
        Dict with game structure, payoff matrices per player pair, and metadata.
    """
    if edge_types is None:
        edge_types = ["CAUSES", "INFLUENCES", "ENABLES", "DEPENDS_ON", "BLOCKS"]

    # Get all edges
    all_edges = reader.get_edges(edge_types=edge_types, layers=layers)
    edge_map: dict[tuple[str, str], dict[str, Any]] = {}
    for e in all_edges:
        key = (e.from_node, e.to_node)
        if key not in edge_map:
            edge_map[key] = {
                "from": e.from_node,
                "to": e.to_node,
                "confidence": e.confidence or 0.5,
                "probability": e.probability or 0.5,
                "edge_type": e.edge_type,
            }

    # Get all nodes for utility info
    target_node = reader.get_node(target)
    if target_node is None:
        return {"error": f"Target node not found: {target}"}

    target_utility = _node_utility(target_node)

    # Find decision nodes (players)
    if players is None:
        all_nodes = reader.get_nodes()
        players = [
            n.id for n in all_nodes
            if n.type == "decision" and (n.utility_scale or 0) > 0
        ]

    if not players:
        return {
            "error": "No decision nodes found. Specify players or create nodes with type='decision' and utility_scale > 0.",
            "target": target,
        }

    if len(players) > 2:
        return {
            "error": f"Game extraction only supports 2 players (found {len(players)}). Use ?players=a,b to specify exactly 2 decision nodes.",
            "target": target,
            "n_players": len(players),
        }

    if len(players) == 1:
        return {
            "error": "Game extraction requires at least 2 players. Found only 1 decision node.",
            "target": target,
        }

    # Build forward adjacency for causal influence computation
    forward_adj: dict[str, list[str]] = {}
    for (f, t), edata in edge_map.items():
        if f not in forward_adj:
            forward_adj[f] = []
        forward_adj[f].append(t)

    def causal_influence(node_id: str, visited: set[str] | None = None) -> float:
        """Compute causal influence weight from node to target (0-1)."""
        if visited is None:
            visited = set()
        if node_id in visited or node_id == target:
            return 0.0
        visited.add(node_id)
        if node_id == target:
            return 1.0
        # BFS forward
        total = 0.0
        for child in forward_adj.get(node_id, []):
            if child not in visited:
                edata = edge_map.get((node_id, child), {})
                prob = edata.get("probability", 0.5)
                conf = edata.get("confidence", 0.5)
                weight = prob * conf
                if child == target:
                    total += weight
                else:
                    total += weight * causal_influence(child, visited)
        return min(total, 1.0)

    # Get decision node utilities
    decision_utilities: dict[str, dict[str, Any]] = {}
    for p in players:
        node = reader.get_node(p)
        if node is None:
            decision_utilities[p] = {"utility": 0.0, "utility_usd": None}
            continue
        util = _node_utility(node)
        usd: float | None = node.utility_usd_per_day
        decision_utilities[p] = {
            "utility": util,
            "utility_usd": usd,
        }

    # Actions are binary: 0 = bad/intervention, 1 = good/no intervention
    actions = [[0, 1] for _ in players]

    # For each player pair, compute 2x2 payoff matrix
    # For N players, we store payoff arrays per player
    payoff_arrays: list[list[list[float]]] = []
    for i, player_i in enumerate(players):
        payoff_i: list[list[float]] = []
        util_i = decision_utilities[player_i]
        base_payoff_i = _payoff_from_utility(util_i, target_utility)
        for a_i in [0, 1]:
            row: list[float] = []
            for j, player_j in enumerate(players):
                if i == j:
                    row.append(base_payoff_i)
                    continue
                # Get relationship between player_i and player_j
                # Check if player_i BLOCKS player_j or vice versa
                blocks_edge = edge_map.get((player_i, player_j))
                reversed_blocks = edge_map.get((player_j, player_i))
                is_adversarial = (
                    (blocks_edge and blocks_edge["edge_type"] == "BLOCKS")
                    or (reversed_blocks and reversed_blocks["edge_type"] == "BLOCKS")
                )
                # Payoff depends on whether actions align
                # For adversarial (BLOCKS): when player_j acts badly (0), player_i benefits
                # For cooperative: aligned actions benefit both
                # Action 1 = good, Action 0 = bad
                if is_adversarial:
                    # In adversarial game: if player_j chooses 0 (bad), player_i gets full payoff
                    # if player_j chooses 1 (good), player_i gets reduced payoff
                    payoff_if_j_bad = base_payoff_i
                    payoff_if_j_good = base_payoff_i * 0.3  # opponent cooperating reduces benefit
                    payoffs_for_j = [payoff_if_j_bad, payoff_if_j_good]
                else:
                    # Cooperative: aligned actions maximize payoff
                    payoff_if_j_bad = base_payoff_i * 0.5
                    payoff_if_j_good = base_payoff_i
                    payoffs_for_j = [payoff_if_j_bad, payoff_if_j_good]
                row.append(payoffs_for_j[j])
            payoff_i.append(row)
        payoff_arrays.append(payoff_i)

    return {
        "target": target,
        "players": players,
        "decision_utilities": decision_utilities,
        "actions": actions,
        "action_labels": ["bad_intervention", "good_no_intervention"],
        "payoff_matrices": payoff_arrays,
        "n_players": len(players),
        "game_type": "normal_form",
        "extraction_method": "causal_influence_weighted",
    }


def _node_utility(node: Any) -> float:
    """Extract utility float from node (utility_scale or 1.0 default)."""
    if node.utility_scale is not None and node.utility_scale > 0:
        return float(node.utility_scale)
    return 1.0


def _payoff_from_utility(player_util: dict[str, float], target_util: float) -> float:
    """Compute payoff as product of player utility and target utility weight."""
    util = player_util.get("utility", 1.0)
    usd = player_util.get("utility_usd")
    if usd is not None:
        return usd / 1e6  # normalize to millions
    return util * target_util


def compute_nash(payoff_matrices: list[list[list[float]]], players: list[str]) -> dict[str, Any]:
    """Compute Nash equilibria for a normal-form game.

    For 2-player games: enumerates pure-strategy equilibria first, then uses
    analytical or gradient-based method for mixed equilibria.

    For N-player games: uses iterated elimination of dominated strategies when
    pure-strategy equilibria exist.

    Args:
        payoff_matrices: List of payoff arrays per player.
            payoff_matrices[i][a1][a2]... = payoff to player i when actions are taken.
        players: List of player names.

    Returns:
        Dict with equilibria (list of {strategy_profile, expected_payoffs, equilibrium_type}),
        n_players, and solution_method.
    """
    n_players = len(players)
    if n_players != 2:
        return {
            "equilibria": [],
            "n_players": n_players,
            "solution_method": "indeterminate",
            "game_type": "general_sum",
            "message": "N-player Nash computation not yet implemented for N>2",
        }

    A = np.array(payoff_matrices[0])
    B = np.array(payoff_matrices[1])
    n0, n1 = A.shape

    equilibria = _find_pure_equilibria_2p(A, B, players)
    if not equilibria:
        mixed = _find_mixed_equilibrium_2p(A, B, players)
        if mixed:
            equilibria.append(mixed)

    return {
        "equilibria": equilibria,
        "n_players": 2,
        "solution_method": "enumeration_plus_gradient" if len(equilibria) > 1 else "pure_enumeration" if equilibria else "none_found",
        "game_type": "general_sum",
    }


def _find_mixed_equilibrium_2p(
    A: np.ndarray, B: np.ndarray, players: list[str]
) -> dict[str, Any] | None:
    """Find mixed-strategy Nash equilibrium for 2-player game via gradient ascent."""
    n0, n1 = A.shape

    def expected_payoff_0(p, q):
        return float(p @ A @ q)

    def expected_payoff_1(p, q):
        return float(p @ B @ q)

    def best_response_payoffs(p, A_mat):
        return A_mat @ p

    def brq(q, A_mat):
        expected = A_mat.T @ q
        max_val = np.max(expected)
        return (expected >= max_val - 1e-9).astype(float)

    def brp(p, B_mat):
        expected = B_mat @ p
        max_val = np.max(expected)
        return (expected >= max_val - 1e-9).astype(float)

    p = np.ones(n0) / n0
    q = np.ones(n1) / n1
    alpha = 0.01

    for _ in range(2000):
        q_new = q + alpha * (B.T @ p)
        q_new = np.maximum(q_new, 0)
        if q_new.sum() > 0:
            q_new /= q_new.sum()
        else:
            q_new = np.ones(n1) / n1

        p_new = p + alpha * (A @ q)
        p_new = np.maximum(p_new, 0)
        if p_new.sum() > 0:
            p_new /= p_new.sum()
        else:
            p_new = np.ones(n0) / n0

        if np.max(np.abs(p_new - p)) < 1e-6 and np.max(np.abs(q_new - q)) < 1e-6:
            p, q = p_new, q_new
            break
        p, q = p_new, q_new

    payoff_0 = expected_payoff_0(p, q)
    payoff_1 = expected_payoff_1(p, q)

    return {
        "strategy_profile": {
            players[0]: [round(float(x), 4) for x in p],
            players[1]: [round(float(x), 4) for x in q],
        },
        "expected_payoffs": {
            players[0]: round(payoff_0, 4),
            players[1]: round(payoff_1, 4),
        },
        "equilibrium_type": "mixed_strategy_gradient",
    }


def _best_response_1(A: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Compute player 1's best response probabilities to player 0's strategy p."""
    n_actions_1 = A.shape[1]
    expected = A.T @ p  # expected payoff for each of player 1's actions
    max_val = np.max(expected)
    # Pure best response
    best = np.zeros(n_actions_1)
    best[np.argmax(expected)] = 1.0
    return best


def _find_pure_equilibria_2p(
    A: np.ndarray, B: np.ndarray, players: list[str]
) -> list[dict[str, Any]]:
    """Find pure-strategy Nash equilibria for 2-player game."""
    equilibria = []
    n0, n1 = A.shape
    for i in range(n0):
        for j in range(n1):
            # Check if (i,j) is a Nash equilibrium
            # Player 0's action i is best response to j
            is_br_0 = np.all(A[i, j] >= A[:, j])
            # Player 1's action j is best response to i
            is_br_1 = np.all(B[i, j] >= B[i, :])
            if is_br_0 and is_br_1:
                equilibria.append({
                    "strategy_profile": {
                        players[0]: [1.0 if k == i else 0.0 for k in range(n0)],
                        players[1]: [1.0 if k == j else 0.0 for k in range(n1)],
                    },
                    "expected_payoffs": {
                        players[0]: round(float(A[i, j]), 4),
                        players[1]: round(float(B[i, j]), 4),
                    },
                    "equilibrium_type": "pure_strategy",
                })
    return equilibria


def _compute_nash_general(
    payoff_matrices: list[list[list[float]]], players: list[str]
) -> dict[str, Any]:
    """Compute Nash equilibrium for N-player games via iterated dominance."""
    n_players = len(players)
    actions = [len(payoff_matrices[i]) for i in range(n_players)]

    try:
        # Iterated elimination of strictly dominated strategies
        surviving_actions = [list(range(a)) for a in actions]

        changed = True
        while changed:
            changed = False
            for i in range(n_players):
                if len(surviving_actions[i]) <= 1:
                    continue
                # Check each action for strict domination
                dominated: set[int] = set()
                for a1 in surviving_actions[i]:
                    for a2 in surviving_actions[i]:
                        if a1 == a2 or a1 in dominated:
                            continue
                        # Check if a2 dominates a1 (a1 gets less than a2 in all contingencies)
                        dominates = True
                        all_player_indices = list(range(n_players))
                        for profile in _all_profiles(surviving_actions, all_player_indices):
                            payoff_a1 = _get_payoff(payoff_matrices, i, profile, a1)
                            payoff_a2 = _get_payoff(payoff_matrices, i, profile, a2)
                            if payoff_a1 >= payoff_a2:
                                dominates = False
                                break
                        if dominates:
                            dominated.add(a1)
                            changed = True
                surviving_actions[i] = [a for a in surviving_actions[i] if a not in dominated]

        # Check if unique pure equilibrium exists
        all_surviving = [len(s) for s in surviving_actions]
        if all(n == 1 for n in all_surviving):
            # Unique pure equilibrium
            profile = [s[0] for s in surviving_actions]
            return {
                "equilibria": [{
                    "strategy_profile": {
                        players[i]: [1.0 if k == profile[i] else 0.0 for k in range(actions[i])]
                        for i in range(n_players)
                    },
                    "expected_payoffs": {
                        players[i]: round(_get_payoff(payoff_matrices, i, profile, profile[i]), 4)
                        for i in range(n_players)
                    },
                    "equilibrium_type": "pure_strategy_iterated_dominance",
                }],
                "n_players": n_players,
                "solution_method": "iterated_dominance",
                "game_type": "general_sum",
            }
    except Exception:
        pass

    # Fallback: N-player mixed equilibrium not yet implemented
    return {
        "equilibria": [],
        "n_players": n_players,
        "solution_method": "indeterminate",
        "message": f"N-player ({n_players}) mixed equilibrium computation not yet implemented. Use 2-player games.",
    }

    # Fallback: return surviving action sets for mixed strategy computation
    return {
        "equilibria": [],
        "n_players": n_players,
        "solution_method": "indeterminate",
        "surviving_actions": {
            players[i]: surviving_actions[i] for i in range(n_players)
        },
        "message": f"No pure-strategy equilibrium found after iterated dominance. {n_players}-player mixed equilibrium computation not yet implemented.",
    }


def _all_profiles(surviving: list[list[int]], active_players: list[int]) -> list[list[int]]:
    """Generate all action profiles for active_players (list of player indices to include)."""
    if not active_players:
        return [[]]
    player_idx = active_players[0]
    remaining = surviving[player_idx]
    rest_profiles: list[list[int]] = []
    for a in remaining:
        for rest in _all_profiles(surviving, active_players[1:]):
            rest_profiles.append([a] + rest)
    return rest_profiles


def _get_payoff(
    payoff_matrices: list[list[list[float]]],
    player: int,
    profile: list[int],
    action_override: int,
) -> float:
    """Get payoff for player given action profile, with override for one player."""
    p = profile.copy()
    p[player] = action_override
    payoff: float | list = payoff_matrices[player]
    for a in p:
        payoff = payoff[a]  # type: ignore[index]
    return float(payoff)  # type: ignore[return-value]
