"""Tests for OHM Markov chain analysis (OHM-g09)."""

from __future__ import annotations

import pytest

from ohm.markov import NUMPY_AVAILABLE, markov_absorbing_risk, markov_expected_steps
from tests.conftest import create_sample_edge, create_sample_node

pytestmark = pytest.mark.skipif(not NUMPY_AVAILABLE, reason="numpy not installed")


@pytest.fixture
def test_db():
    from tests.conftest import create_test_db

    return create_test_db()


def _build_linear_chain(conn):
    healthy = create_sample_node(conn, label="healthy")
    symptomatic = create_sample_node(conn, label="symptomatic")
    critical = create_sample_node(conn, label="critical")
    deceased = create_sample_node(conn, label="deceased")

    create_sample_edge(conn, from_node=healthy, to_node=symptomatic, edge_type="TRANSITIONS_TO", probability=0.3)
    create_sample_edge(conn, from_node=healthy, to_node=healthy, edge_type="TRANSITIONS_TO", probability=0.7)
    create_sample_edge(conn, from_node=symptomatic, to_node=critical, edge_type="TRANSITIONS_TO", probability=0.2)
    create_sample_edge(conn, from_node=symptomatic, to_node=symptomatic, edge_type="TRANSITIONS_TO", probability=0.5)
    create_sample_edge(conn, from_node=symptomatic, to_node=healthy, edge_type="TRANSITIONS_TO", probability=0.3)
    create_sample_edge(conn, from_node=critical, to_node=deceased, edge_type="TRANSITIONS_TO", probability=0.4)
    create_sample_edge(conn, from_node=critical, to_node=critical, edge_type="TRANSITIONS_TO", probability=0.6)

    return healthy, symptomatic, critical, deceased


