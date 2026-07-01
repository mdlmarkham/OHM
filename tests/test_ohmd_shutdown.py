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

Also covered (OHM-inq1): the signal handler must be FAST and NON-BLOCKING.
Earlier versions ran ``store.sync_heartbeat()`` and
``store.conn.execute("CHECKPOINT")`` inside the handler. If a background
worker thread (ducklake-sync, fragment-eviction, semantic-metric-actions,
beads-sync) is in the middle of a long DuckDB query holding ``store.conn``,
those calls block waiting for the connection — the main thread is stuck
inside the handler, ``serve_forever()`` never re-enters its selector loop,
``__is_shut_down`` never gets set, the daemon-thread that called
``server.shutdown()`` hangs forever, and the process won't exit.

Fix: shrink the handler to set stop events + dispatch server.shutdown() +
return. Move sync_heartbeat / CHECKPOINT / tenant_manager.shutdown /
store.close() to AFTER ``serve_forever`` returns, with bounded
``thread.join(timeout=...)`` waits on each worker thread so a stuck worker
can't hang shutdown indefinitely.

Tests here:
- Unit-level verification that the shutdown handler dispatches
  ``server.shutdown()`` to a separate thread rather than calling it
  synchronously. This is OS-agnostic.
- AST-level regression protection: shutdown_handler must NOT call
  ``store.sync_heartbeat()`` or ``store.conn.execute(...)`` — those are
  moved to the post-serve_forever cleanup (OHM-inq1).
- Behavioural test: a handler that mirrors the production shape returns
  within bounded time even when a worker thread is "stuck" (i.e., would
  hold ``store.conn`` for an unbounded duration).
- POSIX end-to-end test that an ``ohmd`` subprocess exits cleanly on
  SIGTERM within a bounded wait. Skipped on Windows (no real SIGTERM) and
  when the ``ohmd`` entry point is not on PATH.
