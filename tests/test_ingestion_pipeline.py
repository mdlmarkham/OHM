"""Tests for the staged ingestion pipeline with shell-hook architecture (OHM-tjkx)."""

from __future__ import annotations

import base64
import sys
import json
from pathlib import Path

import duckdb
import pytest

from ohm.schema import initialize_schema
from ohm.ingestion.pipeline import run_pipeline, PipelineResult
from ohm.hooks import hooks_enabled, VALID_HOOK_EVENTS, INGESTION_STAGES, HookRunner, HookRecord


@pytest.fixture
def test_conn():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


def _make_item(content: bytes = b"# Test Doc\n\nA paragraph.", filename: str = "test.md", **kw) -> dict:
    item = {
        "id": "test-item-001",
        "content_bytes": base64.b64encode(content).decode(),
        "filename": filename,
        "content_type": "text/markdown",
    }
    item.update(kw)
    return item


class TestHooksEnabled:
    """Test the hooks_enabled() CI-mode check."""

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("BEADS_HOOKS", raising=False)
        monkeypatch.delenv("OHM_NO_HOOKS", raising=False)
        assert hooks_enabled() is True

    def test_disabled_by_beads_hooks_0(self, monkeypatch):
        monkeypatch.setenv("BEADS_HOOKS", "0")
        assert hooks_enabled() is False

    def test_disabled_by_ohm_no_hooks(self, monkeypatch):
        monkeypatch.setenv("OHM_NO_HOOKS", "1")
        assert hooks_enabled() is False

    def test_enabled_when_beads_hooks_1(self, monkeypatch):
        monkeypatch.setenv("BEADS_HOOKS", "1")
        assert hooks_enabled() is True


class TestValidHookEvents:
    """Verify ingestion stage events are in VALID_HOOK_EVENTS."""

    def test_ingestion_events_present(self):
        for event in ("pre_fetch", "post_fetch", "pre_parse", "post_parse", "pre_commit", "post_commit", "on_error"):
            assert event in VALID_HOOK_EVENTS, f"Missing event: {event}"

    def test_legacy_events_still_present(self):
        for event in ("pre_ingest", "post_ingest", "pre_query", "post_query"):
            assert event in VALID_HOOK_EVENTS

    def test_ingestion_stages_list(self):
        assert INGESTION_STAGES == ("fetch", "parse", "commit")


class TestPipelineBasic:
    """Test the pipeline without hooks (skip_hooks=True)."""

    def test_pipeline_creates_source_node(self, test_conn):
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent", skip_hooks=True)
        assert result.success
        assert result.source_node_id is not None
        assert len(result.stages) == 3
        assert result.stages[0]["stage"] == "fetch"
        assert result.stages[1]["stage"] == "parse"
        assert result.stages[2]["stage"] == "commit"

    def test_pipeline_creates_correct_node_type(self, test_conn):
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent", skip_hooks=True)
        row = test_conn.execute(
            "SELECT type FROM ohm_nodes WHERE id = ?", [result.source_node_id]
        ).fetchone()
        assert row[0] == "source"

    def test_pipeline_html_content(self, test_conn):
        html = b"<html><body><h1>Title</h1><p>Body text.</p></body></html>"
        item = _make_item(content=html, filename="doc.html", content_type="text/html")
        result = run_pipeline(test_conn, item, created_by="test_agent", skip_hooks=True)
        assert result.success
        assert result.source_node_id is not None

    def test_pipeline_plain_text(self, test_conn):
        item = _make_item(content=b"Just plain text.", filename="note.txt", content_type="text/plain")
        result = run_pipeline(test_conn, item, created_by="test_agent", skip_hooks=True)
        assert result.success

    def test_pipeline_missing_content_raises(self, test_conn):
        item = {"id": "bad-item"}  # no url, no local_path, no content_bytes
        result = run_pipeline(test_conn, item, created_by="test_agent", skip_hooks=True)
        assert not result.success
        assert result.error is not None

    def test_pipeline_result_to_dict(self, test_conn):
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent", skip_hooks=True)
        d = result.to_dict()
        assert d["item_id"] == "test-item-001"
        assert d["success"] is True
        assert "stages" in d
        assert "duration_ms" in d


