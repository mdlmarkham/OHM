"""Tests for the TOPO CLI — argument parsing and integration tests."""

import json
import os
import sys
import tempfile
from io import StringIO

from ohm.cli import build_parser, main


# ── Parsing Tests ────────────────────────────────────────────────────────────


class TestTopoCLIParsing:
    """Tests for TOPO CLI argument parsing."""

    def test_topo_schema(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "schema"])
        assert args.command == "topo"
        assert args.topo_command == "schema"

    def test_topo_failure_analysis(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "failure-analysis", "pump_A"])
        assert args.command == "topo"
        assert args.topo_command == "failure-analysis"
        assert args.node_id == "pump_A"

    def test_topo_failure_analysis_with_depth(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "failure-analysis", "pump_A", "--depth", "3"])
        assert args.depth == 3

    def test_topo_failure_analysis_with_edge_types(self):
        parser = build_parser()
        args = parser.parse_args([
            "topo", "failure-analysis", "pump_A",
            "--edge-type", "FEEDS",
            "--edge-type", "DEPENDS_ON",
        ])
        assert args.edge_types == ["FEEDS", "DEPENDS_ON"]

    def test_topo_compliance_map(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "compliance-map", "reactor_1"])
        assert args.command == "topo"
        assert args.topo_command == "compliance-map"
        assert args.node_id == "reactor_1"

    def test_topo_compliance_map_with_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "topo", "compliance-map", "reactor_1",
            "--depth", "5",
            "--direction", "incoming",
        ])
        assert args.depth == 5
        assert args.direction == "incoming"

    def test_topo_impact_study(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "impact-study", "valve_X"])
        assert args.command == "topo"
        assert args.topo_command == "impact-study"
        assert args.node_id == "valve_X"

    def test_topo_impact_study_with_depth(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "impact-study", "valve_X", "--depth", "7"])
        assert args.depth == 7

    def test_topo_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "failure-analysis", "node_1"])
        assert args.depth == 5  # default depth for failure-analysis
        assert args.edge_types is None  # default: use TOPO edge types

    def test_topo_compliance_map_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "compliance-map", "node_1"])
        assert args.depth == 3  # default depth for compliance-map
        assert args.direction == "both"  # default direction

    def test_topo_impact_study_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["topo", "impact-study", "node_1"])
        assert args.depth == 5  # default depth for impact-study


# ── Integration Tests ────────────────────────────────────────────────────────


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