class TestAbsorbingRisk:
    def test_linear_chain_absorption(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_absorbing_risk(test_db, healthy, edge_types=["TRANSITIONS_TO"])

        assert result["method"] == "markov_absorbing_risk"
        assert result["start_node"] == healthy
        assert deceased in result["absorption_probabilities"]
        assert 0 < result["absorption_probabilities"][deceased] <= 1.0
        assert deceased in result["absorbing_states"]
        assert healthy in result["transient_states"]

    def test_start_node_is_absorbing(self, test_db):
        node_a = create_sample_node(test_db, label="absorbing_only")
        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])
        assert result["absorption_probabilities"] == {node_a: 1.0}

    def test_start_node_not_in_graph(self, test_db):
        result = markov_absorbing_risk(test_db, "nonexistent", edge_types=["TRANSITIONS_TO"])
        assert "error" in result
        assert "not in graph" in result["error"]

    def test_start_node_exists_but_not_in_subgraph(self, test_db):
        """Node exists in DB but has no TRANSITIONS_TO edges — returns warning, not error (#975)."""
        isolated = create_sample_node(test_db, label="isolated")
        node_a = create_sample_node(test_db, label="ina")
        node_b = create_sample_node(test_db, label="inb")
        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=0.5)
        result = markov_absorbing_risk(test_db, isolated, edge_types=["TRANSITIONS_TO"])
        assert "warning" in result
        assert "exists" in result["warning"]
        assert result["absorption_probabilities"] == {isolated: 1.0}
        assert "error" not in result

    def test_no_edges(self, test_db):
        node_a = create_sample_node(test_db, label="isolated")
        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])
        assert result["absorption_probabilities"] == {node_a: 1.0}

    def test_absorption_probabilities_sum_to_one(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_absorbing_risk(test_db, healthy, edge_types=["TRANSITIONS_TO"])

        total = sum(result["absorption_probabilities"].values())
        assert abs(total - 1.0) < 0.01

    def test_multiple_absorbing_states(self, test_db):
        start = create_sample_node(test_db, label="start")
        sink_a = create_sample_node(test_db, label="sink_a")
        sink_b = create_sample_node(test_db, label="sink_b")

        create_sample_edge(conn=test_db, from_node=start, to_node=sink_a, edge_type="TRANSITIONS_TO", probability=0.6)
        create_sample_edge(conn=test_db, from_node=start, to_node=sink_b, edge_type="TRANSITIONS_TO", probability=0.4)

        result = markov_absorbing_risk(test_db, start, edge_types=["TRANSITIONS_TO"])

        assert abs(result["absorption_probabilities"][sink_a] - 0.6) < 0.01
        assert abs(result["absorption_probabilities"][sink_b] - 0.4) < 0.01


class TestExpectedSteps:
    def test_linear_chain_steps(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_expected_steps(test_db, healthy, edge_types=["TRANSITIONS_TO"])

        assert result["method"] == "markov_expected_steps"
        assert result["expected_steps"] > 0
        assert result["expected_steps_per_state"][healthy] > 0

    def test_absorbing_node_zero_steps(self, test_db):
        node_a = create_sample_node(test_db, label="absorbing")
        result = markov_expected_steps(test_db, node_a, edge_types=["TRANSITIONS_TO"])
        assert result["expected_steps"] == 0.0

    def test_transient_steps_greater_than_closer_states(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_expected_steps(test_db, healthy, edge_types=["TRANSITIONS_TO"])

        healthy_steps = result["expected_steps_per_state"][healthy]
        symptomatic_steps = result["expected_steps_per_state"][symptomatic]
        assert healthy_steps > symptomatic_steps

    def test_target_state(self, test_db):
        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        result = markov_expected_steps(test_db, healthy, target_state=deceased, edge_types=["TRANSITIONS_TO"])

        assert "target_state" in result
        assert "target_probability" in result
        assert result["target_probability"] > 0

    def test_start_node_exists_but_not_in_subgraph(self, test_db):
        """Node exists in DB but has no TRANSITIONS_TO edges — returns warning, not error (#975)."""
        isolated = create_sample_node(test_db, label="isolated")
        node_a = create_sample_node(test_db, label="ina")
        node_b = create_sample_node(test_db, label="inb")
        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=0.5)
        result = markov_expected_steps(test_db, isolated, edge_types=["TRANSITIONS_TO"])
        assert "warning" in result
        assert "exists" in result["warning"]
        assert result["expected_steps"] == 0.0
        assert "error" not in result


class TestSCCCollapse:
    def test_scc_detection(self, test_db):
        """Test that SCCs are properly detected in a graph with a cycle."""
        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")
        node_c = create_sample_node(test_db, label="c")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(test_db, from_node=node_b, to_node=node_a, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="TRANSITIONS_TO", probability=1.0)

        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])

        assert result["method"] == "markov_absorbing_risk"
        assert "sccs" in result
        multi_node_sccs = [s for s in result["sccs"] if len(s) > 1]
        assert len(multi_node_sccs) == 1
        assert set(multi_node_sccs[0]) == {node_a, node_b}

    def test_cycle_with_collapse_flag(self, test_db):
        """Test that SCC collapse can be triggered via collapse_sccs flag."""
        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")
        node_c = create_sample_node(test_db, label="c")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(test_db, from_node=node_b, to_node=node_a, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="TRANSITIONS_TO", probability=1.0)

        from ohm.inference.markov import _build_transition_matrix

        nodes, matrix, transient, absorbing, sccs, meta_members = _build_transition_matrix(test_db, edge_types=["TRANSITIONS_TO"], collapse_sccs=True)

        assert len(nodes) < 3
        assert len(sccs) == 2
        multi_node_sccs = [s for s in sccs if len(s) > 1]
        assert len(multi_node_sccs) == 1
        assert meta_members

    def test_three_node_cycle_absorption(self, test_db):
        """Test absorption from a 3-node cycle A->B->C->A with exit to sink.

        This graph is invertible (cycle has exits), so no SCC collapse
        is triggered — standard Markov absorption applies directly.
        """
        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")
        node_c = create_sample_node(test_db, label="c")
        node_sink = create_sample_node(test_db, label="sink")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=0.8)
        create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="TRANSITIONS_TO", probability=0.8)
        create_sample_edge(test_db, from_node=node_c, to_node=node_a, edge_type="TRANSITIONS_TO", probability=0.8)
        create_sample_edge(test_db, from_node=node_a, to_node=node_sink, edge_type="TRANSITIONS_TO", probability=0.2)

        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])

        assert result["method"] == "markov_absorbing_risk"
        assert node_sink in result["absorption_probabilities"]
        total = sum(result["absorption_probabilities"].values())
        assert abs(total - 1.0) < 0.02

    def test_three_node_cycle_expected_steps(self, test_db):
        """Test expected steps from a 3-node cycle A->B->C->A with exit to sink.

        This graph is invertible (cycle has exits), so standard Markov
        expected steps apply without SCC collapse.
        """
        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")
        node_c = create_sample_node(test_db, label="c")
        node_sink = create_sample_node(test_db, label="sink")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=0.8)
        create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="TRANSITIONS_TO", probability=0.8)
        create_sample_edge(test_db, from_node=node_c, to_node=node_a, edge_type="TRANSITIONS_TO", probability=0.8)
        create_sample_edge(test_db, from_node=node_a, to_node=node_sink, edge_type="TRANSITIONS_TO", probability=0.2)

        result = markov_expected_steps(test_db, node_a, edge_types=["TRANSITIONS_TO"])

        assert result["method"] == "markov_expected_steps"
        assert result["expected_steps"] > 0

    def test_sccs_detected_no_collapse(self, test_db):
        """Test that SCCs are properly reported even without collapse needed."""
        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")
        node_c = create_sample_node(test_db, label="c")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="TRANSITIONS_TO", probability=1.0)

        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])

        assert "sccs" in result
        assert result["scc_collapsed"] is False
        assert all(len(scc) == 1 for scc in result["sccs"])

    def test_singular_cycle_triggers_collapse(self, test_db):
        """Test that a closed cycle (no exits) triggers LinAlgError fallback.

        A<->B forms a cycle with no path to any absorbing state. The
        transient submatrix (I-Q) is singular, so SCC collapse is
        triggered. After collapse, A,B become a meta-node that is
        absorbing (no exits after collapse), and probability is
        distributed equally.
        """
        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(test_db, from_node=node_b, to_node=node_a, edge_type="TRANSITIONS_TO", probability=1.0)

        result = markov_absorbing_risk(test_db, node_a, edge_types=["TRANSITIONS_TO"])

        assert result["method"] == "markov_absorbing_risk"
        assert result["scc_collapsed"] is True
        assert len(result["collapsed_sccs"]) == 1
        assert set(result["collapsed_sccs"][0]) == {node_a, node_b}
        assert node_a in result["absorption_probabilities"]
        assert node_b in result["absorption_probabilities"]
        assert abs(result["absorption_probabilities"][node_a] - 0.5) < 0.01
        assert abs(result["absorption_probabilities"][node_b] - 0.5) < 0.01

    def test_singular_cycle_expected_steps(self, test_db):
        """Test expected steps from a closed cycle triggers collapse."""
        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(test_db, from_node=node_b, to_node=node_a, edge_type="TRANSITIONS_TO", probability=1.0)

        result = markov_expected_steps(test_db, node_a, edge_types=["TRANSITIONS_TO"])

        assert result["method"] == "markov_expected_steps"
        assert result["scc_collapsed"] is True


