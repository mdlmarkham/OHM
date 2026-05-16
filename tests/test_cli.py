"""Tests for the OHM CLI argument parsing and dispatch."""

from ohm.cli import build_parser


class TestCLIParsing:
    """Tests for CLI argument parsing."""

    def test_parser_created(self):
        parser = build_parser()
        assert parser.prog == "ohm"

    def test_serve_start(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "start", "--port", "9876"])
        assert args.command == "serve"
        assert args.serve_command == "start"
        assert args.port == 9876

    def test_serve_status(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "status"])
        assert args.command == "serve"
        assert args.serve_command == "status"

    def test_graph_schema(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "schema"])
        assert args.command == "graph"
        assert args.graph_command == "schema"

    def test_graph_layers(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "layers"])
        assert args.command == "graph"
        assert args.graph_command == "layers"

    def test_graph_write(self):
        parser = build_parser()
        args = parser.parse_args([
            "graph", "write",
            "--from", "node_a",
            "--to", "node_b",
            "--type", "CAUSES",
            "--layer", "L3",
            "--confidence", "0.94",
        ])
        assert args.command == "graph"
        assert args.graph_command == "write"
        assert args.from_node == "node_a"
        assert args.to_node == "node_b"
        assert args.edge_type == "CAUSES"
        assert args.layer == "L3"
        assert args.confidence == 0.94

    def test_graph_neighborhood(self):
        parser = build_parser()
        args = parser.parse_args([
            "graph", "neighborhood", "node_123",
            "--depth", "5",
            "--layer", "L3",
            "--direction", "outgoing",
        ])
        assert args.graph_command == "neighborhood"
        assert args.node_id == "node_123"
        assert args.depth == 5
        assert args.layer == "L3"
        assert args.direction == "outgoing"

    def test_graph_challenge(self):
        parser = build_parser()
        args = parser.parse_args([
            "graph", "challenge", "edge_abc",
            "--reason", "insufficient evidence",
            "--confidence", "0.3",
        ])
        assert args.graph_command == "challenge"
        assert args.edge_id == "edge_abc"
        assert args.reason == "insufficient evidence"
        assert args.confidence == 0.3

    def test_graph_confidence(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "confidence", "edge_xyz"])
        assert args.graph_command == "confidence"
        assert args.edge_id == "edge_xyz"

    def test_graph_listen(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "listen", "--since", "2026-05-16T00:00:00"])
        assert args.graph_command == "listen"
        assert args.since == "2026-05-16T00:00:00"

    def test_graph_impact(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "impact", "pump_A", "--depth", "3"])
        assert args.graph_command == "impact"
        assert args.node_id == "pump_A"
        assert args.depth == 3

    def test_graph_path(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "path", "node_a", "node_z", "--max-depth", "15"])
        assert args.graph_command == "path"
        assert args.from_node == "node_a"
        assert args.to_node == "node_z"
        assert args.max_depth == 15

    def test_graph_stats(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "stats"])
        assert args.graph_command == "stats"

    def test_state_set(self):
        parser = build_parser()
        args = parser.parse_args(["state", "set", "researching", "AND→OR", "patterns"])
        assert args.command == "state"
        assert args.state_command == "set"
        assert args.focus == ["researching", "AND→OR", "patterns"]

    def test_state_show(self):
        parser = build_parser()
        args = parser.parse_args(["state", "show", "clio"])
        assert args.state_command == "show"
        assert args.agent == "clio"

    def test_state_show_self(self):
        parser = build_parser()
        args = parser.parse_args(["state", "show"])
        assert args.state_command == "show"
        assert args.agent is None

    def test_state_who_is_working_on(self):
        parser = build_parser()
        args = parser.parse_args(["state", "who-is-working-on", "democratic", "institutions"])
        assert args.state_command == "who-is-working-on"
        assert args.topic == ["democratic", "institutions"]

    def test_state_history(self):
        parser = build_parser()
        args = parser.parse_args(["state", "history"])
        assert args.state_command == "history"

    def test_snapshot(self):
        parser = build_parser()
        args = parser.parse_args(["snapshot", "2026-05-15T14:30:00", "--node", "node_abc"])
        assert args.command == "snapshot"
        assert args.timestamp == "2026-05-15T14:30:00"
        assert args.node == "node_abc"

    def test_diff(self):
        parser = build_parser()
        args = parser.parse_args([
            "diff", "2026-05-15", "2026-05-16",
            "--layer", "L3",
            "--agent", "metis",
        ])
        assert args.command == "diff"
        assert args.from_ts == "2026-05-15"
        assert args.to_ts == "2026-05-16"
        assert args.layer == "L3"
        assert args.agent == "metis"

    def test_version(self):
        parser = build_parser()
        args = parser.parse_args(["--version"])
        assert args.version is True

    def test_format_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--format", "json", "graph", "status"])
        assert args.format == "json"

    def test_no_args_shows_help(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None
