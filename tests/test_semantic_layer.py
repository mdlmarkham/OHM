"""Tests for the OHM semantic-layer module.

Covers:
- YAML metric loading
- SQL execution against in-memory DuckDB
- HTTP endpoint integration
- Metric correctness on a small constructed graph
- Metrics → Actions threshold evaluation
"""

from __future__ import annotations

import pytest

from ohm.semantic_layer import (
    evaluate_thresholds,
    list_metrics,
    load_metrics,
    run_metrics,
    run_metrics_and_actions,
)
from ohm.semantic_layer.actions import (
    _is_rate_limited,
    _parse_threshold,
    _record_action,
    create_beads_task,
    run_actions,
)
from ohm.server.server import DEFAULT_CONFIG
from ohm.queries import create_node, create_edge, create_challenge, query_record_outcome


class TestSemanticLayerMetrics:
    """Unit tests for the semantic-layer engine."""

    def test_load_metrics_returns_three_definitions(self):
        metrics = load_metrics()
        assert set(metrics) == {"verification_rate", "challenge_ratio", "source_reliability_avg"}
        for name, definition in metrics.items():
            assert "sql" in definition
            assert definition["sql"].strip().upper().startswith("SELECT")
            assert "thresholds" in definition
            assert isinstance(definition["thresholds"], list)

    def test_list_metrics_has_descriptions(self):
        descriptions = list_metrics()
        assert set(descriptions) == {"verification_rate", "challenge_ratio", "source_reliability_avg"}
        assert all(isinstance(d, str) for d in descriptions.values())

    def test_run_metrics_on_empty_graph(self, test_db):
        values = run_metrics(test_db, use_ibis=False)
        assert values["verification_rate"] is None
        assert values["challenge_ratio"] is None
        assert values["source_reliability_avg"] is None

    def test_verification_rate_and_challenge_ratio(self, test_db):
        # Three concepts
        a = create_node(test_db, label="A", node_type="concept", created_by="test_agent")
        b = create_node(test_db, label="B", node_type="concept", created_by="test_agent")
        c = create_node(test_db, label="C", node_type="concept", created_by="test_agent")
        d = create_node(test_db, label="D", node_type="concept", created_by="test_agent")

        # Two causal L3 edges; sign one to mark it "verified"
        e1 = create_edge(
            test_db,
            from_node=a["id"],
            to_node=b["id"],
            layer="L3",
            edge_type="CAUSES",
            created_by="test_agent",
        )
        e2 = create_edge(
            test_db,
            from_node=b["id"],
            to_node=c["id"],
            layer="L3",
            edge_type="PREDICTS",
            created_by="test_agent",
        )
        # Non-causal L3 edge: counted in challenge-ratio denominator only
        create_edge(
            test_db,
            from_node=c["id"],
            to_node=d["id"],
            layer="L3",
            edge_type="SUPPORTS",
            created_by="test_agent",
        )

        # Sign one causal edge -> write_signature not NULL
        test_db.execute(
            "UPDATE ohm_edges SET write_signature = 'sig1' WHERE id = ?",
            [e1["id"]],
        )

        # Add a CHALLENGED_BY edge against e2 (challenge = 1, total L3 = 4)
        create_challenge(
            test_db,
            edge_id=e2["id"],
            reason="counter-evidence",
            created_by="critic_agent",
            confidence=0.4,
        )

        values = run_metrics(test_db, use_ibis=False)
        assert values["verification_rate"] == pytest.approx(1 / 2)
        assert values["challenge_ratio"] == pytest.approx(1 / 4)

    def test_source_reliability_avg(self, test_db):
        claim = create_node(test_db, label="Claim", node_type="concept", created_by="test_agent")

        query_record_outcome(
            test_db,
            source_agent="source_agent_1",
            claim_node=claim["id"],
            outcome=True,
            recorded_by="test_agent",
        )
        query_record_outcome(
            test_db,
            source_agent="source_agent_1",
            claim_node=claim["id"],
            outcome=False,
            recorded_by="test_agent",
        )
        query_record_outcome(
            test_db,
            source_agent="source_agent_2",
            claim_node=claim["id"],
            outcome=True,
            recorded_by="test_agent",
        )

        values = run_metrics(test_db, use_ibis=False)
        # (1 + 0 + 1) / 3 = 2/3
        assert values["source_reliability_avg"] == pytest.approx(2 / 3)