class TestMarkovCache:
    """Tests for module-level Markov transition-matrix cache."""

    def test_matrix_cache_reused_across_calls(self, test_db):
        from ohm.inference.markov import _markov_matrix_cache

        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        _markov_matrix_cache.clear()

        result1 = markov_absorbing_risk(test_db, healthy, edge_types=["TRANSITIONS_TO"])
        result2 = markov_absorbing_risk(test_db, healthy, edge_types=["TRANSITIONS_TO"])
        result3 = markov_expected_steps(test_db, healthy, edge_types=["TRANSITIONS_TO"])

        # Only one matrix build should be cached for identical parameters.
        assert len(_markov_matrix_cache) == 1
        assert result1["absorption_probabilities"]
        assert result2["absorption_probabilities"] == result1["absorption_probabilities"]
        assert result3["expected_steps"] is not None

    def test_cache_invalidation_on_graph_generation_change(self, test_db):
        from ohm.inference.markov import _markov_matrix_cache

        healthy, symptomatic, critical, deceased = _build_linear_chain(test_db)

        _markov_matrix_cache.clear()
        markov_absorbing_risk(test_db, healthy, edge_types=["TRANSITIONS_TO"])
        assert len(_markov_matrix_cache) == 1

        # Manually bump graph_generation (mimics what OhmStore does on writes).
        test_db.execute("UPDATE ohm_meta SET value = '1' WHERE key = 'graph_generation'")

        markov_absorbing_risk(test_db, healthy, edge_types=["TRANSITIONS_TO"])
        # New generation means a new cache entry, so total should be 2.
        assert len(_markov_matrix_cache) == 2


