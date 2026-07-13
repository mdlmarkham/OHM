"""Tests for OHM-847: Nudge-message optimization autoresearch loop.

Covers Fisher's exact test, variant selection, exposure logging,
evaluation with insufficient-data floor, promotion/demotion, and
HTTP endpoints.
"""

import json
import pytest

from tests.conftest import _request

pytestmark = pytest.mark.integration


class TestSchemaMigration:
    """Schema-level invariants."""

    def test_schema_version_bumped(self):
        from ohm.graph.schema import SCHEMA_VERSION
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 56, 0)

    def test_migration_0_56_0_present(self):
        from ohm.graph.schema import MIGRATIONS
        versions = [m[0] for m in MIGRATIONS]
        assert "0.56.0" in versions

    def test_migration_adds_variant_id(self):
        from ohm.graph.schema import MIGRATIONS
        for ver, desc, stmts in MIGRATIONS:
            if ver == "0.56.0":
                assert "variant_id" in desc.lower()
                assert any("variant_id" in s for s in stmts)
                return
        pytest.fail("Migration 0.56.0 not found")


class TestFisherExact:
    """Fisher's exact test unit tests."""

    def test_significant_difference(self):
        from ohm.server.nudge_optimization import _fisher_exact
        p = _fisher_exact(20, 5, 10, 25)
        assert p < 0.05

    def test_no_difference(self):
        from ohm.server.nudge_optimization import _fisher_exact
        p = _fisher_exact(15, 15, 15, 15)
        assert p >= 0.05

    def test_empty_returns_one(self):
        from ohm.server.nudge_optimization import _fisher_exact
        assert _fisher_exact(0, 0, 0, 0) == 1.0

    def test_extreme_difference(self):
        from ohm.server.nudge_optimization import _fisher_exact
        p = _fisher_exact(30, 0, 0, 30)
        assert p < 0.01


class TestSelectVariant:
    """Variant selection tests."""

    def test_single_variant(self, test_db):
        from ohm.server.nudge_optimization import select_variant
        result = select_variant(test_db, nudge_type="test_nudge", variants=["A"])
        assert result == "A"

    def test_no_variants(self, test_db):
        from ohm.server.nudge_optimization import select_variant
        result = select_variant(test_db, nudge_type="test_nudge", variants=[])
        assert result == "default"

    def test_least_exposed_variant(self, test_db):
        from ohm.server.nudge_optimization import select_variant
        for _ in range(35):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'A')",
                [f"nudge_a_{_}"],
            )
        test_db.commit()
        result = select_variant(test_db, nudge_type="test_nudge", variants=["A", "B"])
        assert result == "B"


class TestRecordExposure:
    """Exposure logging tests."""

    def test_record_exposure(self, test_db):
        from ohm.server.nudge_optimization import record_exposure
        record_exposure(
            test_db,
            nudge_id="exp1",
            nudge_type="test_nudge",
            variant_id="A",
            agent="agent1",
            message="Test message",
        )
        test_db.commit()
        row = test_db.execute(
            "SELECT variant_id, nudge_type, agent, message FROM ohm_nudge_log WHERE id = 'exp1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "A"
        assert row[1] == "test_nudge"

    def test_record_exposure_best_effort(self, test_db):
        from ohm.server.nudge_optimization import record_exposure
        record_exposure(
            test_db,
            nudge_id="exp2",
            nudge_type="test_nudge",
            variant_id="B",
            agent="agent1",
            message="Test message B",
            target_id="node1",
        )
        test_db.commit()
        rows = test_db.execute(
            "SELECT COUNT(*) FROM ohm_nudge_log WHERE id = 'exp2'"
        ).fetchone()
        assert rows[0] == 1


class TestEvaluateNudgeVariants:
    """Evaluation tests."""

    def test_insufficient_data_no_exposures(self, test_db):
        from ohm.server.nudge_optimization import evaluate_nudge_variants
        result = evaluate_nudge_variants(test_db, nudge_type="nonexistent")
        assert result["insufficient_data"] is True
        assert result["winner"] is None

    def test_insufficient_data_one_variant(self, test_db):
        from ohm.server.nudge_optimization import evaluate_nudge_variants
        for i in range(40):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'A', true)",
                [f"n_a_{i}"],
            )
        test_db.commit()
        result = evaluate_nudge_variants(test_db, nudge_type="test_nudge")
        assert result["insufficient_data"] is True

    def test_insufficient_data_below_floor(self, test_db):
        from ohm.server.nudge_optimization import evaluate_nudge_variants
        for i in range(10):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'A', true)",
                [f"n_a_{i}"],
            )
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'B', false)",
                [f"n_b_{i}"],
            )
        test_db.commit()
        result = evaluate_nudge_variants(test_db, nudge_type="test_nudge", min_exposures=30)
        assert result["insufficient_data"] is True

    def test_significant_winner(self, test_db):
        from ohm.server.nudge_optimization import evaluate_nudge_variants
        for i in range(35):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'A', true)",
                [f"w_a_{i}"],
            )
        for i in range(35):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'B', false)",
                [f"w_b_{i}"],
            )
        test_db.commit()
        result = evaluate_nudge_variants(test_db, nudge_type="test_nudge")
        assert result["insufficient_data"] is False
        assert result["winner"] == "A"
        assert result["p_value"] < 0.05

    def test_no_significant_winner(self, test_db):
        from ohm.server.nudge_optimization import evaluate_nudge_variants
        for i in range(35):
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'A', true)",
                [f"t_a_{i}"],
            )
            test_db.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'test_nudge', 'info', 'B', true)",
                [f"t_b_{i}"],
            )
        test_db.commit()
        result = evaluate_nudge_variants(test_db, nudge_type="test_nudge")
        assert result["insufficient_data"] is False
        assert result["winner"] is None


