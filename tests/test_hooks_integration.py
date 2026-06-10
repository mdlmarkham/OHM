"""End-to-end integration tests for OHM hook system (OHM-aznh.13).

Tests the full hook lifecycle against a real HTTP server:
  1. pre_ingest hook exits 0: POST /node succeeds
  2. pre_ingest hook exits 1: POST /node returns 422 hook_rejected
  3. pre_ingest hook exits 1: POST /edge returns 422
  4. post_ingest hook stdout: response includes hook_decorations
  5. post_ingest hook fails: response still succeeds
  6. pre_ingest hook times out: 422 with timed_out=True
  7. python: prefix hook: in-process callable
  8. Multiple pre_ingest hooks: all must pass
  9. No hooks registered: normal operation

All tests run against a real HTTP server with a temp database.
"""

import json
import sys
import threading
from http.client import HTTPConnection

import pytest


def _can_fork_sh():
    """Check if the current environment can fork child processes via /bin/sh.

    Returns False if the OHM sandbox is active (RLIMIT_NPROC=0 prevents forking)
    or if /bin/sh cannot fork for other reasons.
    """
    import os as _os

    # If sandbox is active, forking is intentionally disabled (NPROC=0)
    if _os.environ.get("OHM_SANDBOX_DISABLE", "") not in ("1", "true", "yes"):
        return False
    try:
        import subprocess

        result = subprocess.run(["sh", "-c", "sleep 0.1"], timeout=2, capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


from pathlib import Path

import pytest

from ohm.schema import DEFAULT_SCHEMA
from ohm.server import OhmHandler
from ohm.store import OhmStore


def _start_test_server(store, no_auth=True):
    import socketserver

    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.schema_config = DEFAULT_SCHEMA
    OhmHandler.tokens = {}
    OhmHandler.roles = {}
    OhmHandler.no_auth = no_auth
    OhmHandler.require_read_auth = False
    OhmHandler.multi_tenant = False

    server = socketserver.TCPServer(("127.0.0.1", 0), OhmHandler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from tests.conftest import wait_for_port

    wait_for_port("127.0.0.1", port)
    return port, server, thread


def _request(method, port, path, body=None, headers=None, token=None):
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=10)
    hdrs = headers or {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = None
    conn.request(method, path, body=body_bytes, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except json.JSONDecodeError:
        return resp.status, data


@pytest.fixture
def hook_server(tmp_path):
    db_path = str(tmp_path / "hook_test.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test")
    port, server, thread = _start_test_server(store)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


def _register_hook(port, event, command, timeout_ms=5000):
    return _request(
        "POST",
        port,
        "/hooks",
        body={
            "event": event,
            "command": command,
            "timeout_ms": timeout_ms,
        },
    )


def _cleanup_hooks(port):
    status, data = _request("GET", port, "/hooks")
    if status == 200:
        for h in data.get("hooks", []):
            _request("DELETE", port, f"/hooks/{h['id']}")


@pytest.mark.xdist_group("server")
class TestHookIntegration:
    """Full integration tests for hook lifecycle (OHM-aznh.13)."""

    def test_scenario1_pre_ingest_pass_node_succeeds(self, hook_server):
        port, _ = hook_server
        _register_hook(port, "pre_ingest", "echo ok")
        status, _ = _request("POST", port, "/node", body={"id": "s1", "label": "test", "type": "concept"})
        assert status == 201
        _cleanup_hooks(port)

    def test_scenario2_pre_ingest_fail_node_returns_422(self, hook_server):
        port, _ = hook_server
        _register_hook(port, "pre_ingest", "/bin/false")
        status, data = _request("POST", port, "/node", body={"id": "s2", "label": "rejected", "type": "concept"})
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert data["exit_code"] == 1
        _cleanup_hooks(port)

    def test_scenario3_pre_ingest_fail_edge_returns_422(self, hook_server):
        port, _ = hook_server
        _request("POST", port, "/node", body={"id": "from3", "label": "from", "type": "concept"})
        _request("POST", port, "/node", body={"id": "to3", "label": "to", "type": "concept"})
        _register_hook(port, "pre_ingest", "exit 1")
        status, data = _request("POST", port, "/edge", body={"from": "from3", "to": "to3", "type": "SUPPORTS", "layer": "L3"})
        assert status == 422
        assert data["error"] == "hook_rejected"
        _cleanup_hooks(port)

    @pytest.mark.skipif(not _can_fork_sh(), reason="Environment cannot fork via /bin/sh")
    def test_scenario4_post_ingest_stdout_decorates_response(self, hook_server):
        port, _ = hook_server
        cmd = 'python3 -c "import sys,json; sys.stdout.write(json.dumps({chr(100)+chr(101)+chr(99)+chr(111)+chr(114)+chr(97)+chr(116)+chr(101)+chr(100): True}))"'
        _register_hook(port, "post_ingest", cmd)
        status, data = _request("POST", port, "/node", body={"id": "s4", "label": "decorated", "type": "concept"})
        assert status == 201
        assert data.get("hook_decorations", {}).get("decorated") is True
        _cleanup_hooks(port)

    def test_scenario5_post_ingest_failure_still_succeeds(self, hook_server):
        port, _ = hook_server
        _register_hook(port, "post_ingest", "exit 1")
        status, data = _request("POST", port, "/node", body={"id": "s5", "label": "still works", "type": "concept"})
        assert status == 201
        _cleanup_hooks(port)

    @pytest.mark.skipif(not _can_fork_sh(), reason="Environment cannot fork via /bin/sh")
    def test_scenario6_pre_ingest_timeout_returns_422(self, hook_server):
        port, _ = hook_server
        if sys.platform == "win32":
            cmd = "ping -n 10 127.0.0.1"
        else:
            cmd = "sleep 10"
        _register_hook(port, "pre_ingest", cmd, timeout_ms=200)
        status, data = _request("POST", port, "/node", body={"id": "s6", "label": "timeout", "type": "concept"})
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert data["timed_out"] is True
        _cleanup_hooks(port)

    def test_scenario7_python_prefix_hook(self, hook_server):
        port, _ = hook_server
        import types

        mod = types.ModuleType("_inttest_hook_mod")
        mod.check_hook = lambda payload: (0, "checked", "")
        sys.modules["_inttest_hook_mod"] = mod
        try:
            _register_hook(port, "pre_ingest", "python:_inttest_hook_mod.check_hook")
            status, _ = _request("POST", port, "/node", body={"id": "s7", "label": "python hook", "type": "concept"})
            assert status == 201
        finally:
            del sys.modules["_inttest_hook_mod"]
            _cleanup_hooks(port)

    def test_scenario8_multiple_pre_ingest_hooks_all_must_pass(self, hook_server):
        port, _ = hook_server
        _register_hook(port, "pre_ingest", "echo first")
        _register_hook(port, "pre_ingest", "echo second")
        status, _ = _request("POST", port, "/node", body={"id": "s8a", "label": "all pass", "type": "concept"})
        assert status == 201
        _cleanup_hooks(port)

        _register_hook(port, "pre_ingest", "echo first")
        _register_hook(port, "pre_ingest", "exit 1")
        status, data = _request("POST", port, "/node", body={"id": "s8b", "label": "second fails", "type": "concept"})
        assert status == 422
        assert data["error"] == "hook_rejected"
        _cleanup_hooks(port)

    def test_scenario9_no_hooks_normal_operation(self, hook_server):
        port, _ = hook_server
        status, _ = _request("POST", port, "/node", body={"id": "s9", "label": "no hooks", "type": "concept"})
        assert status == 201
        status2, _ = _request("POST", port, "/node", body={"id": "s9b", "label": "also no hooks", "type": "concept"})
        assert status2 == 201