class TestCacheIdentityRegression:
    """Regression tests for cache key identity bug (issue #820).

    CPython's id() is only unique for the lifetime of an object. When a
    DuckDB :memory: connection is destroyed and a new one reuses the same
    address, the module-level cache (previously keyed by id(conn)) collides
    with the new connection, returning a stale matrix from an unrelated graph.
    """

    def test_no_stale_cache_when_conn_id_reused(self):
        """Connection 1 is dereferenced before connection 2 is created.

        CPython's allocator may reuse the same id() for conn2. The Markov
        cache must not return conn1's stale matrix.
        """
        from ohm.inference.markov import _markov_matrix_cache
        from tests.conftest import create_test_db

        _markov_matrix_cache.clear()

        # ── Connection 1: linear chain A→B→C→D (D is sole absorbing state) ──
        conn1 = create_test_db()
        a = create_sample_node(conn1, label="node_a")
        b = create_sample_node(conn1, label="node_b")
        c = create_sample_node(conn1, label="node_c")
        d = create_sample_node(conn1, label="node_d")
        create_sample_edge(conn1, from_node=a, to_node=b, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(conn1, from_node=b, to_node=c, edge_type="TRANSITIONS_TO", probability=1.0)
        create_sample_edge(conn1, from_node=c, to_node=d, edge_type="TRANSITIONS_TO", probability=1.0)
        result1 = markov_absorbing_risk(conn1, a, edge_types=["TRANSITIONS_TO"])
        assert d in result1["absorption_probabilities"]

        # Capture node IDs to reuse on conn2 (same IDs, different graph)
        node_ids = {"a": a, "b": b, "c": c, "d": d}

        # Destroy conn1 — its id() may be reused by conn2
        conn1.close()
        del conn1

        # ── Connection 2: multi-sink A→B, A→C using same node IDs ──
        conn2 = create_test_db()
        for label, nid in node_ids.items():
            conn2.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence) "
                "VALUES (?, ?, 'concept', 'test_agent', 'team', 'conversation', 1.0)",
                [nid, label],
            )
        create_sample_edge(conn2, from_node=node_ids["a"], to_node=node_ids["b"], edge_type="TRANSITIONS_TO", probability=0.6)
        create_sample_edge(conn2, from_node=node_ids["a"], to_node=node_ids["c"], edge_type="TRANSITIONS_TO", probability=0.4)

        # This must not raise KeyError (which happens if conn1's stale matrix is returned)
        result2 = markov_absorbing_risk(conn2, node_ids["a"], edge_types=["TRANSITIONS_TO"])

        assert result2["method"] == "markov_absorbing_risk"
        # Direct dict access — raises KeyError if stale cache from conn1
        prob_b = result2["absorption_probabilities"][node_ids["b"]]
        prob_c = result2["absorption_probabilities"][node_ids["c"]]
        assert abs(prob_b - 0.6) < 0.01
        assert abs(prob_c - 0.4) < 0.01
        # D is not reachable in conn2's graph — must not appear in results
        assert node_ids["d"] not in result2["absorption_probabilities"]

        conn2.close()


class TestCacheIdentityStress:
    """Stress tests for cache key identity fix (issue #820)."""

    def test_500_iterations_no_keyerror_or_wrong_collapse(self):
        """500 iterations of fresh connections + Markov queries.

        Each iteration creates a fresh in-memory DuckDB, builds a graph,
        and runs a Markov query. Alternates between multi-sink (absorbing
        risk) and singular-cycle (expected steps with SCC collapse) graphs.
        No KeyError or wrong scc_collapsed values should occur.
        """
        from ohm.inference.markov import _markov_matrix_cache
        from tests.conftest import create_test_db

        _markov_matrix_cache.clear()

        for i in range(500):
            conn = create_test_db()
            try:
                if i % 2 == 0:
                    # Multi-sink graph (test_multiple_absorbing_states style)
                    start = create_sample_node(conn, label="start")
                    sink_a = create_sample_node(conn, label="sink_a")
                    sink_b = create_sample_node(conn, label="sink_b")
                    create_sample_edge(conn, from_node=start, to_node=sink_a, edge_type="TRANSITIONS_TO", probability=0.6)
                    create_sample_edge(conn, from_node=start, to_node=sink_b, edge_type="TRANSITIONS_TO", probability=0.4)
                    result = markov_absorbing_risk(conn, start, edge_types=["TRANSITIONS_TO"])
                    assert result["method"] == "markov_absorbing_risk", f"iteration {i}"
                    # Direct dict access — raises KeyError if stale cache
                    prob_a = result["absorption_probabilities"][sink_a]
                    assert abs(prob_a - 0.6) < 0.01, f"iteration {i}: prob_a={prob_a}"
                else:
                    # Singular cycle (test_singular_cycle_expected_steps style)
                    node_a = create_sample_node(conn, label="a")
                    node_b = create_sample_node(conn, label="b")
                    create_sample_edge(conn, from_node=node_a, to_node=node_b, edge_type="TRANSITIONS_TO", probability=1.0)
                    create_sample_edge(conn, from_node=node_b, to_node=node_a, edge_type="TRANSITIONS_TO", probability=1.0)
                    result = markov_expected_steps(conn, node_a, edge_types=["TRANSITIONS_TO"])
                    assert result["method"] == "markov_expected_steps", f"iteration {i}"
                    assert result["scc_collapsed"] is True, f"iteration {i}: scc_collapsed={result['scc_collapsed']}"
            finally:
                conn.close()
            del conn