class TestPromoteDemote:
    """Promotion and demotion tests."""

    def test_promote_variant(self, test_db):
        from ohm.server.nudge_optimization import promote_nudge_variant, get_default_variant
        result = promote_nudge_variant(test_db, nudge_type="test_nudge", variant_id="B")
        assert result["status"] == "promoted"
        assert get_default_variant(test_db, nudge_type="test_nudge") == "B"

    def test_demote_variant(self, test_db):
        from ohm.server.nudge_optimization import promote_nudge_variant, demote_nudge_variant, get_default_variant
        promote_nudge_variant(test_db, nudge_type="test_nudge", variant_id="A")
        assert get_default_variant(test_db, nudge_type="test_nudge") == "A"
        demote_nudge_variant(test_db, nudge_type="test_nudge")
        assert get_default_variant(test_db, nudge_type="test_nudge") is None

    def test_get_default_variant_none(self, test_db):
        from ohm.server.nudge_optimization import get_default_variant
        assert get_default_variant(test_db, nudge_type="nonexistent") is None


class TestHTTPIntegration:
    """HTTP endpoint tests."""

    def test_evaluate_via_http(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/nudge/evaluate", {
            "nudge_type": "nonexistent",
        })
        assert status == 200
        assert data["insufficient_data"] is True

    def test_evaluate_missing_type(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/nudge/evaluate", {})
        assert status == 422

    def test_promote_via_http(self, test_server):
        port, store = test_server
        status, data = _request("POST", port, "/nudge/promote", {
            "nudge_type": "test_nudge",
            "variant_id": "B",
        })
        assert status == 200
        assert data["status"] == "promoted"

        from ohm.server.nudge_optimization import get_default_variant
        assert get_default_variant(store.read_conn, nudge_type="test_nudge") == "B"

    def test_promote_missing_fields(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/nudge/promote", {"nudge_type": "test"})
        assert status == 422

    def test_demote_via_http(self, test_server):
        port, store = test_server
        _request("POST", port, "/nudge/promote", {
            "nudge_type": "test_nudge",
            "variant_id": "A",
        })
        status, data = _request("POST", port, "/nudge/demote", {
            "nudge_type": "test_nudge",
        })
        assert status == 200
        assert data["status"] == "demoted"

    def test_demote_missing_type(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/nudge/demote", {})
        assert status == 422

    def test_full_loop_via_http(self, test_server):
        """Full loop: log exposures, accept/reject, evaluate, promote."""
        port, store = test_server
        conn = store.conn

        for i in range(35):
            conn.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'loop_test', 'info', 'A', true)",
                [f"loop_a_{i}"],
            )
        for i in range(35):
            conn.execute(
                "INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, variant_id, accepted) VALUES (?, 'a', 'node', 'loop_test', 'info', 'B', false)",
                [f"loop_b_{i}"],
            )
        conn.commit()

        status, eval_result = _request("POST", port, "/nudge/evaluate", {
            "nudge_type": "loop_test",
        })
        assert status == 200
        assert eval_result["winner"] == "A"

        status, promo_result = _request("POST", port, "/nudge/promote", {
            "nudge_type": "loop_test",
            "variant_id": "A",
        })
        assert status == 200
        assert promo_result["status"] == "promoted"