"""

from __future__ import annotations

import ast
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
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
                shutdown_called_from["is_main"] = threading.current_thread() is threading.main_thread()

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
            threading.Thread(target=mock_server.shutdown, daemon=True, name="ohmd-shutdown").start()

        shutdown_handler(signal.SIGTERM, None)

        # Wait briefly for the daemon thread to surface the result
        deadline = time.time() + 2.0
        while time.time() < deadline and "thread" not in shutdown_called_from:
            time.sleep(0.01)

        assert "thread" in shutdown_called_from, "server.shutdown() was never invoked — daemon failed to start"
        # The whole point of OHM-k79z: shutdown must NOT run on the main thread
        assert shutdown_called_from["is_main"] is False, "server.shutdown() ran on the main thread — this is the OHM-k79z deadlock. It must be dispatched to a separate thread."
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

        handler_funcs = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "shutdown_handler"]
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
        assert contains_thread_call, "shutdown_handler must dispatch server.shutdown() via threading.Thread(target=server.shutdown, ...).start() — synchronous calls deadlock (OHM-k79z)."


class TestShutdownHandlerIsMinimal:
    """OHM-inq1: the signal handler must be fast and non-blocking.

    Earlier versions ran ``store.sync_heartbeat()`` and
    ``store.conn.execute("CHECKPOINT")`` inside the handler. If a worker
    thread is mid-query on ``store.conn``, those calls block waiting for
    the connection, the main thread is stuck inside the handler,
    ``serve_forever()`` never re-enters its loop, and the daemon-thread
    that called ``server.shutdown()`` hangs forever. Fix: move all that
    work to the post-``serve_forever`` cleanup with bounded joins.
    """

    @staticmethod
    def _load_shutdown_handler_ast():
        server_path = REPO_ROOT / "src" / "ohm" / "server" / "server.py"
        source = server_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "shutdown_handler"]
        assert handlers, "shutdown_handler must be defined in server.py"
        return handlers[0]

    def test_handler_does_not_call_sync_heartbeat(self):
        """``store.sync_heartbeat(...)`` must not appear inside
        ``shutdown_handler``. That's an OHM-inq1 regression — the call
        belongs to the post-serve_forever cleanup."""
        handler = self._load_shutdown_handler_ast()
        offenders = []
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match: store.sync_heartbeat(...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "sync_heartbeat"
                and isinstance(func.value, ast.Name)
                and func.value.id == "store"
            ):
                offenders.append(ast.dump(node))
        assert not offenders, (
            "shutdown_handler must not call store.sync_heartbeat() — "
            "that work belongs AFTER serve_forever returns (OHM-inq1). "
            f"Found: {offenders}"
        )

    def test_handler_does_not_call_checkpoint(self):
        """``store.conn.execute(\"CHECKPOINT\")`` must not appear inside
        ``shutdown_handler``. The CHECKPOINT belongs to the
        post-serve_forever cleanup (OHM-inq1)."""
        handler = self._load_shutdown_handler_ast()
        offenders = []
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match: store.conn.execute("CHECKPOINT") / store.conn.execute('CHECKPOINT')
            if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
                continue
            chain = func.value
            if not (isinstance(chain, ast.Attribute) and chain.attr == "conn"):
                continue
            if not (isinstance(chain.value, ast.Name) and chain.value.id == "store"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "CHECKPOINT" in arg.value.upper():
                    offenders.append(ast.dump(node))
        assert not offenders, (
            "shutdown_handler must not call store.conn.execute(\"CHECKPOINT\") — "
            "that work belongs AFTER serve_forever returns (OHM-inq1). "
            f"Found: {offenders}"
        )

    def test_handler_sets_all_four_stop_events(self):
        """Regression guard: shutdown_handler must set all four worker stop
        events (ducklake-sync, fragment-eviction, semantic-metric-actions,
        beads-sync). If any is missed, that worker won't see the stop
        signal and may hang the post-serve_forever cleanup."""
        handler = self._load_shutdown_handler_ast()
        names_set = set()
        for node in ast.walk(handler):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set":
                if isinstance(node.func.value, ast.Name):
                    names_set.add(node.func.value.id)
        for required in ("_sync_stop", "_eviction_stop", "_metric_actions_stop", "_beads_sync_stop"):
            assert required in names_set, (
                f"shutdown_handler must set {required} so the corresponding "
                f"worker thread sees the stop signal (OHM-inq1)."
            )

    def test_minimal_handler_returns_despite_blocked_worker(self):
        """Behavioural proof of the fix shape: a handler mirroring the
        production minimal structure (just set stop events + dispatch
        server.shutdown via daemon thread) returns promptly even when a
        worker thread is "stuck" in a long operation. This is the exact
        property OHM-inq1 requires.

        The earlier (broken) shape would call store.sync_heartbeat() /
        CHECKPOINT inline; if any of those touched ``store.conn`` while a
        worker held it, the handler would block. The new shape avoids
        them entirely inside the handler.
        """
        sync_stop = threading.Event()
        eviction_stop = threading.Event()
        metric_stop = threading.Event()
        beads_stop = threading.Event()

        shutdown_invoked = threading.Event()

        class MockServer:
            def shutdown(self_inner):
                shutdown_invoked.set()

        mock_server = MockServer()

        def shutdown_handler(signum, frame):
            sync_stop.set()
            eviction_stop.set()
            metric_stop.set()
            beads_stop.set()
            # NO store.sync_heartbeat() / CHECKPOINT here — that's OHM-inq1.
            threading.Thread(target=mock_server.shutdown, daemon=True, name="ohmd-shutdown").start()

        # Simulate a worker that is genuinely stuck. The old handler
        # shape would deadlock trying to talk to store.conn while this
        # worker held it; the new shape must not even notice.
        def stuck_worker():
            time.sleep(60)

        worker = threading.Thread(target=stuck_worker, daemon=True, name="stuck-eviction")
        worker.start()
        # Make sure the worker is actually running before we trigger
        # shutdown — otherwise we're testing nothing.
        time.sleep(0.01)

        start = time.time()
        shutdown_handler(signal.SIGTERM, None)
        elapsed = time.time() - start

        # Handler must return almost immediately — must NOT wait for the
        # worker (which would never finish within the test window).
        assert elapsed < 0.5, (
            f"shutdown_handler took {elapsed:.3f}s — it must return "
            f"promptly without waiting on worker threads (OHM-inq1)."
        )

        # All four stop events must be set so the workers will exit on
        # their next iteration.
        assert sync_stop.is_set()
        assert eviction_stop.is_set()
        assert metric_stop.is_set()
        assert beads_stop.is_set()

        # server.shutdown() must be dispatched to a daemon thread (the
        # OHM-k79z hop), so the daemon-thread was invoked promptly.
        assert shutdown_invoked.wait(timeout=1.0), (
            "server.shutdown() was never invoked — OHM-k79z dispatch failed."
        )

        # Worker is daemon=True so it won't block test teardown.

    def test_post_serve_forever_cleanup_has_bounded_joins(self):
        """OHM-inq1: the post-serve_forever cleanup must call
        ``thread.join(timeout=...)`` on each worker thread (not an
        unbounded ``.join()``), so a stuck worker can't hang shutdown
        forever. We mirror the production cleanup shape and verify a
        stuck worker doesn't block past the deadline.
        """
        JOIN_TIMEOUT = 0.5  # Mirrors _WORKER_THREAD_JOIN_TIMEOUT = 5.0 in production, smaller here.

        def join_worker(thread, name):
            if thread is None:
                return
            thread.join(timeout=JOIN_TIMEOUT)
            if thread.is_alive():
                # Mirrors the production log line.
                print(f"Worker thread {name} did not exit within {JOIN_TIMEOUT:.1f}s")

        def stuck_worker():
            time.sleep(60)  # Never finishes within the test window.

        t = threading.Thread(target=stuck_worker, daemon=True, name="stuck")
        t.start()
        time.sleep(0.01)  # Ensure thread is alive.

        start = time.time()
        join_worker(t, "stuck")
        elapsed = time.time() - start

        # The bounded join must return in ~JOIN_TIMEOUT, not the 60s
        # the worker actually needs.
        assert elapsed < JOIN_TIMEOUT + 0.5, (
            f"join_worker took {elapsed:.3f}s; bounded join should "
            f"return within ~{JOIN_TIMEOUT:.1f}s even when the worker "
            f"is stuck (OHM-inq1)."
        )
        # And the worker is still alive (proving the bounded join
        # returned without waiting for it to finish).
        assert t.is_alive()

    def test_production_cleanup_uses_bounded_joins(self):
        """Static AST check: the production ``run_server`` post-shutdown
        cleanup must use ``thread.join(timeout=...)`` (not a bare
        ``.join()``) so a stuck worker can't hang shutdown (OHM-inq1).
        """
        server_path = REPO_ROOT / "src" / "ohm" / "server" / "server.py"
        source = server_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Look at run_server and find every .join() call.
        run_server_funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_server"]
        assert run_server_funcs

        unbounded_joins = []
        bounded_joins = 0
        for node in ast.walk(run_server_funcs[0]):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "join":
                # Bare .join() — no timeout kwarg.
                has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                if not has_timeout:
                    unbounded_joins.append(ast.dump(node))
                else:
                    bounded_joins += 1

        assert not unbounded_joins, (
            "run_server must not call thread.join() without a timeout "
            "— a stuck worker thread would hang shutdown forever "
            "(OHM-inq1). Offending calls: "
            + "; ".join(unbounded_joins)
        )
        # And we should have at least one bounded join (the worker cleanup).
        assert bounded_joins >= 1, (
            "run_server should call thread.join(timeout=...) at least "
            "once for the worker cleanup (OHM-inq1)."
        )


@pytest.mark.skipif(
    not _POSIX or OHMD_BIN is None,
    reason="Requires POSIX SIGTERM and 'ohmd' on PATH (Linux production env)",
)
@pytest.mark.integration
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
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--db",
                db_path,
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
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
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
                pytest.fail("ohmd did NOT exit within 10s of SIGTERM — the OHM-k79z deadlock is still present.")
            # 0 = clean exit, -15 = terminated by SIGTERM (acceptable under
            # default signal handling); any other code indicates a real error.
            assert rc in (0, -signal.SIGTERM), f"ohmd exited with unexpected code {rc} after SIGTERM"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
