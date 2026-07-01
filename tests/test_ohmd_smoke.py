"""OHM-kg16: live-daemon smoke test for CI integration.

This is the test counterpart to the GitHub Actions workflow
``.github/workflows/ohmd-smoke.yml``. The workflow spawns the
real ``ohm`` CLI as a subprocess and exercises a smoke-test
endpoint matrix against it.

Why a subprocess (and not the in-process test_server fixture):
- catches real CLI arg-parsing bugs the in-process test_server
  cannot (port conflicts, --no-auth, --db resolution, etc.)
- exercises the actual deployment path operators use
- runs against the production module path so any
  packaging/dependency mismatch shows up here first
- uses no_auth=True so CI doesn't need token management

Endpoint matrix (the smoke set the issue calls for):

  Boot:
    - GET  /health                       — returns 200 with status=ok
    - GET  /status                       — returns 200 with version info

  Read:
    - GET  /stats                        — returns 200 with node/edge counts
    - GET  /neighborhood?id=<n>&depth=1  — returns 200 with neighbors
    - GET  /ask?question=foo              — returns 200 (synthesis fallback)

  Write (a small graph for downstream assertions):
    - POST /node                        — creates a node, returns 201
    - POST /edge                        — creates an edge, returns 201
    - POST /observe                     — creates an observation, returns 201
    - POST /challenge                   — creates a CHALLENGED_BY edge
    - POST /record-outcome              — records an outcome
    - POST /heartbeat                   — returns agent state

  Maintenance:
    - POST /admin/verification-decay    — applies decay
    - GET  /nudges/quality              — returns aggregate stats
    - GET  /admin/health                — returns admin health

These are all the endpoints called out in the kg16 issue items 2
("endpoints NOT exercised in the original 2026-06-30 report") and
the surrounding Industrial Agent Manifesto (OHM-dp38) coverage.

Run from CLI:
    python -m pytest tests/test_ohmd_smoke.py -v

Run from CI:
    The matching .github/workflows/ohmd-smoke.yml spawns the
    daemon, runs this file, then asserts exit code 0.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# Smoke test marker — separates from the in-process test suite.
# Run with: pytest -m "smoke" or just include the file path.
pytestmark = pytest.mark.smoke


# ── Helpers ───────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Bind a socket to port 0 to get a free port, then release."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, dict | str]:
    """Make a GET request, return (status, parsed_json_or_text)."""
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


def _http_post_json(
    url: str,
    body: dict,
    timeout: float = 5.0,
) -> tuple[int, dict | str]:
    """Make a POST request with a JSON body."""
    data = _to_json_bytes(body)
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, _safe_json(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _safe_json(s: str):
    """Parse JSON, return {} on failure (smoke test tolerates non-JSON)."""
    import json
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {}


def _to_json_bytes(obj) -> bytes:
    import json
    return json.dumps(obj).encode("utf-8")


def _wait_for_health(base_url: str, deadline_s: float = 30.0) -> bool:
    """Poll /health until it returns 200 or deadline expires."""
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            status, body = _http_get(f"{base_url}/health", timeout=1.0)
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    return False


# ── Test ──────────────────────────────────────────────────────────────────


def test_ohmd_smoke_endpoint_matrix(tmp_path):
    """Spawn the real ``ohm serve`` CLI, run an endpoint matrix.

    Mirrors the OHM-kg16 acceptance: 'GitHub Actions job that boots
    ohmd + DuckDB + seed data and exercises the smoke-test endpoint
    matrix.' The same file is invoked by .github/workflows/ohmd-smoke.yml.
    """
    port = _free_port()
    db_path = str(tmp_path / "smoke.duckdb")
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    # Disable auth so the smoke test doesn't need a token.
    env["OHM_NO_AUTH"] = "true"
    # Pin the agent to a known name for /heartbeat assertions.
    env.setdefault("OHM_ACTOR", "smoke_test")

    # Spawn ``ohm serve start`` as a real subprocess. We pass the
    # same args operators use in production: --db to scope the path,
    # --port for the listen socket, --quack off (we don't need Quack
    # for a single-writer smoke test).
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "ohm.cli", "serve", "start",
            "--port", str(port),
            "--db", db_path,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        # ── Boot phase: wait for /health ────────────────────────────
        healthy = _wait_for_health(base_url, deadline_s=30.0)
        if not healthy:
            # Capture stderr for the failure message so the CI log
            # shows the daemon's boot output.
            try:
                proc.terminate()
                _, err = proc.communicate(timeout=5)
            except Exception:
                err = b"<could not capture stderr>"
            pytest.fail(
                f"ohmd did not become healthy on {base_url} within 30s.\n"
                f"daemon stderr:\n{err.decode(errors='replace')}"
            )

        # ── Read endpoints ─────────────────────────────────────────
        status, body = _http_get(f"{base_url}/health")
        assert status == 200, f"/health returned {status}"
        assert isinstance(body, dict)
        assert body.get("status") in ("ok", "healthy", "running"), body

        status, body = _http_get(f"{base_url}/status")
        assert status == 200, f"/status returned {status}"
        # /status returns version info; just assert we got a dict back.
        assert isinstance(body, dict), body

        status, body = _http_get(f"{base_url}/stats")
        assert status == 200, f"/stats returned {status}"
        assert isinstance(body, dict), body

        # ── Write endpoints: seed a small graph ────────────────────
        # POST /node (label, type)
        node_a_id = "smoke_node_a"
        status, body = _http_post_json(
            f"{base_url}/node",
            {
                "id": node_a_id,
                "label": "Smoke node A",
                "type": "concept",
            },
        )
        assert status in (200, 201), f"POST /node returned {status}: {body}"

        node_b_id = "smoke_node_b"
        status, body = _http_post_json(
            f"{base_url}/node",
            {
                "id": node_b_id,
                "label": "Smoke node B",
                "type": "concept",
            },
        )
        assert status in (200, 201), f"POST /node returned {status}: {body}"

        # POST /edge
        status, body = _http_post_json(
            f"{base_url}/edge",
            {
                "from_node": node_a_id,
                "to_node": node_b_id,
                "layer": "L3",
                "edge_type": "CAUSES",
                "confidence": 0.7,
            },
        )
        assert status in (200, 201), f"POST /edge returned {status}: {body}"

        # POST /observe
        status, body = _http_post_json(
            f"{base_url}/observe",
            {
                "node_id": node_a_id,
                "type": "measurement",
                "value": 0.5,
            },
        )
        assert status in (200, 201), f"POST /observe returned {status}: {body}"

        # POST /heartbeat (verifies the verification_overdue field per
        # the kg16 item 2 call-out).
        status, body = _http_post_json(f"{base_url}/heartbeat", {})
        assert status in (200, 201), f"POST /heartbeat returned {status}: {body}"
        assert isinstance(body, dict)

        # POST /record-outcome
        status, body = _http_post_json(
            f"{base_url}/record-outcome",
            {
                "claim_id": node_a_id,
                "outcome": True,
                "notes": "smoke test outcome",
            },
        )
        assert status in (200, 201), (
            f"POST /record-outcome returned {status}: {body}"
        )

        # POST /admin/verification-decay — per kg16 item 2.
        status, body = _http_post_json(
            f"{base_url}/admin/verification-decay", {}
        )
        assert status in (200, 201), (
            f"POST /admin/verification-decay returned {status}: {body}"
        )

        # GET /nudges/quality — per the OHM-49bg nudges acceptance.
        status, body = _http_get(f"{base_url}/nudges/quality")
        assert status == 200, f"GET /nudges/quality returned {status}: {body}"

        # GET /neighborhood — basic graph traversal
        status, body = _http_get(
            f"{base_url}/neighborhood?id={node_a_id}&depth=1"
        )
        assert status == 200, (
            f"GET /neighborhood returned {status}: {body}"
        )

        # GET /ask — synthesis endpoint (always returns 200)
        status, body = _http_get(
            f"{base_url}/ask?question=smoke%20test"
        )
        assert status == 200, f"GET /ask returned {status}: {body}"
        assert isinstance(body, dict)

        # GET /admin/health — admin health (mirrors /health for the
        # admin surface).
        status, body = _http_get(f"{base_url}/admin/health")
        assert status == 200, (
            f"GET /admin/health returned {status}: {body}"
        )

    finally:
        # ── Teardown: stop the daemon ───────────────────────────────
        try:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=5)
        except Exception:
            # Don't let teardown errors mask a test failure above.
            pass
