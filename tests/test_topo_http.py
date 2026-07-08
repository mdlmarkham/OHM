"""Tests for TOPO temporal HTTP endpoints (commit 2326f81).

Spawns a real ohmd with --schema topo --no-auth and exercises:
- GET /plans
- GET /reports
- GET /runs
- GET /rul
- GET /timeline/{ancestor_id}
- GET /report/{id}
- GET /run/{id}

These endpoints are thin wrappers around ohm.queries. The goal is to
verify routing, parameter plumbing, and response shape, not to fully
populate TOPO data.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _safe_json(s: str):
    import json

    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {}


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode()
            try:
                return resp.status, _safe_json(data)
            except ValueError:
                return resp.status, data
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@contextlib.contextmanager
def _spawn_topo_ohmd(tmp_path: Path):
    """Spawn ohmd with the TOPO schema for testing temporal endpoints."""
    port = _free_port()
    db_path = tmp_path / "topo_http.duckdb"
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["OHM_NO_AUTH"] = "true"
    env.setdefault("OHM_ACTOR", "topo_http_test")

    repo_root = Path(__file__).resolve().parents[1]
    stderr_log = tmp_path / "ohmd.stderr.log"
    stderr_fh = stderr_log.open("wb")

    cmd = [
        sys.executable,
        "-m",
        "ohm.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--db",
        str(db_path),
        "--schema",
        "topo",
        "--no-auth",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        cwd=str(repo_root),
        env=env,
    )
    try:
        yield base_url, proc, stderr_log
    finally:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
        stderr_fh.close()


def _wait_for_health(base_url: str, deadline_s: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            status, _ = _http_get(f"{base_url}/health", timeout=1.0)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


@pytest.fixture(scope="module")
def topo_ohmd(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("topo_ohmd")
    with _spawn_topo_ohmd(tmp_path) as (base_url, proc, stderr_log):
        if not _wait_for_health(base_url, deadline_s=30.0):
            try:
                err = stderr_log.read_bytes()[-4000:].decode(errors="replace")
            except Exception:
                err = "<could not read stderr>"
            pytest.fail(f"topo ohmd did not become healthy\n{err}")
        yield base_url


def test_get_plans(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/plans")
    assert status == 200, f"/plans failed: {body}"
    assert body.get("ok") is True
    assert "data" in body


def test_get_reports(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/reports")
    assert status == 200, f"/reports failed: {body}"
    assert body.get("ok") is True
    assert "data" in body


def test_get_runs(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/runs")
    assert status == 200, f"/runs failed: {body}"
    assert body.get("ok") is True
    assert "data" in body


def test_get_rul(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/rul")
    assert status == 200, f"/rul failed: {body}"
    assert body.get("ok") is True
    assert "data" in body


def test_get_timeline_rollup(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/timeline/some-ancestor")
    # Empty topo DB will return no rows; endpoint should still 200.
    assert status == 200, f"/timeline failed: {body}"
    assert body.get("ok") is True
    assert "data" in body


def test_get_report_not_found(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/report/nonexistent")
    assert status == 404, f"missing report should 404: {body}"
    payload = _safe_json(body) if isinstance(body, str) else body
    assert payload.get("ok") is False


def test_get_run_not_found(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/run/nonexistent")
    assert status == 404, f"missing run should 404: {body}"
    payload = _safe_json(body) if isinstance(body, str) else body
    assert payload.get("ok") is False


def test_get_plans_with_filters(topo_ohmd):
    status, body = _http_get(f"{topo_ohmd}/plans?node_id=n1&plan_type=maintenance&status=active&horizon=30d")
    assert status == 200, f"/plans with filters failed: {body}"
    assert body.get("ok") is True
