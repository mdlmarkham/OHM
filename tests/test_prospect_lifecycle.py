"""Tests for OHM-844: MCP-first prospect lifecycle surfaces.

Covers all 4 endpoints:
  POST /prospect            — create a prospect node
  POST /prospect/transition/ — transition status with authority check
  GET  /prospects           — list with status/tag/creator filters
  GET  /prospect/{id}       — detail with children and observations

All tests use the test_server fixture (no-auth dev mode) for HTTP
and direct DuckDB queries for verification.
"""

import json
import pytest

from tests.conftest import _request

pytestmark = pytest.mark.integration


@pytest.fixture
def seed_graph(test_server):
    """Create a minimal graph with nodes that a prospect can reference."""
    port, store = test_server
    conn = store.conn

    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at) VALUES "
        "('src1', 'Market analysis', 'source', 'Analysis doc', 'metis', CURRENT_TIMESTAMP), "
        "('cnc1', 'Strategy alpha', 'concept', 'Core strategy', 'metis', CURRENT_TIMESTAMP), "
        "('evt1', 'Revenue event', 'event', 'Q2 revenue', 'metis', CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES "
        "('cnc1', 'src1', 'SUPPORTS', 'L2', 0.9, 'metis', CURRENT_TIMESTAMP)"
    )
    conn.commit()
    return port, store


class TestPostProspect:
    """POST /prospect — create a prospect node."""

    def test_create_minimal(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {"label": "Hormuz expansion"})
        assert status == 201
        assert data["type"] == "prospect"
        assert data["label"] == "Hormuz expansion"
        assert data["task_status"] == "proposed"

    def test_create_with_authority(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {
            "label": "Build pipeline",
            "authority": "hermes",
        })
        assert status == 201
        assert data["task_status"] == "proposed"

        conn = seed_graph[1].conn
        row = conn.execute(
            "SELECT assigned_to FROM ohm_nodes WHERE id = ?", [data["id"]]
        ).fetchone()
        assert row and row[0] == "hermes"

    def test_create_with_tags(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {
            "label": "Risk review",
            "tags": ["risk", "quarterly"],
        })
        assert status == 201

        conn = seed_graph[1].conn
        row = conn.execute(
            "SELECT tags FROM ohm_nodes WHERE id = ?", [data["id"]]
        ).fetchone()
        assert row and "risk" in json.loads(row[0])

    def test_create_with_horizon(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {
            "label": "Hormuz plan",
            "planned_start": "2026-07-01",
            "planned_end": "2026-12-31",
            "horizon_label": "H3 2026",
        })
        assert status == 201

        conn = seed_graph[1].conn
        row = conn.execute(
            "SELECT metadata FROM ohm_nodes WHERE id = ?", [data["id"]]
        ).fetchone()
        assert row
        meta = json.loads(row[0]) if row[0] else {}
        assert meta.get("planned_start") == "2026-07-01"
        assert meta.get("planned_end") == "2026-12-31"
        assert meta.get("horizon_label") == "H3 2026"

    def test_create_with_cross_links(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {
            "label": "Linked prospect",
            "connects_to": ["src1", "cnc1"],
        })
        assert status == 201
        assert data["id"]

    def test_create_cross_link_missing_node_fails(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {
            "label": "Bad links",
            "connects_to": ["nonexistent_xyz"],
        })
        assert status >= 400

    def test_create_missing_label_fails(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {"content": "no label"})
        assert status == 400

    def test_create_creates_assessment_observation(self, seed_graph):
        """Create prospect does NOT create an observation (observation comes from transition)."""
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect", {
            "label": "Observed prospect",
            "content": "Initial rationale",
        })
        assert status == 201
        assert data["type"] == "prospect"


