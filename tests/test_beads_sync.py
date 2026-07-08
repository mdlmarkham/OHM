"""Tests for OHM-sdrr: Beads → OHM task sync."""

import json

import pytest

from ohm.integrations.beads_sync import (
    BD_PRIORITY_MAP,
    BD_STATUS_MAP,
    sync_beads_to_ohm_tasks,
    _normalize_assignee,
    BEADS_BACKLOG_ANCHOR_ID,
)


def _make_issue(**overrides):
    base = {
        "id": "OHM-test1",
        "title": "Test Issue",
        "description": "A test issue",
        "status": "open",
        "priority": 1,
        "assignee": "metis@olympus.local",
        "issue_type": "task",
        "labels": ["test", "integration"],
    }
    base.update(overrides)
    return base


class TestNormalizeAssignee:
    def test_strips_email_suffix(self):
        assert _normalize_assignee("metis@olympus.local") == "metis"

    def test_passes_plain_name_through(self):
        assert _normalize_assignee("metis") == "metis"

    def test_none_returns_none(self):
        assert _normalize_assignee(None) is None


class TestSyncBeadsToOHMTasks:
    """Unit tests for sync_beads_to_ohm_tasks via direct DB connection."""

    def test_creates_task_for_assigned_issue(self, test_db):
        issue = _make_issue()
        report = sync_beads_to_ohm_tasks(test_db, [issue])
        assert report["created"] == 1
        assert report["skipped"] == 0

        row = test_db.execute(
            "SELECT label, type, task_status, assigned_to, priority FROM ohm_nodes WHERE id = ?",
            ["beads_ohm_test1"],
        ).fetchone()
        assert row is not None
        assert row[0] == "Test Issue"
        assert row[1] == "task"
        assert row[2] == "open"
        assert row[3] == "metis"
        assert row[4] == "P1"

    def test_skips_unassigned_issue(self, test_db):
        issue = _make_issue(assignee=None)
        report = sync_beads_to_ohm_tasks(test_db, [issue])
        assert report["skipped"] == 1
        assert report["created"] == 0

    def test_creates_anchor_node(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue()])
        row = test_db.execute(
            "SELECT label, type FROM ohm_nodes WHERE id = ?",
            [BEADS_BACKLOG_ANCHOR_ID],
        ).fetchone()
        assert row is not None
        assert row[1] == "concept"

    def test_cross_links_task_to_anchor(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue()])
        edge = test_db.execute(
            "SELECT edge_type FROM ohm_edges WHERE from_node = ? AND to_node = ?",
            [BEADS_BACKLOG_ANCHOR_ID, "beads_ohm_test1"],
        ).fetchone()
        assert edge is not None
        assert edge[0] == "REFERENCES"

    def test_updates_existing_task(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(title="Old Title")])
        report = sync_beads_to_ohm_tasks(test_db, [_make_issue(title="New Title")])
        assert report["updated"] == 1
        assert report["created"] == 0

        row = test_db.execute(
            "SELECT label FROM ohm_nodes WHERE id = ?",
            ["beads_ohm_test1"],
        ).fetchone()
        assert row[0] == "New Title"

    def test_does_not_regress_task_status(self, test_db):
        # First sync as open
        sync_beads_to_ohm_tasks(test_db, [_make_issue(status="open")])
        # Manually advance to done
        test_db.execute(
            "UPDATE ohm_nodes SET task_status = 'done' WHERE id = ?",
            ["beads_ohm_test1"],
        )
        # Re-sync as open — should NOT regress to open
        sync_beads_to_ohm_tasks(test_db, [_make_issue(status="open")])
        row = test_db.execute(
            "SELECT task_status FROM ohm_nodes WHERE id = ?",
            ["beads_ohm_test1"],
        ).fetchone()
        assert row[0] == "done"

    def test_advances_status_forward(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(status="open")])
        sync_beads_to_ohm_tasks(test_db, [_make_issue(status="closed")])
        row = test_db.execute(
            "SELECT task_status FROM ohm_nodes WHERE id = ?",
            ["beads_ohm_test1"],
        ).fetchone()
        assert row[0] == "done"

    def test_priority_mapped(self, test_db):
        for bd_p, ohm_p in BD_PRIORITY_MAP.items():
            issue = _make_issue(id=f"OHM-p{bd_p}", priority=bd_p)
            sync_beads_to_ohm_tasks(test_db, [issue])
            row = test_db.execute(
                "SELECT priority FROM ohm_nodes WHERE id = ?",
                [f"beads_ohm_p{bd_p}"],
            ).fetchone()
            assert row[0] == ohm_p

    def test_status_mapped(self, test_db):
        for bd_s, ohm_s in BD_STATUS_MAP.items():
            issue = _make_issue(id=f"OHM-s{bd_s}", status=bd_s)
            sync_beads_to_ohm_tasks(test_db, [issue])
            row = test_db.execute(
                "SELECT task_status FROM ohm_nodes WHERE id = ?",
                [f"beads_ohm_s{bd_s}"],
            ).fetchone()
            assert row[0] == ohm_s

    def test_labels_stored_as_tags(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(labels=["alpha", "beta"])])
        row = test_db.execute(
            "SELECT tags FROM ohm_nodes WHERE id = ?",
            ["beads_ohm_test1"],
        ).fetchone()
        tags = json.loads(row[0])
        assert "alpha" in tags
        assert "beta" in tags

    def test_metadata_contains_beads_id(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue()])
        row = test_db.execute(
            "SELECT metadata FROM ohm_nodes WHERE id = ?",
            ["beads_ohm_test1"],
        ).fetchone()
        meta = json.loads(row[0])
        assert meta["beads_id"] == "OHM-test1"
        assert meta["beads_issue_type"] == "task"

    def test_idempotent_sync(self, test_db):
        issue = _make_issue()
        r1 = sync_beads_to_ohm_tasks(test_db, [issue])
        r2 = sync_beads_to_ohm_tasks(test_db, [issue])
        assert r1["created"] == 1
        assert r2["created"] == 0
        assert r2["skipped"] == 1

    def test_multiple_issues(self, test_db):
        issues = [
            _make_issue(id="OHM-a", assignee="metis@olympus.local"),
            _make_issue(id="OHM-b", assignee="atlas@olympus.local"),
            _make_issue(id="OHM-c", assignee=None),
        ]
        report = sync_beads_to_ohm_tasks(test_db, issues)
        assert report["created"] == 2
        assert report["skipped"] == 1
        assert report["total"] == 3


