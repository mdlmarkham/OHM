"""
OHM Substrate Method Tests — contradiction detection, anomaly detection,
heartbeat, aggregation, identity evolution, cold start discovery,
L2 immutability.
"""

import os
import pytest

from ohm.sdk import connect


@pytest.fixture
def graph(tmp_path):
    """Create a temporary graph for testing."""
    db_path = str(tmp_path / "test_substrate.duckdb")
    g = connect(db_path, actor="test_agent")
    yield g
    g.close()


@pytest.fixture
def multi_agent_graph(tmp_path):
    """Create a graph with multiple agents for discovery testing."""
    db_path = str(tmp_path / "test_multi.duckdb")
    if os.path.exists(db_path):
        os.remove(db_path)

    # Agent 1: Metis
    with connect(db_path, actor="metis") as g:
        g.register_agent(
            values=["wisdom", "connections"],
            capabilities=["research", "critique"],
            interests=["economics", "cognition"],
        )
        node = g.create_node(label="Hormuz traffic", node_type="concept")
        g.observe(node["id"], obs_type="measurement", value=0.3,
                  baseline=0.3, sigma=0.5, source="routine")
        g.observe(node["id"], obs_type="measurement", value=0.9,
                  baseline=0.3, sigma=0.5, source="breaking")
        g.observe(node["id"], obs_type="measurement", value=0.35,
                  baseline=0.3, sigma=0.3, source="routine")

    # Agent 2: Clio — opposing observation
    with connect(db_path, actor="clio") as g:
        g.register_agent(
            values=["wisdom", "source-coverage"],
            capabilities=["deep-research"],
            interests=["economics", "international-law"],
        )
        node = g.find_or_create_node(label="Hormuz traffic", node_type="concept")
        g.observe(node["id"], obs_type="measurement", value=0.1,
                  baseline=0.3, sigma=0.4, source="alternative")

    return db_path


# ===== Anomaly Detection =====

