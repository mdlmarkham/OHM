"""Tests for OHM-797: Guided bootstrap interview for fresh instances."""

from __future__ import annotations

import pytest

from ohm.graph.schema import initialize_schema, get_meta, set_meta
from ohm.server.bootstrap import (
    is_fresh_instance,
    get_current_step,
    submit_answer,
    abandon_bootstrap,
    bootstrap_from_template,
    clear_bootstrap_state,
    is_custom_type_valid,
    STEPS,
)


@pytest.fixture
def db():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


class TestFreshInstanceDetection:
    def test_fresh_db_is_fresh(self, db):
        assert is_fresh_instance(db) is True

    def test_not_fresh_after_schema_persisted(self, db):
        from ohm.graph.schema import SchemaConfig

        SchemaConfig(name="test").to_db(db)
        assert is_fresh_instance(db) is False

    def test_not_fresh_with_nodes(self, db):
        db.execute("INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES ('n1', 'Test', 'concept', 'agent', CURRENT_TIMESTAMP)")
        assert is_fresh_instance(db) is False

    def test_not_fresh_with_deleted_nodes_only(self, db):
        db.execute("INSERT INTO ohm_nodes (id, label, type, created_by, created_at, deleted_at) VALUES ('n1', 'Test', 'concept', 'agent', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        assert is_fresh_instance(db) is True  # deleted nodes don't count


class TestGetStep:
    def test_first_step_on_fresh_db(self, db):
        result = get_current_step(db)
        assert "step" in result
        assert result["step"] == 0
        assert "prompt" in result
        assert result["total_steps"] == len(STEPS)

    def test_complete_after_bootstrap(self, db):
        from ohm.graph.schema import SchemaConfig

        SchemaConfig(name="done").to_db(db)
        result = get_current_step(db)
        assert result.get("complete") is True

    def test_corrupted_state_detected(self, db):
        set_meta(db, "bootstrap.step", "not_a_number")
        result = get_current_step(db)
        assert result.get("corrupted") is True


class TestSubmitAnswer:
    def test_domain_name_step(self, db):
        result = submit_answer(db, "my_domain")
        assert result["ok"] is True
        assert result["step"] == 1

    def test_invalid_domain_name_rejected(self, db):
        result = submit_answer(db, "UPPERCASE")
        assert result["ok"] is False
        assert result["error"] == "invalid_domain_name"

    def test_credential_in_domain_name_rejected(self, db):
        result = submit_answer(db, "api_key_12345")
        assert result["ok"] is False
        assert result["error"] == "credential_detected"

    def test_full_interview_flow(self, db):
        # Step 0: domain name
        r = submit_answer(db, "my_domain")
        assert r["ok"] is True
        assert r["step"] == 1

        # Step 1: description (optional, empty ok)
        r = submit_answer(db, "A test domain")
        assert r["ok"] is True
        assert r["step"] == 2

        # Step 2: vocabulary choice
        r = submit_answer(db, "default")
        assert r["ok"] is True
        assert r["step"] == 3

        # Step 3: onboarding node
        r = submit_answer(db, "auto")
        assert r["ok"] is True
        assert r["step"] == 4

        # Step 4: confirm
        r = submit_answer(db, "yes")
        assert r["ok"] is True
        assert r.get("complete") is True
        assert r["domain_name"] == "my_domain"

        # Verify persisted
        from ohm.graph.schema import SchemaConfig

        loaded = SchemaConfig.from_db(db)
        assert loaded is not None
        assert loaded.name == "my_domain"

    def test_not_confirmed_returns_to_confirm_step(self, db):
        submit_answer(db, "my_domain")
        submit_answer(db, "desc")
        submit_answer(db, "default")
        submit_answer(db, "auto")
        r = submit_answer(db, "no")
        assert r["ok"] is False
        assert r["error"] == "not_confirmed"

    def test_choice_validation(self, db):
        submit_answer(db, "my_domain")
        submit_answer(db, "desc")
        r = submit_answer(db, "invalid_choice")
        assert r["ok"] is False
        assert r["error"] == "invalid_choice"

    def test_already_bootstrapped_rejected(self, db):
        from ohm.graph.schema import SchemaConfig

        SchemaConfig(name="done").to_db(db)
        r = submit_answer(db, "new_domain")
        assert r["ok"] is False
        assert r["error"] == "already_bootstrapped"


class TestResumeAfterRestart:
    def test_wip_state_persisted(self, db):
        submit_answer(db, "my_domain")
        # Simulate restart by creating a new connection to the same data
        # (in-memory DB is gone, but we can check the state was saved)
        state_step = get_meta(db, "bootstrap.step")
        assert state_step == "1"

    def test_resume_from_step_1(self, db):
        # Manually set WIP state to step 1 with answers
        import json

        set_meta(db, "bootstrap.step", "1")
        set_meta(db, "bootstrap.answers", json.dumps({"domain_name": "my_domain"}))
        # Get current step should return step 1
        result = get_current_step(db)
        assert result["step"] == 1
        assert result["field"] == "description"


class TestCorruptedStateRecovery:
    def test_abandon_clears_state(self, db):
        import json

        set_meta(db, "bootstrap.step", "corrupt")
        set_meta(db, "bootstrap.answers", "not json")
        result = abandon_bootstrap(db)
        assert result["ok"] is True
        # State should be cleared
        assert get_meta(db, "bootstrap.step") is None
        assert get_meta(db, "bootstrap.answers") is None

    def test_get_step_after_corruption_reports_it(self, db):
        set_meta(db, "bootstrap.step", "not_a_number")
        result = get_current_step(db)
        assert result.get("corrupted") is True
        assert "abandon" in result["message"].lower()


class TestBootstrapFromTemplate:
    def test_loads_ohm_template(self, db):
        result = bootstrap_from_template(db, "ohm")
        assert result["ok"] is True
        assert result["domain_name"] == "ohm"

    def test_loads_topo_template(self, db):
        result = bootstrap_from_template(db, "topo")
        assert result["ok"] is True

    def test_loads_beef_herd_template(self, db):
        result = bootstrap_from_template(db, "beef_herd")
        assert result["ok"] is True

    def test_invalid_name_rejected(self, db):
        result = bootstrap_from_template(db, "INVALID NAME")
        assert result["ok"] is False
        assert result["error"] == "invalid_domain_name"

    def test_additive_only_on_rerun(self, db):
        from ohm.graph.schema import SchemaConfig

        # First bootstrap
        bootstrap_from_template(db, "ohm")
        first = SchemaConfig.from_db(db)
        original_types = first.node_types

        # Second bootstrap with different domain
        bootstrap_from_template(db, "topo")
        second = SchemaConfig.from_db(db)

        # Should have merged types (additive)
        assert second.node_types >= original_types


class TestCustomTypeValidation:
    def test_valid_custom_type_accepted(self):
        assert is_custom_type_valid("my_custom_type") is True

    def test_reserved_type_rejected(self):
        assert is_custom_type_valid("concept") is False

    def test_ohm_prefix_rejected(self):
        assert is_custom_type_valid("ohm_custom") is False

    def test_system_prefix_rejected(self):
        assert is_custom_type_valid("system_type") is False

    def test_uppercase_rejected(self):
        assert is_custom_type_valid("MyType") is False

    def test_starts_with_number_rejected(self):
        assert is_custom_type_valid("9type") is False


class TestStepCount:
    def test_has_5_steps(self):
        assert len(STEPS) == 5
