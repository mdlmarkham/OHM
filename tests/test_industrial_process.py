"""Industrial process acceptance test — OHM-brps / OHM-8dg4.

Worked example of the OHM temporal decision layer for a chemical reactor
with process state, inventory, and market. Demonstrates the full autonomy
loop: scenario -> action -> execution -> feedback.

Graph structure:
    Reactor (concept, gate_type=AND) --CAUSES--> Yield (concept)
    Feed Stock (concept) --CAUSES--> Reactor
    Market Price (concept) --PREDICTS--> Revenue (concept)
    Reactor Twin (twin) --EVALUATES--> Reactor
    Scenario (scenario) --COUNTERFACTUAL_OF--> Reactor
    Scenario --PROPOSES_ACTION--> Action (action)
    Action --EXECUTED_BY--> Agent

The test exercises:
1. AND-gate governance (gate_type, gate_status, constraint_expr)
2. Counterfactual scenario with edge overrides
3. Action proposal and execution
4. Loop status reporting
"""

from __future__ import annotations

import duckdb
import pytest

from ohm.schema import initialize_schema
from ohm.queries import (
    create_node,
    create_edge,
    query_counterfactual_cascade,
    query_compare_scenarios,
    propose_action,
    execute_action,
    query_loop_status,
)


@pytest.fixture
def industrial_conn():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


def _seed_industrial_graph(conn):
    """Build the industrial process graph."""
    # Core process nodes
    reactor = create_node(conn, label="Chemical Reactor R-101", node_type="concept", created_by="plant_agent")
    conn.execute(
        "UPDATE ohm_nodes SET gate_type = 'AND', gate_status = 'intact' WHERE id = ?",
        [reactor["id"]],
    )

    yield_node = create_node(conn, label="Daily Yield", node_type="concept", created_by="plant_agent")
    feed_stock = create_node(conn, label="Feed Stock Inventory", node_type="concept", created_by="plant_agent")
    market_price = create_node(conn, label="Market Price", node_type="concept", created_by="trading_agent")
    revenue = create_node(conn, label="Daily Revenue", node_type="concept", created_by="trading_agent")

    # Causal edges with probabilities
    e_feed_to_reactor = create_edge(
        conn, from_node=feed_stock["id"], to_node=reactor["id"],
        edge_type="CAUSES", layer="L3", created_by="plant_agent", probability=0.85,
    )
    e_reactor_to_yield = create_edge(
        conn, from_node=reactor["id"], to_node=yield_node["id"],
        edge_type="CAUSES", layer="L3", created_by="plant_agent", probability=0.9,
    )
    e_yield_to_revenue = create_edge(
        conn, from_node=yield_node["id"], to_node=revenue["id"],
        edge_type="CAUSES", layer="L3", created_by="trading_agent", probability=0.95,
    )
    e_price_to_revenue = create_edge(
        conn, from_node=market_price["id"], to_node=revenue["id"],
        edge_type="EXPECTED_LIKELIHOOD", layer="L3", created_by="trading_agent", probability=0.8,
    )

    # AND-gate constraint: reactor requires BOTH feed stock and catalyst
    conn.execute(
        "UPDATE ohm_edges SET constraint_expr = 'feed_stock AND catalyst_flow' WHERE id = ?",
        [e_feed_to_reactor["id"]],
    )

    # Register a digital twin for the reactor
    twin = create_node(
        conn, label="Reactor Digital Twin", node_type="twin",
        created_by="plant_agent", connects_to=[reactor["id"]],
    )
    # Link twin to reactor via EVALUATES edge
    create_edge(
        conn, from_node=twin["id"], to_node=reactor["id"],
        edge_type="EVALUATES", layer="L3", created_by="plant_agent",
    )

    return {
        "reactor": reactor,
        "yield": yield_node,
        "feed_stock": feed_stock,
        "market_price": market_price,
        "revenue": revenue,
        "twin": twin,
        "e_feed": e_feed_to_reactor,
        "e_reactor": e_reactor_to_yield,
        "e_yield": e_yield_to_revenue,
        "e_price": e_price_to_revenue,
    }


