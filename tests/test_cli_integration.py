"""Integration tests for the OHM CLI — execute commands against a real DB."""

import json
import os
import sys
import tempfile
from io import StringIO

from ohm.cli import main


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI with given args and capture stdout/stderr + exit code."""
    stdout = StringIO()
    stderr = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    exit_code = 0
    try:
        main(argv)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    except Exception:
        exit_code = 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return exit_code, stdout.getvalue(), stderr.getvalue()


class TestCLIIntegration:
    """End-to-end CLI tests against a real database."""

    def test_graph_schema_output(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "schema"])
        assert code == 0
        assert "Node Types" in out

    def test_graph_layers_output(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "layers"])
        assert code == 0
        assert "L1: Structure" in out

    def test_graph_status_empty(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "status"])
        assert code == 0

    def test_graph_stats_empty(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "stats"])
        assert code == 0

    def test_write_and_query(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_shared.db")
        try:
            c1, o1, _ = _run_cli([
                "--db", db_path, "--actor", "test",
                "graph", "write",
                "--from", "a", "--to", "b", "--type", "CAUSES", "--layer", "L3",
            ])
            assert c1 == 0, f"write failed: {o1}"

            c2, o2, _ = _run_cli([
                "--db", db_path, "--actor", "test",
                "graph", "write",
                "--from", "b", "--to", "c", "--type", "INFLUENCES", "--layer", "L2",
            ])
            assert c2 == 0, f"write2 failed: {o2}"

            c3, o3, _ = _run_cli([
                "--db", db_path, "graph", "neighborhood", "a", "--depth", "2",
            ])
            assert c3 == 0, f"neighborhood failed: {o3}"
            assert "CAUSES" in o3
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_state_set_and_show(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_state.db")
        try:
            c1, o1, _ = _run_cli([
                "--db", db_path, "--actor", "metis",
                "state", "set", "researching patterns",
            ])
            assert c1 == 0, f"state set failed: {o1}"

            c2, o2, _ = _run_cli([
                "--db", db_path, "--actor", "metis",
                "state", "show",
            ])
            assert c2 == 0, f"state show failed: {o2}"
            assert "metis" in o2
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_graph_query_text(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "query", "test"])
        assert code == 0

    def test_graph_listen(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "listen"])
        assert code == 0

    def test_graph_impact(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "impact", "nonexistent"])
        assert code == 0

    def test_graph_path(self):
        code, out, _ = _run_cli(["--db", ":memory:", "graph", "path", "a", "z"])
        assert code == 0

    def test_version_flag(self):
        code, out, _ = _run_cli(["--version"])
        assert code == 0
        assert "ohm" in out

    def test_json_format(self):
        code, out, _ = _run_cli(["--db", ":memory:", "--format", "json", "graph", "status"])
        assert code == 0
        data = json.loads(out)
        assert "total_nodes" in data

    def test_update_nonexistent_edge(self):
        code, out, err = _run_cli([
            "--db", ":memory:", "--actor", "test",
            "graph", "update", "nonexistent",
            "--confidence", "0.5",
        ])
        assert "not found" in (out + err).lower()

    def test_observe_nonexistent_node(self):
        code, out, err = _run_cli([
            "--db", ":memory:", "--actor", "test",
            "graph", "observe", "nonexistent",
            "--type", "measurement", "--value", "1.0",
        ])
        assert "not found" in (out + err).lower()