class TestSemanticLayerThresholds:
    """Unit tests for threshold parsing and action evaluation."""

    def test_parse_threshold_variants(self):
        assert _parse_threshold("< 0.3") == ("<", 0.3)
        assert _parse_threshold("<=0.5") == ("<=", 0.5)
        assert _parse_threshold("> 0.8") == (">", 0.8)
        assert _parse_threshold(">=0.6") == (">=", 0.6)

    def test_evaluate_thresholds_fires_when_values_low(self):
        definitions = {
            "verification_rate": {
                "description": "verify",
                "sql": "SELECT 1",
                "thresholds": [
                    {
                        "when": "< 0.3",
                        "action": "create_task",
                        "title": "Run more verification experiments",
                        "priority": "P1",
                    }
                ],
            },
            "challenge_ratio": {
                "description": "challenge",
                "sql": "SELECT 1",
                "thresholds": [
                    {
                        "when": "< 0.05",
                        "action": "prompt_agent",
                        "skill": "critique",
                        "target": "high_confidence_l3_edges",
                    }
                ],
            },
            "source_reliability_avg": {
                "description": "reliability",
                "sql": "SELECT 1",
                "thresholds": [
                    {
                        "when": "< 0.6",
                        "action": "create_task",
                        "title": "Review low-reliability sources",
                        "priority": "P2",
                    }
                ],
            },
        }
        values = {
            "verification_rate": 0.2,
            "challenge_ratio": 0.01,
            "source_reliability_avg": 0.55,
        }
        actions = evaluate_thresholds(values, definitions)
        assert len(actions) == 3

        by_metric = {a["metric"]: a for a in actions}
        assert by_metric["verification_rate"]["action"] == "create_task"
        assert by_metric["verification_rate"]["priority"] == "P1"
        assert by_metric["challenge_ratio"]["action"] == "prompt_agent"
        assert by_metric["challenge_ratio"]["skill"] == "critique"
        assert by_metric["source_reliability_avg"]["priority"] == "P2"

    def test_evaluate_thresholds_empty_when_values_high(self):
        definitions = {
            "verification_rate": {
                "description": "verify",
                "sql": "SELECT 1",
                "thresholds": [{"when": "< 0.3", "action": "create_task", "title": "X", "priority": "P1"}],
            }
        }
        actions = evaluate_thresholds({"verification_rate": 0.9}, definitions)
        assert actions == []

    def test_evaluate_thresholds_handles_none_values(self):
        definitions = {
            "verification_rate": {
                "description": "verify",
                "sql": "SELECT 1",
                "thresholds": [{"when": "< 0.3", "action": "create_task", "title": "X", "priority": "P1"}],
            }
        }
        actions = evaluate_thresholds({"verification_rate": None}, definitions)
        assert actions == []


class TestSemanticLayerActions:
    """Tests for the action executor."""

    def test_create_beads_task_creates_issue(self, tmp_path):
        import subprocess

        repo = tmp_path / "test_repo"
        repo.mkdir()
        subprocess.run(["bd", "init"], cwd=str(repo), check=True, capture_output=True)

        result = create_beads_task(
            repo_path=str(repo),
            title="Test task",
            description="Test description",
            priority="P1",
            labels=["ohm", "metrics"],
        )
        assert result["title"] == "Test task"
        assert result["priority"] == "P1"
        assert "ohm" in result["labels"]
        assert result["issue_id"] is not None

    def test_run_metrics_and_actions_lists_without_execution(self, test_db):
        claim = create_node(test_db, label="Claim", node_type="concept", created_by="test_agent")
        query_record_outcome(
            test_db,
            source_agent="low_reliability",
            claim_node=claim["id"],
            outcome=False,
            recorded_by="test_agent",
        )

        result = run_metrics_and_actions(test_db, execute=False, use_ibis=False)
        assert "metrics" in result
        assert "actions" in result
        assert "executed" not in result

    def test_run_metrics_and_actions_executes_actions(self, test_db, tmp_path):
        import subprocess

        repo = tmp_path / "action_repo"
        repo.mkdir()
        subprocess.run(["bd", "init"], cwd=str(repo), check=True, capture_output=True)

        claim = create_node(test_db, label="Claim", node_type="concept", created_by="test_agent")
        query_record_outcome(
            test_db,
            source_agent="low_reliability",
            claim_node=claim["id"],
            outcome=False,
            recorded_by="test_agent",
        )

        result = run_metrics_and_actions(test_db, repo_path=str(repo), execute=True, use_ibis=False)
        assert "executed" in result
        executed = result["executed"]
        assert any(e["status"] == "created" for e in executed)


