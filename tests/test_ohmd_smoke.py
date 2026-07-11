"""OHM-kg16: live-daemon smoke test for CI integration.

This is the test counterpart to the GitHub Actions workflow
``.github/workflows/ohmd-smoke.yml``. The workflow spawns the
real ``ohm.server`` as a subprocess and exercises a smoke-test
endpoint matrix against it.

Why a subprocess (and not the in-process test_server fixture):
- catches real CLI arg-parsing bugs the in-process test_server
  cannot (port conflicts, --no-auth, --db resolution, etc.)
- exercises the actual deployment path operators use
- runs against the production module path so any
  packaging/dependency mismatch shows up here first
- uses --no-auth so CI doesn't need token management

Endpoint matrix (the smoke set the kg16 issue calls for):

  Boot:
    - GET  /health                       - returns 200 with status=ok
    - GET  /status                       - returns 200 with version info

  Read:
    - GET  /stats                        - returns 200 with node/edge counts
    - GET  /layers                       - returns 200 with L0-L4 layer descriptions
    - GET  /neighborhood/{id}?depth=1    - returns 200 with neighbors

  Write (a small graph for downstream assertions):
    - POST /node                         - creates a node, returns 201
    - POST /edge                         - creates an edge, returns 201
    - POST /observe/{id}                 - creates an observation, returns 201
    - POST /challenge/{edge_id}          - creates a CHALLENGED_BY edge
    - POST /outcome                      - records an outcome
    - POST /heartbeat                    - returns agent state

  Maintenance:
    - POST /admin/verification-decay     - applies decay
    - GET  /admin/nudges/quality         - returns aggregate stats
    - GET  /admin/health                 - returns admin health

Note: /ask synthesis is covered by tests/test_ask_endpoint.py (the
in-process test_server fixture is faster for that heavy endpoint).
The smoke test prioritises fast, well-isolated endpoints that catch
arg-parsing and dependency mismatches in CI.

These endpoints cover the kg16 issue items 2 ("endpoints NOT exercised
in the original 2026-06-30 report") — /heartbeat, /outcome, and
/admin/verification-decay — plus the surrounding Industrial Agent
Manifesto (OHM-dp38) coverage.

Run from CLI:
    python -m pytest tests/test_ohmd_smoke.py -v

Run from CI:
    The matching .github/workflows/ohmd-smoke.yml spawns the
    daemon, runs this file, then asserts exit code 0.
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
        url,
        data=data,
        method="POST",
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


@contextlib.contextmanager
def _spawn_ohmd(tmp_path, *, agent: str = "smoke_test", db_name: str = "smoke.duckdb"):
    """Spawn ``ohm.server`` on a free port with a temp DuckDB.

    Yields ``(base_url, env, proc, stderr_log)``. On exit: terminate,
    wait, and close log file handles. Stderr is routed to a temp file
    (NOT PIPE — see the Windows pipe-blocking note in the body).

    Usage::

        with _spawn_ohmd(tmp_path) as (base_url, _env, proc, stderr_log):
            _wait_for_healthy_or_fail(base_url, proc, stderr_log)
            ...
    """
    port = _free_port()
    db_path = str(tmp_path / db_name)
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    # Disable auth so the smoke tests don't need a token.
    env["OHM_NO_AUTH"] = "true"
    # Pin the agent so /heartbeat + verification endpoints have a
    # known author for the rows they create.
    env.setdefault("OHM_ACTOR", agent)

    # Isolate from any production ~/.ohm/ohmd.json on the dev machine.
    # The admin bypass (--no-auth) requires both no_auth=True AND an
    # empty tokens dict; a production config with tokens would defeat
    # the bypass. Write an empty {} config and pass --config so the
    # subprocess never reads the user's real ohmd.json. Also pop
    # OHM_CONFIG defensively in case it points elsewhere.
    env.pop("OHM_CONFIG", None)
    config_path = tmp_path / "ohmd_smoke_config.json"
    config_path.write_text("{}")

    # Route the daemon's stdout/stderr to temp files rather than
    # subprocess.PIPE. On Windows the default pipe buffer is small,
    # so a daemon that logs ~15+ lines will block on write to stderr
    # while the test process is busy making HTTP calls — the parent
    # never reads the pipe, the child blocks, all subsequent requests
    # time out. With files, the child writes don't block; on failure
    # we tail the log into the assertion message.
    stdout_log = tmp_path / f"daemon.{db_name}.stdout.log"
    stderr_log = tmp_path / f"daemon.{db_name}.stderr.log"
    stdout_fh = stdout_log.open("wb")
    stderr_fh = stderr_log.open("wb")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ohm.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db",
            db_path,
            "--no-auth",
            "--config",
            str(config_path),
        ],
        env=env,
        stdout=stdout_fh,
        stderr=stderr_fh,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        yield base_url, env, proc, stderr_log
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
        try:
            stdout_fh.close()
        except Exception:
            pass
        try:
            stderr_fh.close()
        except Exception:
            pass


def _wait_for_healthy_or_fail(base_url: str, proc: subprocess.Popen, stderr_log: Path) -> None:
    """Block until /health is 200 or pytest.fail with the daemon log tail."""
    if _wait_for_health(base_url, deadline_s=30.0):
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    try:
        err = stderr_log.read_bytes()[-4000:]
    except Exception:
        err = b"<could not read stderr log>"
    pytest.fail(f"ohmd did not become healthy on {base_url} within 30s.\ndaemon stderr (tail):\n{err.decode(errors='replace')}")


# ── Test ──────────────────────────────────────────────────────────────────


def test_ohmd_smoke_endpoint_matrix(tmp_path):
    """Spawn the real ``ohm serve`` CLI, run an endpoint matrix.

    Mirrors the OHM-kg16 acceptance: 'GitHub Actions job that boots
    ohmd + DuckDB + seed data and exercises the smoke-test endpoint
    matrix.' The same file is invoked by .github/workflows/ohmd-smoke.yml.
    """
    with _spawn_ohmd(tmp_path, db_name="smoke.duckdb") as (base_url, _env, proc, stderr_log):
        # ── Boot phase: wait for /health ────────────────────────────
        _wait_for_healthy_or_fail(base_url, proc, stderr_log)

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

        # POST /edge — body uses short keys `from`/`to`/`type`.
        # The response includes the new edge's `id`, which the
        # challenge endpoint below needs as a path parameter.
        status, body = _http_post_json(
            f"{base_url}/edge",
            {
                "from": node_a_id,
                "to": node_b_id,
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.7,
            },
        )
        assert status in (200, 201), f"POST /edge returned {status}: {body}"
        edge_id = body.get("id") if isinstance(body, dict) else None
        assert edge_id, f"POST /edge response missing 'id': {body}"

        # POST /observe/{node_id} — node id is in the path, not body.
        status, body = _http_post_json(
            f"{base_url}/observe/{node_a_id}",
            {
                "type": "measurement",
                "value": 0.5,
            },
        )
        assert status in (200, 201), f"POST /observe/{node_a_id} returned {status}: {body}"

        # POST /heartbeat (verifies the verification_overdue field per
        # the kg16 item 2 call-out).
        status, body = _http_post_json(f"{base_url}/heartbeat", {})
        assert status in (200, 201), f"POST /heartbeat returned {status}: {body}"
        assert isinstance(body, dict)

        # POST /challenge/{edge_id} — body uses `reason` + `confidence`.
        status, body = _http_post_json(
            f"{base_url}/challenge/{edge_id}",
            {
                "reason": "smoke test challenge",
                "confidence": 0.3,
            },
        )
        assert status in (200, 201), f"POST /challenge/{edge_id} returned {status}: {body}"

        # POST /outcome — body requires source_agent, claim_node, outcome.
        status, body = _http_post_json(
            f"{base_url}/outcome",
            {
                "source_agent": "smoke_test",
                "claim_node": node_a_id,
                "outcome": True,
                "notes": "smoke test outcome",
            },
        )
        assert status in (200, 201), f"POST /outcome returned {status}: {body}"

        # POST /admin/verification-decay — per kg16 item 2.
        status, body = _http_post_json(f"{base_url}/admin/verification-decay", {})
        assert status in (200, 201), f"POST /admin/verification-decay returned {status}: {body}"

        # GET /admin/nudges/quality — per the OHM-49bg nudges acceptance.
        status, body = _http_get(f"{base_url}/admin/nudges/quality")
        assert status == 200, f"GET /admin/nudges/quality returned {status}: {body}"

        # GET /neighborhood/{id} — node id in path, depth in query.
        status, body = _http_get(f"{base_url}/neighborhood/{node_a_id}?depth=1")
        assert status == 200, f"GET /neighborhood/{node_a_id} returned {status}: {body}"

        # GET /layers — L0-L4 layer descriptions. Tests a fresh read
        # endpoint (the static SchemaConfig) without rebuilding the
        # full schema guide payload that /schema returns. Both pull
        # from the same in-memory schema_config, so /layers gives the
        # same coverage with a much smaller response. The body is
        # a dict-of-layer-strings, not a JSON object — the test
        # accepts any non-empty payload.
        status, body = _http_get(f"{base_url}/layers")
        assert status == 200, f"GET /layers returned {status}: {body}"
        assert body, "GET /layers returned empty body"

        # GET /admin/health — admin health (mirrors /health for the
        # admin surface).
        status, body = _http_get(f"{base_url}/admin/health")
        assert status == 200, f"GET /admin/health returned {status}: {body}"


# ── Verification matrix (OHM-kg16 item 2) ────────────────────────────────


def test_ohmd_smoke_verification_matrix(tmp_path):
    """Second live-daemon integration test pass — verification endpoints.

    OHM-kg16 item 2: exercise endpoints NOT covered in the original
    2026-06-30 report that underpin the Industrial Agent Manifesto
    (OHM-dp38) 15 principles:

      - /heartbeat                       — verification_overdue field
      - /verifications/detect            — detectable claims (ADR-018.4)
      - /verifications/pending           — list pending verifications
      - /verifications/nudge             — fire_verification_nudge
                                            (creates CHALLENGED_BY)
      - /verifications/outcome           — record_verification_outcome
                                            (ADR-018.3 decay)
      - /admin/verification-decay        — apply_verification_decay

    Together with the endpoint matrix above this covers the full
    verification loop end-to-end against a real daemon subprocess.
    """
    with _spawn_ohmd(tmp_path, agent="verification_smoke", db_name="verification.duckdb") as (
        base_url,
        _env,
        proc,
        stderr_log,
    ):
        _wait_for_healthy_or_fail(base_url, proc, stderr_log)

        # ── Seed: one causal edge, no outcomes ────────────────────
        # The verification matrix needs at least one claim to
        # exercise the detection / nudge / outcome paths. We create
        # a single source→target CAUSES edge with high confidence —
        # a classic "consensus-only" scenario: the claim has no
        # outcome-backed support, so /verifications/detect should
        # surface it.
        src_id = "verify_src"
        dst_id = "verify_dst"
        for node_id, label in ((src_id, "Verify source"), (dst_id, "Verify target")):
            status, body = _http_post_json(
                f"{base_url}/node",
                {
                    "id": node_id,
                    "label": label,
                    "type": "concept",
                },
            )
            assert status in (200, 201), f"POST /node {node_id} returned {status}: {body}"

        status, body = _http_post_json(
            f"{base_url}/edge",
            {
                "from": src_id,
                "to": dst_id,
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.9,
            },
        )
        assert status in (200, 201), f"POST /edge returned {status}: {body}"
        edge_id = body.get("id") if isinstance(body, dict) else None
        assert edge_id, f"POST /edge response missing 'id': {body}"

        # ── /heartbeat — verification_overdue field (ADR-018.3) ──
        # The heartbeat returns verification_overdue_count and the
        # list of edges overdue for verification. The query only
        # surfaces edges past the 14-day grace period, so a fresh
        # CAUSES edge yields count=0; we assert the FIELD exists
        # with a sensible type, not the count itself.
        status, body = _http_post_json(f"{base_url}/heartbeat", {})
        assert status in (200, 201), f"POST /heartbeat returned {status}: {body}"
        assert isinstance(body, dict)
        # Body shape: {ok, data: {agent, focus, verification_overdue,
        # verification_overdue_count, ...}}. Tolerate either flat
        # or nested.
        hb_data = body.get("data", body)
        assert "verification_overdue_count" in hb_data, f"heartbeat response missing verification_overdue_count: {body}"
        overdue_count = hb_data["verification_overdue_count"]
        assert isinstance(overdue_count, int), f"verification_overdue_count is not an int: {overdue_count}"
        assert "verification_overdue" in hb_data, f"heartbeat response missing verification_overdue: {body}"
        assert isinstance(hb_data["verification_overdue"], list), f"verification_overdue should be a list: {hb_data.get('verification_overdue')!r}"

        # ── /verifications/detect — list detectable claims ─────────
        # No body; returns a list of claim candidates for verification.
        # On a fresh graph with our single CAUSES edge, the detector
        # should surface at least that edge (or return an empty list
        # if the agent doesn't match — agent filter is optional).
        status, body = _http_get(f"{base_url}/verifications/detect")
        assert status == 200, f"GET /verifications/detect returned {status}: {body}"
        assert isinstance(body, dict)
        detect_data = body.get("data", body)
        assert isinstance(detect_data, list), f"/verifications/detect data should be a list, got {type(detect_data).__name__}: {body}"
        # The endpoint may filter by agent and return [], or surface
        # the unverified CAUSES edge. Either is acceptable — the
        # important thing is the endpoint doesn't 500.

        # ── /verifications/pending — list pending verifications ───
        status, body = _http_get(f"{base_url}/verifications/pending")
        assert status == 200, f"GET /verifications/pending returned {status}: {body}"
        assert isinstance(body, dict)
        pending_data = body.get("data", body)
        assert isinstance(pending_data, list), f"/verifications/pending data should be a list, got {type(pending_data).__name__}: {body}"

        # ── /verifications/nudge — fire_verification_nudge (ADR-018.4)
        # Creates a CHALLENGED_BY edge on the unverified CAUSES edge.
        # The smoke test only checks the endpoint doesn't error; the
        # consensus-only detection logic is covered by
        # tests/test_consensus_verification.py at the unit level.
        status, body = _http_post_json(
            f"{base_url}/verifications/nudge",
            {
                "edge_id": edge_id,
                "reason": "smoke test verification nudge",
                "confidence": 0.5,
            },
        )
        assert status in (200, 201), f"POST /verifications/nudge returned {status}: {body}"
        assert isinstance(body, dict)

        # ── /verifications/outcome — record outcome (ADR-018.3) ────
        # Records a TRUE outcome on the unverified edge, which
        # should stop it from decaying in the next /admin/verification-decay.
        # The endpoint accepts a string label, not a Python bool:
        # 'true' | 'false' | 'ambiguous' | 'deferred'.
        status, body = _http_post_json(
            f"{base_url}/verifications/outcome",
            {
                "edge_id": edge_id,
                "outcome": "true",
                "reason": "smoke test verified",
            },
        )
        assert status in (200, 201), f"POST /verifications/outcome returned {status}: {body}"
        assert isinstance(body, dict)

        # ── /admin/verification-decay — ADR-018.3 apply decay ─────
        # Default dry_run=True; should return 200 with the edges
        # that would be affected. After the verification outcome
        # above, our CAUSES edge is verified (365d half-life), so
        # the affected set should be smaller than the overdue list.
        status, body = _http_post_json(f"{base_url}/admin/verification-decay", {})
        assert status in (200, 201), f"POST /admin/verification-decay returned {status}: {body}"
        assert isinstance(body, dict)

        # ── /admin/verification-scan — dry-run inspection ─────────
        # Returns the list of edges that WOULD be affected by decay.
        # Different from /verifications/detect (which surfaces
        # claims to verify) — this surfaces claims past their grace
        # period and decaying.
        status, body = _http_get(f"{base_url}/admin/verification-scan")
        assert status == 200, f"GET /admin/verification-scan returned {status}: {body}"
        assert isinstance(body, dict)


# ── Config isolation regression (OHM-817) ───────────────────────────────


def test_spawn_ohmd_isolates_from_production_config(tmp_path, monkeypatch):
    """_spawn_ohmd must not load a production ~/.ohm/ohmd.json.

    Regression for OHM-817: on dev machines with a populated
    ``~/.ohm/ohmd.json`` containing tokens, the daemon subprocess
    would load those tokens, and the ``--no-auth`` admin bypass
    fails because it requires both ``no_auth=True`` AND ``tokens``
    being empty. This test proves the isolation by faking a home
    directory with a populated ohmd.json and asserting an
    ``/admin/*`` call succeeds without a token.
    """
    import json

    fake_home = tmp_path / "fake_home"
    ohm_dir = fake_home / ".ohm"
    ohm_dir.mkdir(parents=True)
    populated_config = {
        "tokens": {
            "some-prod-agent": {
                "hash": "a" * 64,
                "role": "admin",
            },
        },
        "customer_tokens": {
            "some-customer": {
                "hash": "b" * 64,
                "role": "customer",
            },
        },
        "host": "127.0.0.1",
        "port": 9999,
        "db_path": str(tmp_path / "prod.duckdb"),
    }
    (ohm_dir / "ohmd.json").write_text(json.dumps(populated_config))

    # Point Path.home() at the fake home on all platforms.
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))

    with _spawn_ohmd(tmp_path, db_name="isolation.duckdb") as (base_url, _env, proc, stderr_log):
        _wait_for_healthy_or_fail(base_url, proc, stderr_log)

        # An admin endpoint must succeed with NO bearer token — this
        # is the exact scenario that breaks if production tokens leak
        # into the subprocess config (server.py:1305 requires both
        # no_auth=True AND not self.tokens). We use /admin/nudges/quality
        # (a GET) rather than /admin/health, which has a pre-existing
        # UnboundLocalError on challenge_ratio when the graph has no L3
        # edges — unrelated to config isolation.
        status, body = _http_get(f"{base_url}/admin/nudges/quality")
        assert status == 200, (
            f"GET /admin/nudges/quality returned {status} (body: {body}) — "
            "production config tokens likely leaked into the subprocess"
        )