class TestPipelineWithHooks:
    """Test the pipeline with hooks registered."""

    def test_pre_fetch_hook_runs(self, test_conn):
        # Register a simple pre_fetch hook that just passes
        cmd = f'{sys.executable} -c "import sys,json; json.load(sys.stdin); print(json.dumps({{}}))"'
        test_conn.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h-pre-fetch", "pre_fetch", cmd, "test"],
        )
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent")
        assert result.success
        assert result.stages[0]["hooks_run"] >= 1
        # Hook log should have an entry
        log = test_conn.execute(
            "SELECT event, exit_code FROM ohm_hook_log WHERE hook_id = ? ORDER BY triggered_at DESC LIMIT 1",
            ["h-pre-fetch"],
        ).fetchone()
        assert log is not None
        assert log[0] == "pre_fetch"
        assert log[1] == 0

    def test_pre_fetch_hook_aborts_pipeline(self, test_conn):
        # Register a pre_fetch hook that exits non-zero
        cmd = f'{sys.executable} -c "import sys; sys.exit(1)"'
        test_conn.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h-abort", "pre_fetch", cmd, "test"],
        )
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent")
        assert not result.success
        assert result.aborted_by_hook == "h-abort"
        # Only the fetch stage should be recorded (aborted before it ran)
        assert len(result.stages) == 1
        assert result.stages[0]["status"] == "aborted"

    def test_post_commit_hook_runs_after_commit(self, test_conn):
        cmd = f'{sys.executable} -c "import sys,json; json.load(sys.stdin); print(json.dumps({{}}))"'
        test_conn.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h-post-commit", "post_commit", cmd, "test"],
        )
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent")
        assert result.success
        assert result.source_node_id is not None
        # Check hook log for post_commit
        log = test_conn.execute(
            "SELECT event FROM ohm_hook_log WHERE hook_id = ? ORDER BY triggered_at DESC LIMIT 1",
            ["h-post-commit"],
        ).fetchone()
        assert log is not None
        assert log[0] == "post_commit"

    def test_on_error_hook_fires_on_failure(self, test_conn):
        cmd = f'{sys.executable} -c "import sys,json; json.load(sys.stdin); print(json.dumps({{}}))"'
        test_conn.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h-error", "on_error", cmd, "test"],
        )
        # Item with no content source → pipeline fails
        item = {"id": "fail-item"}
        result = run_pipeline(test_conn, item, created_by="test_agent")
        assert not result.success
        # on_error hook should have fired
        log = test_conn.execute(
            "SELECT event FROM ohm_hook_log WHERE hook_id = ? ORDER BY triggered_at DESC LIMIT 1",
            ["h-error"],
        ).fetchone()
        assert log is not None
        assert log[0] == "on_error"

    def test_ci_mode_bypasses_hooks(self, test_conn, monkeypatch):
        monkeypatch.setenv("BEADS_HOOKS", "0")
        cmd = f'{sys.executable} -c "import sys; sys.exit(1)"'
        test_conn.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h-ci-abort", "pre_fetch", cmd, "test"],
        )
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent")
        # Hooks bypassed → pipeline succeeds despite the abort hook
        assert result.success
        assert result.aborted_by_hook is None
        assert result.stages[0]["hooks_run"] == 0

    def test_skip_hooks_flag_bypasses_hooks(self, test_conn):
        cmd = f'{sys.executable} -c "import sys; sys.exit(1)"'
        test_conn.execute(
            "INSERT INTO ohm_hooks (id, event, command, created_by) VALUES (?, ?, ?, ?)",
            ["h-skip", "pre_fetch", cmd, "test"],
        )
        item = _make_item()
        result = run_pipeline(test_conn, item, created_by="test_agent", skip_hooks=True)
        assert result.success
        assert result.aborted_by_hook is None
        assert result.stages[0]["hooks_run"] == 0


class TestPipelineResultDataclass:
    """Test PipelineResult properties."""

    def test_success_property(self):
        r = PipelineResult(item_id="x")
        assert r.success is True
        r.error = "failed"
        assert r.success is False
        r.error = None
        r.aborted_by_hook = "h1"
        assert r.success is False

    def test_to_dict(self):
        r = PipelineResult(item_id="x", source_node_id="n1", duration_ms=42.5)
        r.stages.append({"stage": "fetch", "status": "ok"})
        d = r.to_dict()
        assert d["item_id"] == "x"
        assert d["source_node_id"] == "n1"
        assert d["success"] is True
        assert d["duration_ms"] == 42.5
        assert len(d["stages"]) == 1


class TestHookRecordValidation:
    """Verify HookRecord accepts the new ingestion events."""

    def test_pre_fetch_hook_record(self):
        h = HookRecord(id="h1", event="pre_fetch", command="echo test")
        assert h.event == "pre_fetch"

    def test_post_commit_hook_record(self):
        h = HookRecord(id="h2", event="post_commit", command="echo done")
        assert h.event == "post_commit"

    def test_on_error_hook_record(self):
        h = HookRecord(id="h3", event="on_error", command="echo err")
        assert h.event == "on_error"

    def test_invalid_event_raises(self):
        with pytest.raises(ValueError, match="Invalid hook event"):
            HookRecord(id="h4", event="pre_nonexistent_stage", command="echo x")