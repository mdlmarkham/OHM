"""Tests for OHM hook system (OHM-aznh.2)."""

import pytest

from ohm.hooks import HookRecord, HookResult, HookRunner, VALID_HOOK_EVENTS


class TestHookRecord:
    """Tests for HookRecord dataclass."""

    def test_valid_events(self):
        for event in VALID_HOOK_EVENTS:
            r = HookRecord(id="h1", event=event, command="echo ok")
            assert r.event == event

    def test_invalid_event_raises(self):
        with pytest.raises(ValueError, match="Invalid hook event"):
            HookRecord(id="h1", event="invalid_event", command="echo ok")

    def test_defaults(self):
        r = HookRecord(id="h1", event="pre_ingest", command="echo ok")
        assert r.timeout_ms == 5000
        assert r.enabled is True
        assert r.created_by == "system"
        assert r.created_at is None
        assert r.updated_at is None

    def test_custom_values(self):
        r = HookRecord(
            id="h2",
            event="post_ingest",
            command="python:ohm.hooks_builtin.cross_link_check",
            timeout_ms=10000,
            enabled=False,
            created_by="metis",
            created_at="2026-06-04",
        )
        assert r.timeout_ms == 10000
        assert r.enabled is False
        assert r.created_by == "metis"
        assert r.command.startswith("python:")


class TestHookResult:
    """Tests for HookResult dataclass."""

    def test_success_property(self):
        r = HookResult(hook_id="h1", exit_code=0)
        assert r.success is True

    def test_failure_nonzero_exit(self):
        r = HookResult(hook_id="h1", exit_code=1)
        assert r.success is False

    def test_failure_timed_out(self):
        r = HookResult(hook_id="h1", exit_code=0, timed_out=True)
        assert r.success is False

    def test_defaults(self):
        r = HookResult(hook_id="h1")
        assert r.exit_code == 0
        assert r.stdout == ""
        assert r.stderr == ""
        assert r.duration_ms == 0.0
        assert r.timed_out is False


class TestHookRunnerGetHooks:
    """Tests for HookRunner.get_hooks() reading from ohm_hooks table."""

    def test_get_hooks_returns_registered_hooks(self, test_db):
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h1", "pre_ingest", "echo validate", "test"],
        )
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h2", "post_ingest", "echo done", "test"],
        )
        runner = HookRunner(test_db)
        hooks = runner.get_hooks("pre_ingest")
        assert len(hooks) == 1
        assert hooks[0].id == "h1"
        assert hooks[0].event == "pre_ingest"

    def test_get_hooks_filters_by_event(self, test_db):
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h1", "pre_ingest", "echo validate", "test"],
        )
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h2", "post_ingest", "echo done", "test"],
        )
        runner = HookRunner(test_db)
        pre = runner.get_hooks("pre_ingest")
        post = runner.get_hooks("post_ingest")
        assert len(pre) == 1
        assert len(post) == 1
        assert pre[0].command == "echo validate"
        assert post[0].command == "echo done"

    def test_get_hooks_enabled_only(self, test_db):
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, enabled, created_by) VALUES (?, ?, ?, ?, ?)",
            ["h1", "pre_ingest", "echo active", True, "test"],
        )
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, enabled, created_by) VALUES (?, ?, ?, ?, ?)",
            ["h2", "pre_ingest", "echo disabled", False, "test"],
        )
        runner = HookRunner(test_db)
        enabled = runner.get_hooks("pre_ingest", enabled_only=True)
        all_hooks = runner.get_hooks("pre_ingest", enabled_only=False)
        assert len(enabled) == 1
        assert enabled[0].id == "h1"
        assert len(all_hooks) == 2

    def test_get_hooks_invalid_event_raises(self, test_db):
        runner = HookRunner(test_db)
        with pytest.raises(ValueError, match="Invalid hook event"):
            runner.get_hooks("nonexistent_event")

    def test_get_hooks_empty_table(self, test_db):
        runner = HookRunner(test_db)
        hooks = runner.get_hooks("pre_ingest")
        assert hooks == []


class TestHookRunnerRunHook:
    """Tests for HookRunner.run_hook() stub."""

    def test_run_hook_stub_returns_success(self, test_db):
        hook = HookRecord(id="h1", event="pre_ingest", command="echo ok")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {"agent": "metis", "action": "node"})
        assert result.success is True
        assert result.exit_code == 0
        assert result.hook_id == "h1"