class TestIndustrialProcessAcceptance:
    """Acceptance test for OHM-brps — industrial process with temporal decision layer."""

    def test_and_gate_governance(self, industrial_conn):
        """Reactor has AND-gate type and intact status."""
        nodes = _seed_industrial_graph(industrial_conn)
        row = industrial_conn.execute(
            "SELECT gate_type, gate_status FROM ohm_nodes WHERE id = ?",
            [nodes["reactor"]["id"]],
        ).fetchone()
        assert row[0] == "AND"
        assert row[1] == "intact"

    def test_constraint_expr_on_edge(self, industrial_conn):
        """Feed-to-reactor edge has a constraint expression."""
        nodes = _seed_industrial_graph(industrial_conn)
        row = industrial_conn.execute(
            "SELECT constraint_expr FROM ohm_edges WHERE id = ?",
            [nodes["e_feed"]["id"]],
        ).fetchone()
        assert row[0] == "feed_stock AND catalyst_flow"

    def test_twin_registered(self, industrial_conn):
        """A digital twin node is registered and linked to the reactor."""
        nodes = _seed_industrial_graph(industrial_conn)
        assert nodes["twin"]["type"] == "twin"
        # Twin must be linked to reactor (cross-link requirement)
        edges = industrial_conn.execute(
            "SELECT edge_type FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
            [nodes["twin"]["id"], nodes["twin"]["id"]],
        ).fetchall()
        assert len(edges) >= 1

    def test_baseline_cascade(self, industrial_conn):
        """Baseline cascade: feed (0.85) * reactor (0.9) * yield (0.95) = 0.727."""
        nodes = _seed_industrial_graph(industrial_conn)
        cascade = query_counterfactual_cascade(
            industrial_conn, nodes["feed_stock"]["id"], failure_probability=1.0,
        )
        labels = {r["node_label"]: r["failure_probability"] for r in cascade}
        assert labels["Chemical Reactor R-101"] == pytest.approx(0.85, abs=0.01)
        assert labels["Daily Yield"] == pytest.approx(0.85 * 0.9, abs=0.01)
        assert labels["Daily Revenue"] == pytest.approx(0.85 * 0.9 * 0.95, abs=0.02)

    def test_counterfactual_feed_disruption(self, industrial_conn):
        """What if feed stock reliability drops to 0.3? Revenue should decrease."""
        nodes = _seed_industrial_graph(industrial_conn)
        comparison = query_compare_scenarios(
            industrial_conn, nodes["feed_stock"]["id"],
            failure_probability=1.0,
            edge_overrides={nodes["e_feed"]["id"]: 0.3},
        )
        assert comparison["summary"]["decreased"] >= 2
        # Find the revenue node in deltas by node_id
        revenue_delta = [
            d for d in comparison["deltas"]
            if d["node_id"] == nodes["revenue"]["id"]
        ]
        assert len(revenue_delta) == 1
        assert revenue_delta[0]["direction"] == "decreased"

    def test_full_autonomy_loop(self, industrial_conn):
        """Full loop: scenario -> propose action -> execute -> loop status."""
        nodes = _seed_industrial_graph(industrial_conn)

        # 1. Create a scenario: "What if feed reliability drops?"
        scenario = create_node(
            industrial_conn,
            label="Feed disruption scenario",
            node_type="scenario",
            created_by="plant_agent",
            connects_to=[nodes["reactor"]["id"]],
        )

        # 2. Propose an action: "Increase buffer stock to 30 days"
        action = propose_action(
            industrial_conn,
            scenario_id=scenario["id"],
            label="Increase buffer stock to 30 days",
            created_by="plant_agent",
            rationale="Mitigate feed supply disruption risk",
        )
        assert action["task_status"] == "proposed"

        # 3. Execute the action
        result = execute_action(
            industrial_conn,
            action_id=action["id"],
            executed_by="plant_agent",
            outcome="TRUE",
            outcome_notes="Buffer stock increased, reactor uptime maintained",
        )
        assert result["task_status"] == "executed"
        assert result["outcome"] == "TRUE"

        # 4. Check loop status
        status = query_loop_status(industrial_conn, agent_name="plant_agent")
        assert status["summary"]["executed"] == 1
        assert status["summary"]["outcomes"].get("TRUE") == 1
        assert len(status["recent_scenarios"]) >= 1

    def test_compromised_gate_status(self, industrial_conn):
        """Gate can be marked as compromised when an input fails."""
        nodes = _seed_industrial_graph(industrial_conn)
        industrial_conn.execute(
            "UPDATE ohm_nodes SET gate_status = 'compromised' WHERE id = ?",
            [nodes["reactor"]["id"]],
        )
        row = industrial_conn.execute(
            "SELECT gate_status FROM ohm_nodes WHERE id = ?",
            [nodes["reactor"]["id"]],
        ).fetchone()
        assert row[0] == "compromised"

    def test_metis_gate_status_aliases(self, industrial_conn):
        """Metis design-note gate_status values (open/closed/stuck) are valid."""
        from ohm.schema import VALID_GATE_STATUSES
        assert "open" in VALID_GATE_STATUSES
        assert "closed" in VALID_GATE_STATUSES
        assert "stuck" in VALID_GATE_STATUSES