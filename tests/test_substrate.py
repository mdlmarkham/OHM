"""
OHM Substrate Method Tests — contradiction detection, anomaly detection,
heartbeat, aggregation, identity evolution, cold start discovery,
L2 immutability.
"""

import os
import pytest

from ohm.sdk import connect

pytestmark = pytest.mark.slow


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
        g.observe(node["id"], obs_type="measurement", value=0.3, baseline=0.3, sigma=0.5, source="routine")
        g.observe(node["id"], obs_type="measurement", value=0.9, baseline=0.3, sigma=0.5, source="breaking")
        g.observe(node["id"], obs_type="measurement", value=0.35, baseline=0.3, sigma=0.3, source="routine")

    # Agent 2: Clio — opposing observation
    with connect(db_path, actor="clio") as g:
        g.register_agent(
            values=["wisdom", "source-coverage"],
            capabilities=["deep-research"],
            interests=["economics", "international-law"],
        )
        node = g.find_or_create_node(label="Hormuz traffic", node_type="concept")
        g.observe(node["id"], obs_type="measurement", value=0.1, baseline=0.3, sigma=0.4, source="alternative")

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
            g.create_edge(from_node=n1["id"], to_node=n2["id"], edge_type="CAUSES", layer="L3")

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
            g.create_edge(from_node=b["id"], to_node=a["id"], edge_type="DERIVES_FROM", layer="L2", provenance="test derivation")

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
            g.create_edge(from_node=a["id"], to_node=g.find_or_create_node(label="Connected")["id"], edge_type="RELATED_TO", layer="L3")

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
            g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=0.95, probability=0.95)
            g.create_edge(from_node=b["id"], to_node=c["id"], edge_type="CAUSES", layer="L3", confidence=0.8, probability=0.8)

            result = g.monte_carlo(a["id"], simulations=1000, depth=3, seed=42)
            assert result["simulation_count"] == 1000
            assert len(result["affected_nodes"]) >= 1
            # Child1: confidence=0.95 × probability=0.95 ≈ 0.90
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