class TestTopoCLIIntegration:
    """End-to-end TOPO CLI tests against a real database."""

    def test_topo_schema_human(self):
        code, out, _ = _run_cli(["--db", ":memory:", "topo", "schema"])
        assert code == 0
        assert "TOPO" in out
        assert "Node Types" in out

    def test_topo_schema_json(self):
        code, out, _ = _run_cli(["--db", ":memory:", "--format", "json", "topo", "schema"])
        assert code == 0
        data = json.loads(out)
        assert data["name"] == "topo"
        assert "process" in data["node_types"]
        assert "sensor" in data["node_types"]
        assert "FEEDS" in data["layer_edge_types"]["L2"]

    def test_topo_failure_analysis_empty(self):
        code, out, _ = _run_cli(["--db", ":memory:", "topo", "failure-analysis", "nonexistent"])
        assert code == 0
        assert "No downstream impact" in out

    def test_topo_failure_analysis_with_data(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_topo_fa.db")
        try:
            # Create a chain: pump_A FEEDS vessel_B FLOWS_TO reactor_C
            c1, o1, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "pump_A", "--to", "vessel_B",
                "--type", "FEEDS", "--layer", "L2",
            ])
            assert c1 == 0, f"write1 failed: {o1}"

            c2, o2, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "vessel_B", "--to", "reactor_C",
                "--type", "FLOWS_TO", "--layer", "L2",
            ])
            assert c2 == 0, f"write2 failed: {o2}"

            # Failure analysis should find downstream impacts
            c3, o3, _ = _run_cli([
                "--db", db_path, "topo", "failure-analysis", "pump_A",
            ])
            assert c3 == 0, f"failure-analysis failed: {o3}"
            assert "FEEDS" in o3 or "FLOWS_TO" in o3
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_topo_failure_analysis_json(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_topo_fa_json.db")
        try:
            # Create an edge
            c1, o1, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "pump_A", "--to", "vessel_B",
                "--type", "FEEDS", "--layer", "L2",
            ])
            assert c1 == 0

            c2, o2, _ = _run_cli([
                "--db", db_path, "--format", "json",
                "topo", "failure-analysis", "pump_A",
            ])
            assert c2 == 0
            data = json.loads(o2)
            assert data["node_id"] == "pump_A"
            assert "impacts" in data
            assert "edge_types" in data
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_topo_failure_analysis_edge_type_filter(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_topo_fa_filter.db")
        try:
            # Create two edges: one FEEDS, one CAUSES
            c1, _, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "pump_A", "--to", "vessel_B",
                "--type", "FEEDS", "--layer", "L2",
            ])
            assert c1 == 0

            c2, _, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "pump_A", "--to", "sensor_X",
                "--type", "CAUSES", "--layer", "L3",
            ])
            assert c2 == 0

            # Filter to only FEEDS
            c3, o3, _ = _run_cli([
                "--db", db_path, "--format", "json",
                "topo", "failure-analysis", "pump_A",
                "--edge-type", "FEEDS",
            ])
            assert c3 == 0
            data = json.loads(o3)
            assert data["filtered_impacts"] >= 1
            # All returned impacts should be FEEDS
            for impact in data["impacts"]:
                assert impact["edge_type"] == "FEEDS"
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_topo_compliance_map_empty(self):
        code, out, _ = _run_cli(["--db", ":memory:", "topo", "compliance-map", "nonexistent"])
        assert code == 0
        assert "No edges found" in out

    def test_topo_compliance_map_with_data(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_topo_cm.db")
        try:
            # Create compliance-relevant edges
            c1, o1, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "area_1", "--to", "system_A",
                "--type", "CONTAINS", "--layer", "L1",
            ])
            assert c1 == 0, f"write1 failed: {o1}"

            c2, o2, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "system_A", "--to", "pump_B",
                "--type", "DEPENDS_ON", "--layer", "L4",
            ])
            assert c2 == 0, f"write2 failed: {o2}"

            c3, o3, _ = _run_cli([
                "--db", db_path, "topo", "compliance-map", "system_A",
            ])
            assert c3 == 0, f"compliance-map failed: {o3}"
            assert "compliance-relevant" in o3.lower() or "CONTAINS" in o3 or "DEPENDS_ON" in o3
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_topo_compliance_map_json(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_topo_cm_json.db")
        try:
            c1, _, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "area_1", "--to", "system_A",
                "--type", "CONTAINS", "--layer", "L1",
            ])
            assert c1 == 0

            c2, o2, _ = _run_cli([
                "--db", db_path, "--format", "json",
                "topo", "compliance-map", "system_A",
            ])
            assert c2 == 0
            data = json.loads(o2)
            assert data["node_id"] == "system_A"
            assert "compliance_edges" in data
            assert "other_edges" in data
            assert "total_edges" in data
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_topo_impact_study_empty(self):
        code, out, _ = _run_cli(["--db", ":memory:", "topo", "impact-study", "nonexistent"])
        assert code == 0
        assert "Impact Study" in out

    def test_topo_impact_study_with_data(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_topo_is.db")
        try:
            # Create a chain of edges
            c1, o1, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "pump_A", "--to", "vessel_B",
                "--type", "FEEDS", "--layer", "L2",
            ])
            assert c1 == 0, f"write1 failed: {o1}"

            c2, o2, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "vessel_B", "--to", "reactor_C",
                "--type", "FLOWS_TO", "--layer", "L2",
            ])
            assert c2 == 0, f"write2 failed: {o2}"

            c3, o3, _ = _run_cli([
                "--db", db_path, "topo", "impact-study", "pump_A",
            ])
            assert c3 == 0, f"impact-study failed: {o3}"
            assert "Impact Study" in o3
            assert "Downstream Impact" in o3
            assert "Local Context" in o3
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_topo_impact_study_json(self):
        db_path = os.path.join(tempfile.gettempdir(), "ohm_test_topo_is_json.db")
        try:
            c1, _, _ = _run_cli([
                "--db", db_path, "--actor", "topo-test",
                "graph", "write",
                "--from", "pump_A", "--to", "vessel_B",
                "--type", "FEEDS", "--layer", "L2",
            ])
            assert c1 == 0

            c2, o2, _ = _run_cli([
                "--db", db_path, "--format", "json",
                "topo", "impact-study", "pump_A",
            ])
            assert c2 == 0
            data = json.loads(o2)
            assert data["node_id"] == "pump_A"
            assert "impact" in data
            assert "neighborhood" in data
            assert "total" in data["impact"]
            assert "total" in data["neighborhood"]
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_topo_no_subcommand(self):
        """Running 'ohm topo' without a subcommand should print help."""
        code, out, _ = _run_cli(["--db", ":memory:", "topo"])
        # Should exit 0 (prints help) or show a message
        assert code == 0

    def test_topo_main_entry_point(self):
        """Test that topo_main() routes to topo subcommands."""
        from ohm.cli import topo_main

        stdout = StringIO()
        stderr = StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        exit_code = 0
        try:
            topo_main(["--db", ":memory:", "schema"])
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 1
        except Exception:
            exit_code = 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        assert exit_code == 0
        output = stdout.getvalue()
        assert "TOPO" in output