class TestSemanticLayerEndpoint:
    """HTTP integration tests for the semantic-layer endpoint."""

    def test_get_metrics_semantic_json(self, test_server):
        port, _store = test_server
        from tests.conftest import _request

        status, body = _request("GET", port, "/metrics/semantic")
        assert status == 200
        assert body["count"] == 3
        assert set(body["metrics"]) == {"verification_rate", "challenge_ratio", "source_reliability_avg"}
        assert body["metrics"]["verification_rate"] is None
        assert "actions" not in body

    def test_get_metrics_semantic_prometheus(self, test_server):
        port, _store = test_server
        from tests.conftest import _request

        status, body = _request("GET", port, "/metrics/semantic?format=prometheus")
        assert status == 200
        assert "ohm_semantic_layer_metrics" in body
        assert "verification_rate" in body

    def test_get_metrics_semantic_actions_no_side_effects(self, test_server):
        port, _store = test_server
        from tests.conftest import _request

        status, body = _request("GET", port, "/metrics/semantic?actions=true")
        assert status == 200
        assert body["count"] == 3
        assert "actions" in body
        assert isinstance(body["actions"], list)
        # GET must not create anything; no executed key.
        assert "executed" not in body

    def test_post_metrics_semantic_actions_creates_tasks(self, test_server, tmp_path):
        import subprocess

        port, store = test_server
        from tests.conftest import _request

        # Seed a low-reliability outcome so a threshold fires.
        claim = create_node(store.conn, label="Claim", node_type="concept", created_by="test_agent")
        query_record_outcome(
            store.conn,
            source_agent="low_reliability",
            claim_node=claim["id"],
            outcome=False,
            recorded_by="test_agent",
        )

        repo = tmp_path / "post_repo"
        repo.mkdir()
        subprocess.run(["bd", "init"], cwd=str(repo), check=True, capture_output=True)

        status, body = _request(
            "POST",
            port,
            "/metrics/semantic/actions",
            body={"repo_path": str(repo)},
        )
        assert status == 200
        assert "executed" in body
        executed = body["executed"]
        assert any(e["status"] == "created" for e in executed)


class TestSemanticLayerAutoActions:
    """Tests for OHM-wx42 automatic metric actions + rate limiting."""

    @pytest.fixture
    def beads_repo(self, tmp_path):
        import subprocess

        repo = tmp_path / "auto_action_repo"
        repo.mkdir()
        subprocess.run(["bd", "init"], cwd=str(repo), check=True, capture_output=True)
        return str(repo)

    @pytest.fixture
    def low_reliability_graph(self, test_db):
        claim = create_node(test_db, label="Claim", node_type="concept", created_by="test_agent")
        query_record_outcome(
            test_db,
            source_agent="low_reliability",
            claim_node=claim["id"],
            outcome=False,
            recorded_by="test_agent",
        )
        return test_db

    def test_default_config_has_auto_actions_disabled(self):
        assert "semantic_layer" in DEFAULT_CONFIG
        sl = DEFAULT_CONFIG["semantic_layer"]
        assert sl["auto_actions_enabled"] is False
        assert sl["auto_actions_interval_seconds"] == 3600
        assert sl["auto_actions_rate_limit_seconds"] == 86400

    def test_run_actions_respects_rate_limit_window(self, low_reliability_graph, beads_repo):
        # First run creates the action.
        result1 = run_metrics_and_actions(
            low_reliability_graph,
            repo_path=beads_repo,
            execute=True,
            use_ibis=False,
            rate_limit_window_seconds=86400,
        )
        assert any(e["status"] == "created" for e in result1["executed"])

        # Immediate rerun is rate-limited for the same (metric, threshold, action_type).
        result2 = run_metrics_and_actions(
            low_reliability_graph,
            repo_path=beads_repo,
            execute=True,
            use_ibis=False,
            rate_limit_window_seconds=86400,
        )
        assert all(e["status"] != "created" for e in result2["executed"])
        assert any(e.get("reason") == "rate_limited" for e in result2["executed"])

        # A zero-second window disables deduplication.
        result3 = run_metrics_and_actions(
            low_reliability_graph,
            repo_path=beads_repo,
            execute=True,
            use_ibis=False,
            rate_limit_window_seconds=0,
        )
        assert any(e["status"] == "created" for e in result3["executed"])

    def test_auto_actions_disabled_does_not_create_tasks(self, low_reliability_graph, beads_repo):
        # First call creates the action.
        run_metrics_and_actions(
            low_reliability_graph,
            repo_path=beads_repo,
            execute=True,
            use_ibis=False,
            rate_limit_window_seconds=86400,
        )
        # Second call is within the rate-limit window so it is skipped.
        result = run_metrics_and_actions(
            low_reliability_graph,
            repo_path=beads_repo,
            execute=True,
            use_ibis=False,
            rate_limit_window_seconds=86400,
        )
        assert "executed" in result
        assert not any(e["status"] == "created" for e in result["executed"])
        assert any(e.get("reason") == "rate_limited" for e in result["executed"])

    def test_run_actions_rate_limit_helpers(self, test_db):
        # _is_rate_limited returns False when no record exists.
        assert _is_rate_limited(test_db, "verification_rate", "< 0.3", "create_task", 86400) is False
        _record_action(test_db, "verification_rate", "< 0.3", "create_task", "task-1")
        assert _is_rate_limited(test_db, "verification_rate", "< 0.3", "create_task", 86400) is True
        assert _is_rate_limited(test_db, "verification_rate", "< 0.3", "create_task", 0) is False
        # Different action type is not limited.
        assert _is_rate_limited(test_db, "verification_rate", "< 0.3", "prompt_agent", 86400) is False