class TestHookRunnerRunHooks:
    """Tests for HookRunner.run_hooks() with multiple hooks."""

    def test_run_hooks_executes_all(self, test_db):
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h1", "pre_ingest", "echo a", "test"],
        )
        test_db.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h2", "pre_ingest", "echo b", "test"],
        )
        runner = HookRunner(test_db)
        results = runner.run_hooks("pre_ingest", {"agent": "metis"})
        assert len(results) == 2
        assert all(r.success for r in results)

    def test_run_hooks_no_hooks_returns_empty(self, test_db):
        runner = HookRunner(test_db)
        results = runner.run_hooks("pre_ingest", {"agent": "metis"})
        assert results == []


class TestCreateHook:
    """Tests for queries.create_hook()."""

    def test_create_hook_basic(self, test_db):
        from ohm.queries import create_hook

        hook = create_hook(test_db, event="pre_ingest", command="echo validate", created_by="metis")
        assert hook["event"] == "pre_ingest"
        assert hook["command"] == "echo validate"
        assert hook["created_by"] == "metis"
        assert hook["timeout_ms"] == 5000
        assert hook["enabled"] is True

    def test_create_hook_custom_timeout(self, test_db):
        from ohm.queries import create_hook

        hook = create_hook(test_db, event="post_ingest", command="echo done", created_by="clio", timeout_ms=10000, enabled=False)
        assert hook["timeout_ms"] == 10000
        assert hook["enabled"] is False

    def test_create_hook_invalid_event(self, test_db):
        from ohm.queries import create_hook

        with pytest.raises(ValueError, match="Invalid hook event"):
            create_hook(test_db, event="bad_event", command="echo", created_by="metis")

    def test_create_hook_empty_command(self, test_db):
        from ohm.queries import create_hook

        with pytest.raises(ValueError, match="non-empty string"):
            create_hook(test_db, event="pre_ingest", command="", created_by="metis")

    def test_create_hook_timeout_out_of_range(self, test_db):
        from ohm.queries import create_hook

        with pytest.raises(ValueError, match="timeout_ms"):
            create_hook(test_db, event="pre_ingest", command="echo", created_by="metis", timeout_ms=50)
        with pytest.raises(ValueError, match="timeout_ms"):
            create_hook(test_db, event="pre_ingest", command="echo", created_by="metis", timeout_ms=70000)


class TestQueryHooks:
    """Tests for queries.query_hooks()."""

    def test_query_hooks_empty(self, test_db):
        from ohm.queries import query_hooks

        hooks = query_hooks(test_db)
        assert hooks == []

    def test_query_hooks_returns_all(self, test_db):
        from ohm.queries import create_hook, query_hooks

        create_hook(test_db, event="pre_ingest", command="echo a", created_by="metis")
        create_hook(test_db, event="post_ingest", command="echo b", created_by="clio")
        hooks = query_hooks(test_db)
        assert len(hooks) == 2

    def test_query_hooks_filter_by_event(self, test_db):
        from ohm.queries import create_hook, query_hooks

        create_hook(test_db, event="pre_ingest", command="echo a", created_by="metis")
        create_hook(test_db, event="post_ingest", command="echo b", created_by="clio")
        hooks = query_hooks(test_db, event="pre_ingest")
        assert len(hooks) == 1
        assert hooks[0]["event"] == "pre_ingest"

    def test_query_hooks_invalid_event(self, test_db):
        from ohm.queries import query_hooks

        with pytest.raises(ValueError, match="Invalid hook event"):
            query_hooks(test_db, event="bad_event")


class TestDeleteHook:
    """Tests for queries.delete_hook()."""

    def test_delete_hook_existing(self, test_db):
        from ohm.queries import create_hook, delete_hook

        hook = create_hook(test_db, event="pre_ingest", command="echo", created_by="metis")
        result = delete_hook(test_db, hook_id=hook["id"], deleted_by="metis")
        assert result["deleted"] == hook["id"]
        assert result["type"] == "hook"

    def test_delete_hook_not_found(self, test_db):
        from ohm.queries import delete_hook

        with pytest.raises(ValueError, match="Hook not found"):
            delete_hook(test_db, hook_id="nonexistent", deleted_by="metis")

    def test_delete_hook_removes_from_list(self, test_db):
        from ohm.queries import create_hook, delete_hook, query_hooks

        h = create_hook(test_db, event="pre_ingest", command="echo", created_by="metis")
        delete_hook(test_db, hook_id=h["id"], deleted_by="metis")
        hooks = query_hooks(test_db)
        assert hooks == []