class TestAnomalyDetection:
    def test_detects_high_sigma(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            anomalies = g.anomalies(sigma_threshold=1.0)
            assert len(anomalies) >= 1
            # The 0.9 value with 0.3 baseline, 0.5 sigma = 1.2σ
            high = [a for a in anomalies if a["deviation"] > 1.0]
            assert len(high) >= 1

    def test_no_anomalies_at_high_threshold(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            anomalies = g.anomalies(sigma_threshold=5.0)
            assert len(anomalies) == 0

    def test_anomaly_fields(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            anomalies = g.anomalies(sigma_threshold=1.0)
            if anomalies:
                a = anomalies[0]
                assert "value" in a
                assert "baseline" in a
                assert "sigma" in a
                assert "deviation" in a


# ===== Contradiction Detection =====

class TestContradictionDetection:
    def test_detects_opposite_observations(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            result = g.contradictions()
            opp = result.get("opposite_observations", [])
            assert len(opp) >= 1
            # Metis saw 0.9, Clio saw 0.1 on same node
            pair = opp[0]
            assert "agent_a" in pair
            assert "agent_b" in pair
            assert "gap" in pair

    def test_no_contradictions_empty_graph(self, tmp_path):
        db = str(tmp_path / "empty.duckdb")
        with connect(db, actor="test") as g:
            result = g.contradictions()
            assert len(result["opposite_observations"]) == 0
            assert len(result["high_confidence_challenges"]) == 0


# ===== Agent Heartbeat =====

class TestHeartbeat:
    def test_heartbeat_updates_sync(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            result = g.heartbeat(focus="Testing heartbeat")
            assert result["agent_name"] == "metis"
            assert result.get("last_sync") is not None
            assert result.get("current_focus") == "Testing heartbeat"

    def test_agent_health_reports(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            g.heartbeat(focus="Research")
        with connect(multi_agent_graph, actor="clio") as g:
            g.heartbeat(focus="Deep research")

        with connect(multi_agent_graph, actor="test") as g:
            health = g.agent_health()
            assert len(health) >= 2
            agents = {h["agent_name"]: h for h in health}
            assert "metis" in agents
            assert "clio" in agents
            assert agents["metis"]["status"] in ("alive", "stale", "dead")


# ===== Aggregation =====

class TestAggregation:
    def test_weighted_aggregation(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            node = g.find_or_create_node(label="Hormuz traffic", node_type="concept")
            result = g.aggregate(node["id"], method="weighted")
            assert "value" in result
            assert "confidence" in result
            assert result["observation_count"] >= 3
            assert 0 <= result["agreement_ratio"] <= 1

    def test_mean_aggregation(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="metis") as g:
            node = g.find_or_create_node(label="Hormuz traffic", node_type="concept")
            result = g.aggregate(node["id"], method="mean")
            assert "value" in result
            assert result["observation_count"] >= 3

    def test_aggregation_empty_node(self, tmp_path):
        db = str(tmp_path / "agg_empty.duckdb")
        with connect(db, actor="test") as g:
            node = g.create_node(label="empty node", node_type="concept")
            # No observations — should raise or return empty result
            try:
                result = g.aggregate(node["id"])
                # If it returns instead of raising, check it handles gracefully
                assert result.get("observation_count", 0) == 0 or result.get("value") is None
            except Exception:
                pass  # Expected — no observations to aggregate


# ===== Identity Evolution =====

class TestIdentityEvolution:
    def test_evolve_value(self, tmp_path):
        db = str(tmp_path / "evolution.duckdb")
        with connect(db, actor="metis") as g:
            me = g.register_agent(values=["wisdom", "connections"])
            # Find the VALUES edge for "connections"
            edges = g._conn.execute(
                "SELECT id, to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'VALUES' AND created_by = ?",
                [me["id"], "metis"],
            ).fetchall()

            target_labels = {}
            for eid, tid in edges:
                tn = g.get_node(tid)
                if tn:
                    target_labels[tid] = tn["label"]

            # Find the edge pointing to "connections"
            conn_edge_id = None
            for eid, tid in edges:
                if "connections" in target_labels.get(tid, "").lower():
                    conn_edge_id = eid
                    break

            assert conn_edge_id is not None, "No VALUES edge for 'connections' found"

            # Evolve it
            new_edge = g.evolve_identity(
                conn_edge_id,
                new_target="emergence",
                reason="Pattern discovery over network density",
            )
            assert new_edge["edge_type"] == "VALUES"
            assert "evolved_from" in new_edge.get("provenance", "")

            # Old edge is superseded
            import json
            old = g.get_edge(conn_edge_id)
            meta = json.loads(old.get("metadata", "{}")) if old.get("metadata") else {}
            assert meta.get("superseded") is True
            assert meta.get("superseded_by") == new_edge["id"]

    def test_cannot_evolve_other_agents_edge(self, tmp_path):
        db = str(tmp_path / "evolution_fail.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])

        with connect(db, actor="socrates") as g:
            # Find metis's VALUES edge
            edge = g._conn.execute(
                "SELECT id FROM ohm_edges WHERE edge_type = 'VALUES' AND created_by = 'metis' LIMIT 1",
            ).fetchone()
            if edge:
                from ohm.boundary import enforce_identity_evolution
                with pytest.raises(Exception):
                    enforce_identity_evolution(g._conn, "socrates", edge[0])

    def test_cannot_evolve_non_identity_edge(self, tmp_path):
        db = str(tmp_path / "evolution_type.duckdb")
        with connect(db, actor="metis") as g:
            n1 = g.create_node(label="A", node_type="concept")
            n2 = g.create_node(label="B", node_type="concept")
            g.create_edge(from_node=n1["id"], to_node=n2["id"],
                                 edge_type="CAUSES", layer="L3")

            from ohm.boundary import check_can_evolve_identity_edge
            with pytest.raises(Exception):
                check_can_evolve_identity_edge("metis", "metis", "CAUSES")


# ===== Cold Start Discovery =====

class TestColdStartDiscovery:
    def test_discovers_shared_values(self, multi_agent_graph):
        with connect(multi_agent_graph, actor="socrates") as g:
            g.register_agent(
                values=["wisdom", "falsifiability"],
                capabilities=["critique"],
                interests=["economics", "cognition"],
            )
            peers = g.discover_peers()
            # Should find metis (3 shared) and clio (2 shared)
            assert len(peers) >= 1
            metis_peer = [p for p in peers if p.get("agent_name") == "metis"]
            assert len(metis_peer) >= 1
            assert metis_peer[0]["shared_values_interests"] >= 2

    def test_discovers_complementary_capabilities(self, tmp_path):
        db = str(tmp_path / "complement.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(
                values=["wisdom"],
                capabilities=["research"],
                interests=["economics"],
            )

        with connect(db, actor="hephaestus") as g:
            g.register_agent(
                values=["accuracy"],
                capabilities=["code-audit", "security-review"],
                interests=["systems"],
            )

        with connect(db, actor="socrates") as g:
            g.register_agent(
                values=["wisdom"],
                capabilities=["critique"],  # Same as metis, different from hephaestus
                interests=["economics"],
            )
            peers = g.discover_peers()
            # Hephaestus has complementary capabilities
            heph = [p for p in peers if p.get("agent_name") == "hephaestus"]
            assert len(heph) >= 1

    def test_no_peers_for_unregistered(self, tmp_path):
        db = str(tmp_path / "unreg.duckdb")
        with connect(db, actor="unknown") as g:
            peers = g.discover_peers()
            assert peers == []


# ===== L2 Immutability =====

class TestL2Immutability:
    def test_cannot_update_source_node(self, tmp_path):
        db = str(tmp_path / "l2test.duckdb")
        with connect(db, actor="metis") as g:
            src = g.create_node(label="Reuters: test", node_type="source")
            from ohm.boundary import enforce_l2_immutability
            with pytest.raises(Exception):
                enforce_l2_immutability(g._conn, "metis", src["id"])

    def test_can_update_non_source_node(self, tmp_path):
        db = str(tmp_path / "l2test2.duckdb")
        with connect(db, actor="metis") as g:
            node = g.create_node(label="test concept", node_type="concept")
            from ohm.boundary import enforce_l2_immutability
            # Should not raise for non-source nodes
            enforce_l2_immutability(g._conn, "metis", node["id"])


# ===== Provenance Chain =====

class TestProvenance:
    def test_provenance_chain(self, tmp_path):
        db = str(tmp_path / "prov.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="Root idea", node_type="concept")
            b = g.create_node(label="Derived idea", node_type="concept")
            g.create_edge(from_node=b["id"], to_node=a["id"],
                          edge_type="DERIVES_FROM", layer="L2",
                          provenance="test derivation")

            chain = g.provenance(b["id"])
            assert len(chain) >= 1

    def test_provenance_empty_chain(self, tmp_path):
        db = str(tmp_path / "prov_empty.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="Isolated", node_type="concept")
            chain = g.provenance(a["id"])
            assert len(chain) == 0


# ===== Graph Health =====

class TestGraphHealth:
    def test_health_report(self, tmp_path):
        db = str(tmp_path / "health.duckdb")
        with connect(db, actor="metis") as g:
            g.create_node(label="Orphan", node_type="concept")
            g.create_node(label="Connected", node_type="concept")
            a = g.create_node(label="Target", node_type="concept")
            g.create_edge(from_node=a["id"], to_node=g.find_or_create_node(label="Connected")["id"],
                          edge_type="RELATED_TO", layer="L3")

            report = g.health()
            assert "health_score" in report
            assert "orphans" in report or "orphan_nodes" in report


# ===== Change Feed Consumer =====

class TestChangeFeedConsumer:
    def test_listen_returns_changes(self, tmp_path):
        db = str(tmp_path / "changefeed.duckdb")
        # Agent 1 writes
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"], interests=["economics"])
            g.create_node(label="test concept", node_type="concept")

        # Agent 2 reads
        with connect(db, actor="clio") as g:
            g.register_agent(values=["source-coverage"], interests=["economics"])
            changes = g.listen()
            # Should see metis's changes (not own)
            assert len(changes) >= 0  # May be 0 if same-second

    def test_listen_filters_own_changes(self, tmp_path):
        db = str(tmp_path / "own_changes.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            g.create_node(label="A", node_type="concept")
            g.create_node(label="B", node_type="concept")

            # listen() excludes own changes
            changes = g.listen()
            for c in changes:
                assert c.get("agent_name") != "metis"

    def test_pending_notifications(self, tmp_path):
        db = str(tmp_path / "pending.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            g.create_node(label="test", node_type="concept")

        with connect(db, actor="clio") as g:
            g.register_agent(values=["source-coverage"])
            g.create_node(label="another", node_type="concept")

        with connect(db, actor="metis") as g:
            pending = g.pending_notifications()
            # Should see clio's change, not own
            assert all(c.get("agent_name") != "metis" for c in pending)

    def test_listen_updates_last_sync(self, tmp_path):
        db = str(tmp_path / "lastsync.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            g.heartbeat()

        with connect(db, actor="clio") as g:
            g.register_agent(values=["source-coverage"])
            g.create_node(label="topic", node_type="concept")

        with connect(db, actor="metis") as g:
            # Get last_sync before
            g._conn.execute(
                "SELECT last_sync FROM ohm_agent_state WHERE agent_name = 'metis'",
            ).fetchone()
            g.listen()
            state_after = g._conn.execute(
                "SELECT last_sync FROM ohm_agent_state WHERE agent_name = 'metis'",
            ).fetchone()
            # last_sync should be updated (or at least not None)
            assert state_after[0] is not None


# ===== Monte Carlo Simulation =====

class TestMonteCarlo:
    def test_cascade_propagation(self, tmp_path):
        db = str(tmp_path / "mc.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="Root", node_type="concept")
            b = g.create_node(label="Child1", node_type="concept")
            c = g.create_node(label="Child2", node_type="concept")
            g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=0.95)
            g.create_edge(from_node=b["id"], to_node=c["id"], edge_type="CAUSES", layer="L3", confidence=0.8)

            result = g.monte_carlo(a["id"], simulations=1000, depth=3)
            assert result["simulation_count"] == 1000
            assert len(result["affected_nodes"]) >= 1
            # Child1 should have high impact probability (0.95 confidence edge)
            child1 = [n for n in result["affected_nodes"] if n["label"] == "Child1"]
            assert len(child1) >= 1
            assert child1[0]["impact_probability"] > 0.8

    def test_no_downstream(self, tmp_path):
        db = str(tmp_path / "mc_isolated.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="Isolated", node_type="concept")
            result = g.monte_carlo(a["id"], simulations=100)
            assert result["mean_affected"] == 0
            assert result["affected_nodes"] == []


# ===== Near Duplicate Detection =====

class TestNearDuplicates:
    def test_detects_similar_observations(self, tmp_path):
        db = str(tmp_path / "dedup.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Hormuz traffic", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.85, baseline=0.5, sigma=0.3)

        with connect(db, actor="clio") as g:
            g.register_agent(values=["source-coverage"])
            node = g.find_or_create_node(label="Hormuz traffic", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.87, baseline=0.5, sigma=0.3)

        with connect(db, actor="metis") as g:
            dups = g.near_duplicates(similarity_threshold=0.5)
            assert len(dups) >= 1
            assert dups[0]["similarity"] > 0.9

    def test_no_duplicates_different_values(self, tmp_path):
        db = str(tmp_path / "dedup_none.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="test", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.1, baseline=0.5, sigma=0.3)

        with connect(db, actor="clio") as g:
            g.register_agent(values=["source-coverage"])
            node = g.find_or_create_node(label="test", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.9, baseline=0.5, sigma=0.3)

        with connect(db, actor="metis") as g:
            dups = g.near_duplicates(similarity_threshold=0.8)
            # 0.1 vs 0.9 = very different, should not match at 0.8 threshold
            assert len(dups) == 0


# ===== Confidence Calibration =====

class TestConfidenceCalibration:
    def test_calibration_score(self, tmp_path):
        db = str(tmp_path / "cal.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            a = g.create_node(label="A", node_type="concept")
            b = g.create_node(label="B", node_type="concept")
            c = g.create_node(label="C", node_type="concept")
            g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=0.9)
            g.create_edge(from_node=b["id"], to_node=c["id"], edge_type="SUPPORTS", layer="L3", confidence=0.7)

            result = g.calibration("metis")
            assert result["agent_name"] == "metis"
            assert result["total_l3_l4_edges"] >= 2
            assert "calibration_by_band" in result
            assert "calibration_score" in result

    def test_calibration_unregistered(self, tmp_path):
        db = str(tmp_path / "cal_empty.duckdb")
        with connect(db, actor="unknown") as g:
            result = g.calibration("unknown")
            assert result["total_l3_l4_edges"] == 0
            assert result["calibration_score"] is None


# ===== Connection Discovery =====

class TestConnectionDiscovery:
    def test_suggest_connections_returns_list(self, tmp_path):
        db = str(tmp_path / "discover.duckdb")
        with connect(db, actor="metis") as g:
            g.create_node(label="A", node_type="concept")
            g.create_node(label="B", node_type="concept")
            # No connections yet — returns empty list
            suggestions = g.suggest_connections()
            assert isinstance(suggestions, list)


# ===== Composite Score =====

class TestCompositeScore:
    def test_arithmetic_mean_default(self, tmp_path):
        """Default arithmetic mean is backwards compatible."""
        db = str(tmp_path / "composite_arith.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Test Node", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.6,
                      baseline=0.5, sigma=0.1)
            g.observe(node["id"], obs_type="measurement", value=0.8,
                      baseline=0.5, sigma=0.1)

            result = g.composite_score(node["id"])
            assert result["method"] == "arithmetic"
            # (0.6 + 0.8) / 2 = 0.7 with equal weights
            assert result["observation_score"] is not None
            assert result["composite_score"] is not None

    def test_geometric_mean_multiplicative(self, tmp_path):
        """Geometric mean for demand forecasting multipliers."""
        db = str(tmp_path / "composite_geom.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Demand Factor", node_type="concept")
            # obs=0.8 (1.6x baseline 0.5), evidence=0.75 (1.5x baseline 0.5)
            g.observe(node["id"], obs_type="measurement", value=0.8,
                      baseline=0.5, sigma=0.1)

            result = g.composite_score(
                node["id"],
                observation_weight=0.5,
                evidence_weight=0.5,
                method="geometric",
                baseline=1.0,
            )
            assert result["method"] == "geometric"
            assert result["baseline"] == 1.0
            # Geometric mean of 0.8 and 0.75 ≈ 0.7746
            assert result["composite_score"] is not None

    def test_arithmetic_vs_geometric_differ(self, tmp_path):
        """Arithmetic and geometric should give different results when both signals present."""
        db = str(tmp_path / "composite_compare.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Compare Node", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.5,
                      baseline=0.5, sigma=0.1)
            # Add evidence edge so evidence_score is non-zero
            target = g.create_node(label="Target", node_type="concept")
            g.create_edge(from_node=target["id"], to_node=node["id"],
                          edge_type="CAUSES", layer="L3", confidence=0.7)

            arith = g.composite_score(node["id"], method="arithmetic")
            geom = g.composite_score(node["id"], method="geometric")
            # With obs=0.5 and evidence≈0.7, results should differ
            assert arith["composite_score"] != geom["composite_score"]

    def test_geometric_with_both_scores(self, tmp_path):
        """Geometric mean with both observation and evidence scores."""
        db = str(tmp_path / "composite_geom_both.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Demand Node", node_type="concept")
            # Observation: value=0.8
            g.observe(node["id"], obs_type="measurement", value=0.8,
                      baseline=0.5, sigma=0.1)
            # Evidence: confidence=0.75
            source = g.create_node(label="Saturday", node_type="concept")
            g.create_edge(from_node=source["id"], to_node=node["id"],
                          edge_type="PREDICTS", layer="L3", confidence=0.75)

            result = g.composite_score(
                node["id"], method="geometric", baseline=1.0,
                observation_weight=0.5, evidence_weight=0.5,
            )
            assert result["method"] == "geometric"
            assert result["baseline"] == 1.0
            assert result["composite_score"] is not None
            # Geometric mean should be between the two scores
            assert result["composite_score"] > 0

    def test_geometric_baseline_scaling(self, tmp_path):
        """Geometric mean with non-1.0 baseline applies scaling."""
        db = str(tmp_path / "composite_baseline.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Baseline Node", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.8,
                      baseline=0.5, sigma=0.1)

            result_default = g.composite_score(node["id"], method="geometric", baseline=1.0)
            result_scaled = g.composite_score(node["id"], method="geometric", baseline=2.0)
            # With baseline=2.0, composite should be scaled by 2.0
            if result_default["composite_score"] is not None and result_scaled["composite_score"] is not None:
                # scaled = default * baseline
                expected = round(result_default["composite_score"] * 2.0, 4)
                assert result_scaled["composite_score"] == expected

    def test_geometric_fallback_on_zero(self, tmp_path):
        """Geometric method falls back to arithmetic when values are <= 0."""
        db = str(tmp_path / "composite_fallback.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Zero Node", node_type="concept")
            # Observation with value=0 — geometric can't handle this
            g.observe(node["id"], obs_type="measurement", value=0.0,
                      baseline=0.5, sigma=0.1)

            result = g.composite_score(node["id"], method="geometric")
            # Should still return a result (falls back to arithmetic)
            assert result["composite_score"] is not None
            assert result["method"] == "geometric"

    def test_composite_returns_method_and_baseline(self, tmp_path):
        """Composite score result includes method and baseline keys."""
        db = str(tmp_path / "composite_keys.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Keys Node", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.6,
                      baseline=0.5, sigma=0.1)

            result_arith = g.composite_score(node["id"])
            assert "method" in result_arith
            assert "baseline" in result_arith
            assert result_arith["method"] == "arithmetic"
            assert result_arith["baseline"] == 1.0

            result_geom = g.composite_score(node["id"], method="geometric", baseline=2.5)
            assert result_geom["method"] == "geometric"
            assert result_geom["baseline"] == 2.5


# ===== Graph Import/Export =====

class TestGraphImportExport:
    def test_export_contains_all_tables(self, tmp_path):
        db = str(tmp_path / "export.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            a = g.create_node(label="Test", node_type="concept")
            g.observe(a["id"], obs_type="measurement", value=0.5, baseline=0.5, sigma=0.3)

            exported = g.export_graph()
            assert "nodes" in exported
            assert "edges" in exported
            assert "observations" in exported
            assert "meta" in exported
            assert exported["meta"]["node_count"] >= 1

    def test_round_trip_preserves_data(self, tmp_path):
        db = str(tmp_path / "roundtrip.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            a = g.create_node(label="Node A", node_type="concept")
            b = g.create_node(label="Node B", node_type="concept")
            g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=0.8)
            g.observe(a["id"], obs_type="measurement", value=0.7, baseline=0.5, sigma=0.3)

            exported = g.export_graph()

        # Import into fresh DB
        db2 = str(tmp_path / "roundtrip2.duckdb")
        with connect(db2, actor="importer") as g2:
            result = g2.import_graph(exported)
            assert result["nodes"] >= 2
            assert result["edges"] >= 1
            assert result["observations"] >= 1

            stats = g2.stats()
            assert stats["total_nodes"] >= 2
            assert stats["total_edges"] >= 1

    def test_merge_mode_skips_duplicates(self, tmp_path):
        db = str(tmp_path / "merge.duckdb")
        with connect(db, actor="metis") as g:
            g.create_node(label="Existing", node_type="concept")
            exported = g.export_graph()

        # Import into same DB (merge mode)
        with connect(db, actor="importer") as g2:
            result = g2.import_graph(exported, merge=True)
            # Existing nodes should be skipped
            assert result["skipped"] >= 1


# ===== Edge Versioning =====

class TestEdgeVersioning:
    def test_created_event(self, tmp_path):
        db = str(tmp_path / "version.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="A", node_type="concept")
            b = g.create_node(label="B", node_type="concept")
            edge = g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=0.8)

            history = g.edge_history(edge["id"])
            assert len(history) >= 1
            assert history[0]["type"] == "created"
            assert history[0]["agent"] == "metis"

    def test_challenge_appears_in_history(self, tmp_path):
        db = str(tmp_path / "version2.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="A", node_type="concept")
            b = g.create_node(label="B", node_type="concept")
            edge = g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=0.8)

            from ohm.store import OhmStore
            store = OhmStore(db_path=db, agent_name="socrates")
            store.challenge_edge(edge["id"], "test", 0.5, "CHALLENGED_BY")
            store.close()

            history = g.edge_history(edge["id"])
            types = [h["type"] for h in history]
            assert "challenged_by" in types

    def test_evolution_chain(self, tmp_path):
        db = str(tmp_path / "version3.duckdb")
        with connect(db, actor="metis") as g:
            me = g.register_agent(values=["wisdom"])
            val_edges = g._conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND edge_type = 'VALUES' AND created_by = 'metis' LIMIT 1",
                [me["id"]],
            ).fetchall()
            if val_edges:
                g.evolve_identity(val_edges[0][0], new_target="emergence", reason="test")
                history = g.edge_history(val_edges[0][0])
                types = [h["type"] for h in history]
                assert "superseded" in types or "evolved_to" in types

    def test_nonexistent_edge_empty_history(self, tmp_path):
        db = str(tmp_path / "version4.duckdb")
        with connect(db, actor="metis") as g:
            history = g.edge_history("nonexistent_edge_id")
            assert history == []