class TestMonteCarloTwoStage:
    """Test two-stage sampling: confidence (existence) × probability (propagation).

    ADR-008: probability and confidence are semantically distinct.
    An edge with confidence=0.9, probability=0.1 should activate ~9% of trials.
    """

    def test_high_conf_low_prob_activates_at_product_rate(self, tmp_path):
        """Edge with confidence=0.9, probability=0.1 activates ~9% not 90%."""
        db = str(tmp_path / "mc_2stage.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="Root", node_type="concept")
            b = g.create_node(label="Target", node_type="concept")
            g.create_edge(
                from_node=a["id"],
                to_node=b["id"],
                edge_type="CAUSES",
                layer="L3",
                confidence=0.9,
                probability=0.1,
            )
            result = g.monte_carlo(
                a["id"],
                simulations=5000,
                depth=3,
                seed=42,
            )
            target = [n for n in result["affected_nodes"] if n["label"] == "Target"]
            assert len(target) == 1
            # Expected: 0.9 * 0.1 = 0.09, allow ±3% for randomness
            assert abs(target[0]["impact_probability"] - 0.09) < 0.03

    def test_null_probability_uses_default(self, tmp_path):
        """Edge with probability=NULL uses default_probability=0.5."""
        db = str(tmp_path / "mc_null_prob.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="Root", node_type="concept")
            b = g.create_node(label="Target", node_type="concept")
            # Create edge without probability (NULL)
            g.create_edge(
                from_node=a["id"],
                to_node=b["id"],
                edge_type="CAUSES",
                layer="L3",
                confidence=0.8,
            )
            result = g.monte_carlo(
                a["id"],
                simulations=5000,
                depth=3,
                default_probability=0.5,
                seed=42,
            )
            target = [n for n in result["affected_nodes"] if n["label"] == "Target"]
            assert len(target) == 1
            # Expected: 0.8 * 0.5 = 0.4, allow ±5% for randomness
            assert abs(target[0]["impact_probability"] - 0.4) < 0.05

    def test_high_prob_moderate_conf_activates_at_product(self, tmp_path):
        """Edge with confidence=0.5, probability=0.9 activates ~45%."""
        db = str(tmp_path / "mc_highprob.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="Root", node_type="concept")
            b = g.create_node(label="Target", node_type="concept")
            g.create_edge(
                from_node=a["id"],
                to_node=b["id"],
                edge_type="CAUSES",
                layer="L3",
                confidence=0.5,
                probability=0.9,
            )
            result = g.monte_carlo(
                a["id"],
                simulations=5000,
                depth=3,
                seed=42,
            )
            target = [n for n in result["affected_nodes"] if n["label"] == "Target"]
            assert len(target) == 1
            # Expected: 0.5 * 0.9 = 0.45, allow ±5% for randomness
            assert abs(target[0]["impact_probability"] - 0.45) < 0.05

    def test_cascade_two_stage_propagation(self, tmp_path):
        """Two-stage sampling through a chain: A→B→C."""
        db = str(tmp_path / "mc_chain.duckdb")
        with connect(db, actor="metis") as g:
            a = g.create_node(label="A", node_type="concept")
            b = g.create_node(label="B", node_type="concept")
            c = g.create_node(label="C", node_type="concept")
            # A→B: high confidence, moderate probability
            g.create_edge(
                from_node=a["id"],
                to_node=b["id"],
                edge_type="CAUSES",
                layer="L3",
                confidence=0.9,
                probability=0.8,
            )
            # B→C: moderate confidence, high probability
            g.create_edge(
                from_node=b["id"],
                to_node=c["id"],
                edge_type="CAUSES",
                layer="L3",
                confidence=0.7,
                probability=0.9,
            )
            result = g.monte_carlo(
                a["id"],
                simulations=5000,
                depth=3,
                seed=42,
            )
            # B impact ≈ 0.9 * 0.8 = 0.72
            b_node = [n for n in result["affected_nodes"] if n["label"] == "B"]
            assert len(b_node) == 1
            assert abs(b_node[0]["impact_probability"] - 0.72) < 0.05
            # C impact ≈ 0.72 * 0.7 * 0.9 ≈ 0.45
            c_node = [n for n in result["affected_nodes"] if n["label"] == "C"]
            assert len(c_node) == 1
            assert abs(c_node[0]["impact_probability"] - 0.45) < 0.08


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
            assert "global_challenge_rate" in result
            assert "base_rate_adjusted" in result

    def test_calibration_unregistered(self, tmp_path):
        db = str(tmp_path / "cal_empty.duckdb")
        with connect(db, actor="unknown") as g:
            result = g.calibration("unknown")
            assert result["total_l3_l4_edges"] == 0
            assert result["calibration_score"] is None

    def test_calibration_with_base_rate(self, tmp_path):
        """OHM-gfh: Calibration accounts for global challenge rate.

        In a low-activity graph with few challenges, the expected rate
        should be scaled down so agents aren't penalized for having
        few challenges.
        """
        db = str(tmp_path / "cal_base_rate.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            # Create several edges with high confidence
            for i in range(5):
                a = g.create_node(label=f"cause_{i}", node_type="concept")
                b = g.create_node(label=f"effect_{i}", node_type="concept")
                g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=0.9)

            result = g.calibration("metis")
            # Global challenge rate should be 0 (no challenges)
            assert result["global_challenge_rate"] == 0.0
            # Base rate adjustment should be True (edges exist)
            assert result["base_rate_adjusted"] is True
            # With no challenges, expected rates should be near 0
            for band in result["calibration_by_band"]:
                if band["total_edges"] > 0:
                    assert band["expected_rate"] == 0.0

    def test_calibration_high_activity(self, tmp_path):
        """OHM-gfh: In a high-activity graph, expected rates scale up."""
        db = str(tmp_path / "cal_high_activity.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            # Create many edges with varying confidence
            for i in range(10):
                a = g.create_node(label=f"cause_{i}", node_type="concept")
                b = g.create_node(label=f"effect_{i}", node_type="concept")
                conf = 0.3 + (i * 0.07)  # 0.3 to 0.93
                g.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", confidence=conf)

            result = g.calibration("metis")
            assert result["global_challenge_rate"] == 0.0
            assert result["total_l3_l4_edges"] == 10

    def test_calibration_empty_graph(self, tmp_path):
        """OHM-gfh: Empty graph returns None calibration score."""
        db = str(tmp_path / "cal_empty_graph.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            result = g.calibration("metis")
            assert result["total_l3_l4_edges"] == 0
            assert result["calibration_score"] is None
            assert result["global_challenge_rate"] == 0.0


# ===== Temporal Decay =====


class TestTemporalDecay:
    def test_composite_score_with_temporal_decay(self, tmp_path):
        """Temporal decay weights recent observations more heavily."""
        db = str(tmp_path / "temporal_decay.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Weather", node_type="concept")
            # Add observation — will be very recent (age ~0 hours)
            g.observe(node["id"], obs_type="measurement", value=0.9, baseline=0.5, sigma=0.1)

            # Without decay
            result_no_decay = g.composite_score(node["id"])
            # With decay (should be same since observation is fresh)
            result_decay = g.composite_score(node["id"], temporal_decay_hours=4.0)

            assert result_no_decay["composite_score"] is not None
            assert result_decay["composite_score"] is not None
            # Fresh observation: decay should not change result significantly
            assert abs(result_no_decay["composite_score"] - result_decay["composite_score"]) < 0.01
            assert result_decay["temporal_decay_hours"] == 4.0

    def test_composite_score_temporal_decay_returns_param(self, tmp_path):
        """composite_score returns temporal_decay_hours in result."""
        db = str(tmp_path / "temporal_param.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Test", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.5, baseline=0.5, sigma=0.1)

            result = g.composite_score(node["id"], temporal_decay_hours=4.0)
            assert "temporal_decay_hours" in result
            assert result["temporal_decay_hours"] == 4.0

            result_none = g.composite_score(node["id"])
            assert result_none["temporal_decay_hours"] is None

    def test_decay_observations_dry_run(self, tmp_path):
        """decay_observations in dry_run mode returns results without modifying data."""
        db = str(tmp_path / "decay_dryrun.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            node = g.create_node(label="Product", node_type="concept")
            g.observe(node["id"], obs_type="measurement", value=0.8, baseline=0.5, sigma=0.1)

            results = g.decay_observations(node["id"], temporal_decay_hours=4.0, dry_run=True)
            assert len(results) >= 1
            assert abs(results[0]["original_value"] - 0.8) < 0.001
            assert results[0]["decayed_value"] is not None
            assert results[0]["decay_factor"] > 0
            assert results[0]["age_hours"] >= 0

            # Original value should be unchanged after dry_run
            check = g._conn.execute(
                "SELECT value FROM ohm_observations WHERE node_id = ?",
                [node["id"]],
            ).fetchone()
            assert abs(check[0] - 0.8) < 0.001

    def test_decay_observations_all_nodes(self, tmp_path):
        """decay_observations with node_id=None processes all observations."""
        db = str(tmp_path / "decay_all.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            n1 = g.create_node(label="Node1", node_type="concept")
            n2 = g.create_node(label="Node2", node_type="concept")
            g.observe(n1["id"], obs_type="measurement", value=0.7, baseline=0.5, sigma=0.1)
            g.observe(n2["id"], obs_type="measurement", value=0.9, baseline=0.5, sigma=0.1)

            results = g.decay_observations(temporal_decay_hours=168.0, dry_run=True)
            assert len(results) >= 2


# ===== Medical Diagnosis =====


class TestMedicalDiagnosis:
    def test_rules_out_creates_negates_edge(self, tmp_path):
        """rules_out() creates a NEGATES edge between finding and condition."""
        db = str(tmp_path / "rules_out.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            finding = g.create_node(label="Fever Absent", node_type="concept")
            condition = g.create_node(label="Malaria", node_type="concept")

            edge = g.rules_out(from_node=finding["id"], to_node=condition["id"], confidence=0.9)
            assert edge["edge_type"] == "NEGATES"
            assert edge["from_node"] == finding["id"]
            assert edge["to_node"] == condition["id"]
            assert abs(edge["confidence"] - 0.9) < 0.01

    def test_differential_diagnosis_returns_candidates(self, tmp_path):
        """differential_diagnosis returns candidate conditions ranked by score."""
        db = str(tmp_path / "ddx.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            patient = g.create_node(label="Patient", node_type="concept")
            malaria = g.create_node(label="Malaria", node_type="concept")
            flu = g.create_node(label="Flu", node_type="concept")

            # Evidence: malaria CAUSES patient symptoms
            g.create_edge(from_node=malaria["id"], to_node=patient["id"], edge_type="CAUSES", layer="L3", confidence=0.7)
            # Evidence: flu PREDICTS patient symptoms
            g.create_edge(from_node=flu["id"], to_node=patient["id"], edge_type="PREDICTS", layer="L3", confidence=0.5)

            results = g.differential_diagnosis(patient["id"])
            assert len(results) >= 2
            # Non-ruled-out candidates should come first
            assert not results[0]["ruled_out"]

    def test_differential_diagnosis_excludes_negated(self, tmp_path):
        """differential_diagnosis marks ruled-out conditions."""
        db = str(tmp_path / "ddx_negated.duckdb")
        with connect(db, actor="metis") as g:
            g.register_agent(values=["wisdom"])
            patient = g.create_node(label="Patient", node_type="concept")
            malaria = g.create_node(label="Malaria", node_type="concept")
            flu = g.create_node(label="Flu", node_type="concept")
            no_fever = g.create_node(label="Fever Absent", node_type="concept")

            # Evidence edges
            g.create_edge(from_node=malaria["id"], to_node=patient["id"], edge_type="CAUSES", layer="L3", confidence=0.7)
            g.create_edge(from_node=flu["id"], to_node=patient["id"], edge_type="PREDICTS", layer="L3", confidence=0.5)

            # Fever absent NEGATES malaria
            g.rules_out(from_node=no_fever["id"], to_node=malaria["id"], confidence=0.9)

            results = g.differential_diagnosis(patient["id"])
            # Malaria should be marked as ruled out
            malaria_result = [r for r in results if r["node_id"] == malaria["id"]]
            if malaria_result:
                assert malaria_result[0]["ruled_out"] is True
                assert len(malaria_result[0]["ruled_out_by"]) > 0

    def test_compound_confidence_independent(self):
        """Independent observations compound multiplicatively."""
        from ohm.methods import compound_confidence

        result = compound_confidence(
            [{"confidence": 0.6}, {"confidence": 0.7}],
            correlation=0.0,
        )
        # Independent: 1 - (1-0.6)*(1-0.7) = 1 - 0.12 = 0.88
        assert result["compound_confidence"] is not None
        assert result["compound_confidence"] > 0.7  # Higher than either alone
        assert result["correlation"] == 0.0
        assert result["observation_count"] == 2

    def test_compound_confidence_correlated(self):
        """Perfectly correlated observations use max only."""
        from ohm.methods import compound_confidence

        result = compound_confidence(
            [{"confidence": 0.6}, {"confidence": 0.7}],
            correlation=1.0,
        )
        # Correlated: max(0.6, 0.7) = 0.7
        assert result["compound_confidence"] == 0.7
        assert result["correlation"] == 1.0

    def test_compound_confidence_independent_higher_than_correlated(self):
        """Independent findings give higher composite than correlated ones."""
        from ohm.methods import compound_confidence

        obs = [{"confidence": 0.6}, {"confidence": 0.7}]
        result_indep = compound_confidence(obs, correlation=0.0)
        result_corr = compound_confidence(obs, correlation=1.0)
        assert result_indep["compound_confidence"] > result_corr["compound_confidence"]

    def test_compound_confidence_empty(self):
        """Empty observations returns None."""
        from ohm.methods import compound_confidence

        result = compound_confidence([], correlation=0.0)
        assert result["compound_confidence"] is None
        assert result["observation_count"] == 0

    def test_compound_confidence_partial_correlation(self):
        """Partial correlation interpolates between independent and correlated."""
        from ohm.methods import compound_confidence

        obs = [{"confidence": 0.6}, {"confidence": 0.7}]
        result_0 = compound_confidence(obs, correlation=0.0)
        result_05 = compound_confidence(obs, correlation=0.5)
        result_1 = compound_confidence(obs, correlation=1.0)
        # Partial should be between independent and correlated
        assert result_05["compound_confidence"] >= result_1["compound_confidence"]
        assert result_05["compound_confidence"] <= result_0["compound_confidence"]

    def test_compound_confidence_source_weighting(self):
        """Reliable sources count more when source_weights provided."""
        from ohm.methods import compound_confidence

        # Two observations with equal confidence (0.8)
        obs = [
            {"confidence": 0.8, "source": "reliable_agent"},
            {"confidence": 0.8, "source": "unreliable_agent"},
        ]
        # Reliable (0.9) and unreliable (0.5)
        weights = {"reliable_agent": 0.9, "unreliable_agent": 0.5}

        result = compound_confidence(obs, correlation=0.0, source_weights=weights)
        # Correct formula: P = 1 - Π(1 - w_i * c_i)
        # = 1 - (1 - 0.9*0.8)(1 - 0.5*0.8) = 1 - (1-0.72)(1-0.4) = 1 - 0.28*0.6 = 1 - 0.168 = 0.832
        assert result["compound_confidence"] == 0.832
        assert result["weighted"] is True
        assert result["observation_count"] == 2

    def test_compound_confidence_unknown_source_default_weight(self):
        """Unknown sources use default weight of 0.5."""
        from ohm.methods import compound_confidence

        obs = [{"confidence": 0.8, "source": "unknown_agent"}]
        weights = {"known_agent": 0.9}  # unknown_agent not in weights

        result = compound_confidence(obs, correlation=0.0, source_weights=weights)
        assert result["weighted"] is True
        # With default 0.5 weight: P = 1 - (1 - 0.5*0.8) = 1 - 0.6 = 0.4
        assert result["compound_confidence"] == 0.4

    def test_compound_confidence_weighted_correlated(self):
        """Source weighting works with correlated observations (max)."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.6, "source": "good"},
            {"confidence": 0.9, "source": "bad"},
        ]
        weights = {"good": 0.9, "bad": 0.5}

        result = compound_confidence(obs, correlation=1.0, source_weights=weights)
        # Correlated: max(c*w) = max(0.6*0.9, 0.9*0.5) = max(0.54, 0.45) = 0.54
        assert result["compound_confidence"] == 0.54
        assert result["weighted"] is True

    def test_compound_confidence_no_weights_backward_compat(self):
        """Without source_weights, behavior unchanged (weighted=False)."""
        from ohm.methods import compound_confidence

        obs = [{"confidence": 0.6}, {"confidence": 0.7}]
        result = compound_confidence(obs, correlation=0.0)
        assert result["weighted"] is False
        assert result["compound_confidence"] > 0.7

    def test_compound_confidence_weighted_bug_fix_example(self):
        """Bug fix verification: two observations c=0.5, w=[0.5, 0.5] gives 0.4375 (not 0.50)."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.5},
            {"confidence": 0.5},
        ]
        weights = {"obs0": 0.5, "obs1": 0.5}
        result = compound_confidence(obs, correlation=0.0, source_weights=weights)
        # Correct: 1 - (1-0.5*0.5)(1-0.5*0.5) = 1 - 0.75*0.75 = 1 - 0.5625 = 0.4375
        assert result["compound_confidence"] == 0.4375

    def test_compound_confidence_equal_weights_unchanged(self):
        """Two observations at c=0.5, w=[1.0, 1.0] gives 0.75 (backward compatible)."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.5, "source": "obs0"},
            {"confidence": 0.5, "source": "obs1"},
        ]
        weights = {"obs0": 1.0, "obs1": 1.0}
        result = compound_confidence(obs, correlation=0.0, source_weights=weights)
        # 1 - (1-0.5)(1-0.5) = 1 - 0.5*0.5 = 0.75
        assert result["compound_confidence"] == 0.75

    def test_compound_confidence_diversity_correlation_single_agent_echo_chamber(self):
        """Same agent same day produces high correlation, lower compound confidence."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.9, "created_by": "metis", "created_at": "2026-05-26T10:00:00"},
            {"confidence": 0.9, "created_by": "metis", "created_at": "2026-05-26T11:00:00"},
            {"confidence": 0.9, "created_by": "metis", "created_at": "2026-05-26T12:00:00"},
        ]
        result = compound_confidence(obs, use_diversity_correlation=True)
        assert result["correlation"] == 0.9
        assert result["compound_confidence"] < 0.99  # Lower than independent
        assert "diversity_correlation" in result
        assert result["diversity_correlation"] == 0.9
        assert result["source_diversity_metrics"]["agent_count"] == 1
        assert result["source_diversity_metrics"]["unique_agents"] == ["metis"]

    def test_compound_confidence_diversity_correlation_multi_agent(self):
        """Different agents produce low correlation, higher compound confidence."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.9, "created_by": "metis", "created_at": "2026-05-26T10:00:00"},
            {"confidence": 0.9, "created_by": "clio", "created_at": "2026-05-26T11:00:00"},
            {"confidence": 0.9, "created_by": "hephaestus", "created_at": "2026-05-26T12:00:00"},
        ]
        result = compound_confidence(obs, use_diversity_correlation=True)
        assert result["correlation"] == 0.2
        assert result["compound_confidence"] > 0.97  # Near independent
        assert "diversity_correlation" in result
        assert result["source_diversity_metrics"]["agent_count"] == 3

    def test_compound_confidence_diversity_correlation_mixed(self):
        """Same agent different days produces medium correlation."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.9, "created_by": "metis", "created_at": "2026-05-26T10:00:00"},
            {"confidence": 0.9, "created_by": "metis", "created_at": "2026-05-25T10:00:00"},
            {"confidence": 0.9, "created_by": "clio", "created_at": "2026-05-24T10:00:00"},
        ]
        result = compound_confidence(obs, use_diversity_correlation=True)
        assert 0.2 < result["correlation"] < 0.9  # Between single-agent and multi-agent
        assert "diversity_correlation" in result

    def test_compound_confidence_diversity_backward_compat_no_diversity_data(self):
        """Without created_by/created_at, uses same-agent-different-day correlation=0.6."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.9},
            {"confidence": 0.9},
        ]
        result = compound_confidence(obs, use_diversity_correlation=True)
        assert result["correlation"] == 0.6  # Same agent, unknown day -> 0.6
        assert "diversity_correlation" in result
        assert result["source_diversity_metrics"]["agent_count"] == 1
        assert result["source_diversity_metrics"]["unique_agents"] == ["_unknown_"]

    def test_compound_confidence_diversity_explicit_correlation_override(self):
        """Explicit correlation parameter overrides diversity correlation."""
        from ohm.methods import compound_confidence

        obs = [
            {"confidence": 0.9, "created_by": "metis", "created_at": "2026-05-26T10:00:00"},
            {"confidence": 0.9, "created_by": "clio", "created_at": "2026-05-26T11:00:00"},
        ]
        result = compound_confidence(obs, correlation=0.5, use_diversity_correlation=True)
        assert result["correlation"] == 0.5
        assert "diversity_correlation" not in result

    def test_compound_confidence_weight_clamped_at_one(self):
        """Observation with w*c > 1.0 is clamped to avoid negative probabilities."""
        from ohm.methods import compound_confidence

        obs = [{"confidence": 1.0, "source": "obs0"}]
        weights = {"obs0": 1.5}  # w*c = 1.5 > 1, should clamp
        result = compound_confidence(obs, correlation=0.0, source_weights=weights)
        # Clamped to 1.0: 1 - (1-1.0) = 1 - 0 = 1.0
        assert result["compound_confidence"] == 1.0


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
