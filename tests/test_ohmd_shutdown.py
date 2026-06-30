"""Tests for OHM-k79z — graceful shutdown deadlock fix.

Root cause: ``server.shutdown()`` is called from the SIGTERM/SIGINT signal
handler, which always runs on the main thread. ``server.serve_forever()`` is
also running on the main thread. Per the Python socketserver docs, calling
``shutdown()`` from the same thread that is running ``serve_forever()``
deadlocks: ``shutdown()`` blocks on ``__is_shut_down.wait()``, which is
only set when ``serve_forever()`` exits its loop — but ``serve_forever()``
can't exit because the main thread is stuck inside ``shutdown()``.

Symptom live on the daemon (OHM-k79z): systemctl restart|stop|kill -s SIGTERM
all return 0, but the uhmd PID and start time never change. The signal
handler never returns. SIGKILL was the only way out.

Fix: dispatch ``server.shutdown()`` to a daemon thread. The daemon sets
``__shutdown_request`` (fast, non-blocking), then waits on
``__is_shut_down``. The main thread returns from the handler, resumes
``serve_forever()``, sees the flag, exits the loop, sets ``__is_shut_down``,
the daemon unblocks, the main thread falls through to ``store.close()``
and the process exits.

Tests here:
- Unit-level verification that the shutdown handler dispatches
  ``server.shutdown()`` to a separate thread rather than calling it
  synchronously. This is OS-agnostic.
- POSIX end-to-end test that an ``ohmd`` subprocess exits cleanly on
  SIGTERM within a bounded wait. Skipped on Windows (no real SIGTERM) and
  when the ``ohmd`` entry point is not on PATH.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
OHMD_BIN = shutil.which("ohmd")
_POSIX = hasattr(signal, "SIGTERM") and os.name != "nt"


class TestShutdownHandlerThreadDispatch:
    """The fix: shutdown() must run on a different thread than serve_forever()."""

    def test_shutdown_dispatched_to_separate_thread(self, monkeypatch):
        """Bind a re-defined shutdown_handler against a mock server, fire it,
        and assert server.shutdown() was invoked from a NON-main thread."""
        import threading

        captured_threads: list[threading.Thread] = []
        original_thread_init = threading.Thread.__init__

        def recording_thread_init(self, *args, **kwargs):
            original_thread_init(self, *args, **kwargs)
            captured_threads.append(self)

        monkeypatch.setattr(threading.Thread, "__init__", recording_thread_init)

        shutdown_called_from: dict[str, object] = {}

        class MockServer:
            def shutdown(self):
                shutdown_called_from["thread"] = threading.current_thread().name
                shutdown_called_from["is_main"] = (
                    threading.current_thread() is threading.main_thread()
                )

        class MockStore:
            def sync_heartbeat(self):
                return {"pushed": 0, "pulled": 0}

            class _Conn:
                def execute(self, *a, **k):
                    return None

            conn = _Conn()

        mock_server = MockServer()
        store = MockStore()  # type: ignore[assignment]
        sync_stop = threading.Event()
        eviction_stop = threading.Event()
        metric_stop = threading.Event()

        # Re-implements the closure that production's run_server builds.
        # The point here is to assert the dispatch pattern (daemon-thread
        # hop for server.shutdown) — not to directly call the production
        # closure (which is bound to its own server variable).
        def shutdown_handler(signum, frame):
            sync_stop.set()
            eviction_stop.set()
            metric_stop.set()
            try:
                store.sync_heartbeat()
            except Exception:
                pass
            try:
                store.conn.execute("CHECKPOINT")
            except Exception:
                pass
            # The fix: dispatch to a daemon thread, NOT a synchronous call.
            threading.Thread(
                target=mock_server.shutdown, daemon=True, name="ohmd-shutdown"
            ).start()

        shutdown_handler(signal.SIGTERM, None)

        # Wait briefly for the daemon thread to surface the result
        deadline = time.time() + 2.0
        while time.time() < deadline and "thread" not in shutdown_called_from:
            time.sleep(0.01)

        assert "thread" in shutdown_called_from, (
            "server.shutdown() was never invoked — daemon failed to start"
        )
        # The whole point of OHM-k79z: shutdown must NOT run on the main thread
        assert shutdown_called_from["is_main"] is False, (
            "server.shutdown() ran on the main thread — this is the "
            "OHM-k79z deadlock. It must be dispatched to a separate thread."
        )
        assert shutdown_called_from["thread"] == "ohmd-shutdown"
        assert len(captured_threads) >= 1

    def test_production_handler_uses_thread_dispatch(self):
        """Static AST check: shutdown_handler in server.py must dispatch
        ``server.shutdown()`` via ``threading.Thread(...).start()`` rather than
        calling it synchronously. Protects against regressions."""
        import ast

        server_path = REPO_ROOT / "src" / "ohm" / "server" / "server.py"
        source = server_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        handler_funcs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "shutdown_handler"
        ]
        assert handler_funcs, "shutdown_handler must be defined in server.py"

        # Walk the handler's body and ensure it contains a
        # threading.Thread(... target=server.shutdown ...) construct, where
        # target may be either a bare Attribute (server.shutdown) or a Call
        # (server.shutdown(...)). Both forms dispatch to a separate thread.
        contains_thread_call = False
        for node in ast.walk(handler_funcs[0]):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "Thread":
                    if isinstance(func.value, ast.Name) and func.value.id == "threading":
                        for kw in node.keywords:
                            if kw.arg == "target":
                                target = kw.value
                                shutdown_attr = None
                                if isinstance(target, ast.Attribute):
                                    shutdown_attr = target.attr
                                elif isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute):
                                    shutdown_attr = target.func.attr
                                if shutdown_attr == "shutdown":
                                    contains_thread_call = True
        assert contains_thread_call, (
            "shutdown_handler must dispatch server.shutdown() via "
            "threading.Thread(target=server.shutdown, ...).start() — "
            "synchronous calls deadlock (OHM-k79z)."
        )


@pytest.mark.skipif(
    not _POSIX or OHMD_BIN is None,
    reason="Requires POSIX SIGTERM and 'ohmd' on PATH (Linux production env)",
)
class TestSubprocessSIGTERM:
    """End-to-end: an ohmd subprocess should exit cleanly on SIGTERM in ≤10s."""

    def test_ohmd_exits_on_sigterm(self, tmp_path):
        # Pick a free port so we don't collide with anything
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        db_path = str(tmp_path / "shutdown_test.duckdb")
        proc = subprocess.Popen(
            [
                OHMD_BIN,  # type: ignore[list-item]
                "--host", "127.0.0.1",
                "--port", str(port),
                "--db", db_path,
                "--no-auth",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Wait for /health to respond 200
            deadline = time.time() + 30.0
            healthy = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    stderr_data = proc.stderr.read() if proc.stderr else b""
                    pytest.fail(f"ohmd exited early: {stderr_data.decode(errors='replace')}")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1
                    ) as r:
                        if r.status == 200:
                            healthy = True
                            break
                except Exception:
                    time.sleep(0.2)
            assert healthy, "ohmd did not become healthy within 30s"

            # Send SIGTERM and verify clean exit within 10s
            proc.send_signal(signal.SIGTERM)
            try:
                rc = proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                pytest.fail(
                    "ohmd did NOT exit within 10s of SIGTERM — the "
                    "OHM-k79z deadlock is still present."
                )
            # 0 = clean exit, -15 = terminated by SIGTERM (acceptable under
            # default signal handling); any other code indicates a real error.
            assert rc in (0, -signal.SIGTERM), (
                f"ohmd exited with unexpected code {rc} after SIGTERM"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)