"""Tests for OHM hook system (OHM-aznh.2)."""

import pytest

from ohm.hooks import HookRecord, HookResult, HookRunner, VALID_HOOK_EVENTS, _SHELL_NOT_FOUND_EXIT, _TIMEOUT_EXIT


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
    """Tests for HookRunner.run_hook() — subprocess execution engine."""

    def test_shell_hook_captures_stdout(self, test_db):
        hook = HookRecord(id="h1", event="pre_ingest", command="echo hello")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {"agent": "metis"})
        assert result.success
        assert "hello" in result.stdout
        assert result.exit_code == 0

    def test_shell_hook_nonzero_exit(self, test_db):
        hook = HookRecord(id="h2", event="pre_ingest", command="exit 1")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {})
        assert not result.success
        assert result.exit_code == 1

    def test_shell_hook_command_not_found(self, test_db):
        hook = HookRecord(id="h3", event="pre_ingest", command="nonexistent_command_xyz_12345")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {})
        assert not result.success
        assert result.exit_code != 0

    def test_shell_hook_timeout(self, test_db):
        import sys

        if sys.platform == "win32":
            cmd = "ping -n 10 127.0.0.1"
        else:
            cmd = "sleep 10"
        hook = HookRecord(id="h4", event="pre_ingest", command=cmd, timeout_ms=200)
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {})
        assert not result.success
        assert result.timed_out is True
        assert result.exit_code == _TIMEOUT_EXIT

    def test_shell_hook_reads_stdin_payload(self, test_db):
        import json

        hook = HookRecord(id="h5", event="pre_ingest", command="python -c \"import sys,json; d=json.load(sys.stdin); print(d.get('agent',''))\"")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {"agent": "metis"})
        assert result.success
        assert "metis" in result.stdout

    def test_python_hook_import_and_call(self, test_db):
        hook = HookRecord(id="h6", event="pre_ingest", command="python:os.getcwd")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {})
        assert result.exit_code == 1
        assert result.stderr

    def test_python_hook_with_valid_callable(self, test_db):
        import types

        mod = types.ModuleType("_test_hook_mod")
        mod.test_hook = lambda payload: (0, "ok", "")
        import sys

        sys.modules["_test_hook_mod"] = mod
        try:
            hook = HookRecord(id="h7", event="pre_ingest", command="python:_test_hook_mod.test_hook")
            runner = HookRunner(test_db)
            result = runner.run_hook(hook, {"key": "value"})
            assert result.success
            assert result.stdout == "ok"
        finally:
            del sys.modules["_test_hook_mod"]

    def test_python_hook_invalid_format(self, test_db):
        hook = HookRecord(id="h8", event="pre_ingest", command="python:nodots")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {})
        assert result.exit_code == 1
        assert "Invalid python: hook format" in result.stderr

    def test_python_hook_import_error(self, test_db):
        hook = HookRecord(id="h9", event="pre_ingest", command="python:nonexistent_module.func")
        runner = HookRunner(test_db)
        result = runner.run_hook(hook, {})
        assert result.exit_code == 1


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


class TestHookInvocationLog:
    """Tests for ohm_hook_log audit trail (OHM-aznh.7)."""

    def test_shell_hook_creates_log_row(self, test_db):
        hook = HookRecord(id="h1", event="pre_ingest", command="echo logged")
        runner = HookRunner(test_db)
        runner.run_hook(hook, {"agent": "metis"})
        rows = test_db.execute("SELECT * FROM ohm_hook_log").fetchall()
        assert len(rows) == 1
        cols = [d[0] for d in test_db.description]
        row = dict(zip(cols, rows[0]))
        assert row["hook_id"] == "h1"
        assert row["event"] == "pre_ingest"
        assert row["exit_code"] == 0
        assert "logged" in row["stdout"]
        assert row["timed_out"] is False

    def test_failed_hook_creates_log_row(self, test_db):
        hook = HookRecord(id="h2", event="pre_ingest", command="exit 1")
        runner = HookRunner(test_db)
        runner.run_hook(hook, {})
        rows = test_db.execute("SELECT exit_code, timed_out FROM ohm_hook_log").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 1

    def test_timeout_hook_creates_log_row(self, test_db):
        import sys

        if sys.platform == "win32":
            cmd = "ping -n 10 127.0.0.1"
        else:
            cmd = "sleep 10"
        hook = HookRecord(id="h3", event="pre_ingest", command=cmd, timeout_ms=200)
        runner = HookRunner(test_db)
        runner.run_hook(hook, {})
        rows = test_db.execute("SELECT timed_out FROM ohm_hook_log").fetchall()
        assert len(rows) == 1
        assert rows[0][0] is True

    def test_payload_logged(self, test_db):
        hook = HookRecord(id="h4", event="post_ingest", command="echo ok")
        runner = HookRunner(test_db)
        runner.run_hook(hook, {"agent": "clio", "action": "node"})
        rows = test_db.execute("SELECT payload FROM ohm_hook_log").fetchall()
        assert len(rows) == 1
        import json

        payload = json.loads(rows[0][0])
        assert payload["agent"] == "clio"

    def test_multiple_invocations_create_multiple_rows(self, test_db):
        hook = HookRecord(id="h5", event="pre_ingest", command="echo ok")
        runner = HookRunner(test_db)
        runner.run_hook(hook, {})
        runner.run_hook(hook, {})
        rows = test_db.execute("SELECT COUNT(*) FROM ohm_hook_log").fetchone()
        assert rows[0] == 2