pytestmark = pytest.mark.integration

from tests.conftest import _request  # noqa: E402


@pytest.mark.xdist_group("server")
class TestSyncBeadsHTTP:
    """HTTP integration tests for POST /admin/sync-beads."""

    def test_sync_with_explicit_issues(self, test_server):
        port, _ = test_server
        issues = [_make_issue(id="OHM-http1", title="HTTP Sync Test")]
        status, data = _request("POST", port, "/admin/sync-beads", body={"issues": issues})
        assert status == 200, data
        assert data["created"] == 1
        assert data["total"] == 1

    def test_sync_idempotent_via_http(self, test_server):
        port, store = test_server
        issues = [_make_issue(id="OHM-http2", title="Idempotent Test")]
        # First sync
        status, data = _request("POST", port, "/admin/sync-beads", body={"issues": issues})
        assert status == 200
        assert data["created"] == 1
        # Second sync — no fields changed, so it's skipped (OHM-sbtz.1)
        status, data = _request("POST", port, "/admin/sync-beads", body={"issues": issues})
        assert status == 200
        assert data["created"] == 0
        assert data["skipped"] == 1

    def test_sync_task_appears_in_tasks_endpoint(self, test_server):
        port, store = test_server
        issues = [_make_issue(id="OHM-http3", assignee="test_agent@olympus.local")]
        _request("POST", port, "/admin/sync-beads", body={"issues": issues})
        # The task should now appear in GET /tasks?assigned_to=test_agent
        status, data = _request("GET", port, "/tasks?assigned_to=test_agent")
        assert status == 200
        task_ids = [t.get("id") for t in data.get("tasks", data) if isinstance(t, dict)]
        assert "beads_ohm_http3" in task_ids

    def test_sync_skips_unassigned(self, test_server):
        port, _ = test_server
        issues = [_make_issue(id="OHM-http4", assignee=None)]
        status, data = _request("POST", port, "/admin/sync-beads", body={"issues": issues})
        assert status == 200
        assert data["skipped"] == 1
        assert data["created"] == 0


class TestFetchBeadsIssuesAssigneeEnrichment:
    """OHM-sbtz: fetch_beads_issues enriches bd list output with assignee from JSONL."""

    def test_enriches_assignee_from_jsonl(self, tmp_path, monkeypatch):
        import os

        from ohm.integrations import beads_sync

        # Create a fake JSONL with assignee
        jsonl_path = tmp_path / ".beads" / "issues.jsonl"
        jsonl_path.parent.mkdir(parents=True)
        jsonl_path.write_text(json.dumps({"_type": "issue", "id": "OHM-xxx", "assignee": "metis", "status": "open"}) + "\n")

        # Mock bd list --json to return issues WITHOUT assignee
        class MockResult:
            returncode = 0
            stdout = json.dumps([{"id": "OHM-xxx", "title": "Test", "status": "open"}])

        def mock_run(*args, **kwargs):
            return MockResult()

        monkeypatch.setattr(beads_sync.subprocess, "run", mock_run)
        monkeypatch.chdir(tmp_path)

        issues = beads_sync.fetch_beads_issues()
        assert len(issues) == 1
        assert issues[0]["assignee"] == "metis"

    def test_bd_list_unavailable_falls_back_to_jsonl(self, tmp_path, monkeypatch):
        from ohm.integrations import beads_sync

        jsonl_path = tmp_path / ".beads" / "issues.jsonl"
        jsonl_path.parent.mkdir(parents=True)
        jsonl_path.write_text(json.dumps({"_type": "issue", "id": "OHM-yyy", "assignee": "clio", "status": "open"}) + "\n")

        def mock_run(*args, **kwargs):
            raise FileNotFoundError("bd not found")

        monkeypatch.setattr(beads_sync.subprocess, "run", mock_run)
        monkeypatch.chdir(tmp_path)

        issues = beads_sync.fetch_beads_issues()
        assert len(issues) == 1
        assert issues[0]["assignee"] == "clio"

    def test_jsonl_without_assignee_still_works(self, tmp_path, monkeypatch):
        from ohm.integrations import beads_sync

        jsonl_path = tmp_path / ".beads" / "issues.jsonl"
        jsonl_path.parent.mkdir(parents=True)
        jsonl_path.write_text(json.dumps({"_type": "issue", "id": "OHM-noassignee", "status": "open"}) + "\n")

        class MockResult:
            returncode = 0
            stdout = json.dumps([{"id": "OHM-noassignee", "title": "No Assignee", "status": "open"}])

        monkeypatch.setattr(beads_sync.subprocess, "run", lambda *a, **k: MockResult())
        monkeypatch.chdir(tmp_path)

        issues = beads_sync.fetch_beads_issues()
        assert len(issues) == 1
        # No assignee in either source → field is None/absent, sync will skip
        assert not issues[0].get("assignee")