class TestPostProspectTransition:
    """POST /prospect/transition/{id} — transition status."""

    def _create_prospect(self, conn, pid, status="proposed", assigned_to=None):
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, task_status, assigned_to, created_by, created_at) "
            "VALUES (?, ?, 'prospect', ?, ?, 'metis', CURRENT_TIMESTAMP)",
            [pid, f"Prospect {pid}", status, assigned_to],
        )
        conn.commit()

    def test_proposed_to_committed(self, seed_graph):
        port, store = seed_graph
        self._create_prospect(store.conn, "p1")

        status, data = _request("POST", port, "/prospect/transition/p1", {
            "new_status": "committed",
            "reason": "Approved by council",
        })
        assert status == 200
        assert data["task_status"] == "committed"

        row = store.conn.execute("SELECT task_status FROM ohm_nodes WHERE id = 'p1'").fetchone()
        assert row[0] == "committed"

    def test_committed_to_active(self, seed_graph):
        port, store = seed_graph
        self._create_prospect(store.conn, "p2", status="committed")

        status, data = _request("POST", port, "/prospect/transition/p2", {
            "new_status": "active",
        })
        assert status == 200
        assert data["task_status"] == "active"

    def test_active_to_completed(self, seed_graph):
        port, store = seed_graph
        self._create_prospect(store.conn, "p3", status="active")

        status, data = _request("POST", port, "/prospect/transition/p3", {
            "new_status": "completed",
            "reason": "All deliverables met",
        })
        assert status == 200
        assert data["task_status"] == "completed"

    def test_invalid_transition_fails(self, seed_graph):
        port, store = seed_graph
        self._create_prospect(store.conn, "p4")

        status, data = _request("POST", port, "/prospect/transition/p4", {
            "new_status": "completed",
        })
        assert status == 422

    def test_authority_check_blocks_wrong_agent(self, seed_graph):
        port, store = seed_graph
        self._create_prospect(store.conn, "p5", assigned_to="hermes")

        status, data = _request("POST", port, "/prospect/transition/p5", {
            "new_status": "committed",
        })
        assert status == 403

    def test_transition_creates_assessment_observation(self, seed_graph):
        port, store = seed_graph
        self._create_prospect(store.conn, "p6")

        _request("POST", port, "/prospect/transition/p6", {
            "new_status": "committed",
            "reason": "Board approval",
        })

        obs = store.conn.execute(
            "SELECT type, notes FROM ohm_observations "
            "WHERE node_id = 'p6' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert obs
        assert obs[0] == "assessment"
        assert "committed" in obs[1].lower()

    def test_terminal_state_blocks_transition(self, seed_graph):
        port, store = seed_graph
        self._create_prospect(store.conn, "p7", status="completed")

        status, data = _request("POST", port, "/prospect/transition/p7", {
            "new_status": "active",
        })
        assert status == 422

    def test_nonexistent_prospect_fails(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("POST", port, "/prospect/transition/nonexistent_xyz", {
            "new_status": "committed",
        })
        assert status >= 400


class TestGetProspects:
    """GET /prospects — list prospects with filters."""

    def _seed_multiple(self, store):
        conn = store.conn
        rows = [
            ("lp1", "Alpha plan", "proposed", '["risk","quarterly"]'),
            ("lp2", "Beta plan", "committed", '["ops"]'),
            ("lp3", "Gamma plan", "active", '["risk","ops"]'),
            ("lp4", "Delta plan", "completed", '["long-term"]'),
            ("lp5", "Epsilon plan", "proposed", '["ops"]'),
        ]
        for rid, label, st, tags in rows:
            conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, task_status, tags, created_by, created_at) "
                "VALUES (?, ?, 'prospect', ?, ?, 'metis', CURRENT_TIMESTAMP)",
                [rid, label, st, tags],
            )
        conn.commit()

    def test_list_all(self, seed_graph):
        port, store = seed_graph
        self._seed_multiple(store)
        status, data = _request("GET", port, "/prospects")
        assert status == 200
        assert data["count"] == 5

    def test_filter_by_status(self, seed_graph):
        port, store = seed_graph
        self._seed_multiple(store)
        status, data = _request("GET", port, "/prospects?status=proposed")
        assert status == 200
        assert data["count"] == 2

    def test_filter_by_tag(self, seed_graph):
        port, store = seed_graph
        self._seed_multiple(store)
        status, data = _request("GET", port, "/prospects?tags=risk&tags=ops")
        assert status == 200
        assert data["count"] == 1
        assert data["results"][0]["label"] == "Gamma plan"

    def test_single_tag_filter(self, seed_graph):
        port, store = seed_graph
        self._seed_multiple(store)
        status, data = _request("GET", port, "/prospects?tags=ops")
        assert status == 200
        labels = {r["label"] for r in data["results"]}
        assert labels == {"Beta plan", "Gamma plan", "Epsilon plan"}

    def test_limit(self, seed_graph):
        port, store = seed_graph
        self._seed_multiple(store)
        status, data = _request("GET", port, "/prospects?limit=2")
        assert status == 200
        assert data["count"] == 2

    def test_empty_result(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("GET", port, "/prospects?status=superseded")
        assert status == 200
        assert data["count"] == 0


class TestGetProspectDetail:
    """GET /prospect/{id} — prospect detail with children and observations."""

    def test_detail_basic(self, seed_graph):
        port, store = seed_graph
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, task_status, created_by, created_at) "
            "VALUES ('dt1', 'Detail prospect', 'prospect', 'active', 'metis', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO ohm_observations (node_id, type, value, created_by, notes) "
            "VALUES ('dt1', 'assessment', 1.0, 'metis', 'Prospect on track')"
        )
        conn.commit()

        status, data = _request("GET", port, "/prospect/dt1")
        assert status == 200
        assert data["prospect"]["label"] == "Detail prospect"
        assert data["prospect"]["task_status"] == "active"
        assert data["latest_observation"] is not None

    def test_detail_with_children(self, seed_graph):
        port, store = seed_graph
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, task_status, created_by, created_at) "
            "VALUES ('dt2', 'Parent prospect', 'prospect', 'active', 'metis', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, task_status, created_by, created_at) "
            "VALUES ('dt2_child1', 'Child expectation', 'expectation', 'active', 'metis', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at) "
            "VALUES ('dt2', 'dt2_child1', 'CONTAINS', 'L4', 1.0, 'metis', CURRENT_TIMESTAMP)"
        )
        conn.commit()

        status, data = _request("GET", port, "/prospect/dt2")
        assert status == 200
        child_ids = [c["id"] for c in data.get("children", [])]
        assert "dt2_child1" in child_ids

    def test_detail_nonexistent(self, seed_graph):
        port, _ = seed_graph
        status, data = _request("GET", port, "/prospect/nonexistent_xyz")
        assert status == 404

    def test_detail_without_observations(self, seed_graph):
        port, store = seed_graph
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, task_status, created_by, created_at) "
            "VALUES ('dt3', 'Clean prospect', 'prospect', 'proposed', 'metis', CURRENT_TIMESTAMP)"
        )
        conn.commit()

        status, data = _request("GET", port, "/prospect/dt3")
        assert status == 200
        assert data["prospect"]["label"] == "Clean prospect"
        assert data["latest_observation"] is None
