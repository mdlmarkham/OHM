import pytest
from ohm.cli import build_parser


class TestCLIParsing:
    def test_parser_created(self):
        parser = build_parser()
        assert parser.prog == "ohm"

    @pytest.mark.parametrize(
        "cli_args,attr,expected",
        [
            pytest.param(["--version"], "version", True, id="version-flag"),
            pytest.param(["--format", "json", "graph", "status"], "format", "json", id="format-flag"),
            pytest.param([], "command", None, id="no-args-command-none"),
        ],
    )
    def test_global_flags(self, cli_args, attr, expected):
        parser = build_parser()
        args = parser.parse_args(cli_args)
        assert getattr(args, attr) == expected

    @pytest.mark.parametrize(
        "cli_args,attr,expected",
        [
            pytest.param(["serve", "start", "--port", "9876"], "command", "serve", id="serve-start-command"),
            pytest.param(["serve", "start", "--port", "9876"], "serve_command", "start", id="serve-start-subcommand"),
            pytest.param(["serve", "start", "--port", "9876"], "port", 9876, id="serve-start-port"),
            pytest.param(["serve", "status"], "command", "serve", id="serve-status-command"),
            pytest.param(["serve", "status"], "serve_command", "status", id="serve-status-subcommand"),
        ],
    )
    def test_serve_args(self, cli_args, attr, expected):
        parser = build_parser()
        args = parser.parse_args(cli_args)
        assert getattr(args, attr) == expected

    @pytest.mark.parametrize(
        "cli_args,attr,expected",
        [
            pytest.param(["graph", "schema"], "command", "graph", id="graph-schema-command"),
            pytest.param(["graph", "schema"], "graph_command", "schema", id="graph-schema-sub"),
            pytest.param(["graph", "layers"], "command", "graph", id="graph-layers-command"),
            pytest.param(["graph", "layers"], "graph_command", "layers", id="graph-layers-sub"),
            pytest.param(["graph", "write", "--from", "node_a", "--to", "node_b", "--type", "CAUSES", "--layer", "L3", "--confidence", "0.94"], "command", "graph", id="graph-write-command"),
            pytest.param(["graph", "write", "--from", "node_a", "--to", "node_b", "--type", "CAUSES", "--layer", "L3", "--confidence", "0.94"], "graph_command", "write", id="graph-write-sub"),
            pytest.param(["graph", "write", "--from", "node_a", "--to", "node_b", "--type", "CAUSES", "--layer", "L3", "--confidence", "0.94"], "from_node", "node_a", id="graph-write-from"),
            pytest.param(["graph", "write", "--from", "node_a", "--to", "node_b", "--type", "CAUSES", "--layer", "L3", "--confidence", "0.94"], "to_node", "node_b", id="graph-write-to"),
            pytest.param(["graph", "write", "--from", "node_a", "--to", "node_b", "--type", "CAUSES", "--layer", "L3", "--confidence", "0.94"], "edge_type", "CAUSES", id="graph-write-type"),
            pytest.param(["graph", "write", "--from", "node_a", "--to", "node_b", "--type", "CAUSES", "--layer", "L3", "--confidence", "0.94"], "layer", "L3", id="graph-write-layer"),
            pytest.param(["graph", "write", "--from", "node_a", "--to", "node_b", "--type", "CAUSES", "--layer", "L3", "--confidence", "0.94"], "confidence", 0.94, id="graph-write-confidence"),
            pytest.param(["graph", "neighborhood", "node_123", "--depth", "5", "--layer", "L3", "--direction", "outgoing"], "graph_command", "neighborhood", id="graph-nbr-sub"),
            pytest.param(["graph", "neighborhood", "node_123", "--depth", "5", "--layer", "L3", "--direction", "outgoing"], "node_id", "node_123", id="graph-nbr-node"),
            pytest.param(["graph", "neighborhood", "node_123", "--depth", "5", "--layer", "L3", "--direction", "outgoing"], "depth", 5, id="graph-nbr-depth"),
            pytest.param(["graph", "neighborhood", "node_123", "--depth", "5", "--layer", "L3", "--direction", "outgoing"], "layer", "L3", id="graph-nbr-layer"),
            pytest.param(["graph", "neighborhood", "node_123", "--depth", "5", "--layer", "L3", "--direction", "outgoing"], "direction", "outgoing", id="graph-nbr-direction"),
            pytest.param(["graph", "challenge", "edge_abc", "--reason", "insufficient evidence", "--confidence", "0.3"], "graph_command", "challenge", id="graph-challenge-sub"),
            pytest.param(["graph", "challenge", "edge_abc", "--reason", "insufficient evidence", "--confidence", "0.3"], "edge_id", "edge_abc", id="graph-challenge-edge"),
            pytest.param(["graph", "challenge", "edge_abc", "--reason", "insufficient evidence", "--confidence", "0.3"], "reason", "insufficient evidence", id="graph-challenge-reason"),
            pytest.param(["graph", "challenge", "edge_abc", "--reason", "insufficient evidence", "--confidence", "0.3"], "confidence", 0.3, id="graph-challenge-confidence"),
            pytest.param(["graph", "confidence", "edge_xyz"], "graph_command", "confidence", id="graph-confidence-sub"),
            pytest.param(["graph", "confidence", "edge_xyz"], "edge_id", "edge_xyz", id="graph-confidence-edge"),
            pytest.param(["graph", "listen", "--since", "2026-05-16T00:00:00"], "graph_command", "listen", id="graph-listen-sub"),
            pytest.param(["graph", "listen", "--since", "2026-05-16T00:00:00"], "since", "2026-05-16T00:00:00", id="graph-listen-since"),
            pytest.param(["graph", "listen", "--node-type", "concept"], "graph_command", "listen", id="graph-listen-ntype-sub"),
            pytest.param(["graph", "listen", "--node-type", "concept"], "node_type", "concept", id="graph-listen-ntype"),
            pytest.param(["graph", "impact", "pump_A", "--depth", "3"], "graph_command", "impact", id="graph-impact-sub"),
            pytest.param(["graph", "impact", "pump_A", "--depth", "3"], "node_id", "pump_A", id="graph-impact-node"),
            pytest.param(["graph", "impact", "pump_A", "--depth", "3"], "depth", 3, id="graph-impact-depth"),
            pytest.param(["graph", "path", "node_a", "node_z", "--max-depth", "15"], "graph_command", "path", id="graph-path-sub"),
            pytest.param(["graph", "path", "node_a", "node_z", "--max-depth", "15"], "from_node", "node_a", id="graph-path-from"),
            pytest.param(["graph", "path", "node_a", "node_z", "--max-depth", "15"], "to_node", "node_z", id="graph-path-to"),
            pytest.param(["graph", "path", "node_a", "node_z", "--max-depth", "15"], "max_depth", 15, id="graph-path-maxdepth"),
            pytest.param(["graph", "stats"], "graph_command", "stats", id="graph-stats-sub"),
            pytest.param(["graph", "upgrade"], "graph_command", "upgrade", id="graph-upgrade-sub"),
            pytest.param(["graph", "upgrade"], "dry_run", False, id="graph-upgrade-dryrun-false"),
            pytest.param(["graph", "upgrade", "--dry-run"], "graph_command", "upgrade", id="graph-upgrade-dryrun-sub"),
            pytest.param(["graph", "upgrade", "--dry-run"], "dry_run", True, id="graph-upgrade-dryrun-true"),
            pytest.param(["graph", "voi"], "graph_command", "voi", id="graph-voi-sub"),
            pytest.param(["graph", "voi"], "decision", None, id="graph-voi-decision-default"),
            pytest.param(["graph", "voi"], "top", 10, id="graph-voi-top-default"),
            pytest.param(["graph", "voi"], "layers", None, id="graph-voi-layers-default"),
            pytest.param(["graph", "voi"], "leak", 0.15, id="graph-voi-leak-default"),
            pytest.param(["graph", "voi"], "root_prior", 0.3, id="graph-voi-rootprior-default"),
            pytest.param(["graph", "voi"], "edge_types", None, id="graph-voi-edgetypes-default"),
            pytest.param(["graph", "voi", "--decision", "d1,d2", "--top", "5", "--layers", "L3,L4", "--leak", "0.2", "--root-prior", "0.5", "--edge-types", "CAUSES,DEPENDS_ON"], "graph_command", "voi", id="graph-voi-args-sub"),
            pytest.param(["graph", "voi", "--decision", "d1,d2", "--top", "5", "--layers", "L3,L4", "--leak", "0.2", "--root-prior", "0.5", "--edge-types", "CAUSES,DEPENDS_ON"], "decision", "d1,d2", id="graph-voi-args-decision"),
            pytest.param(["graph", "voi", "--decision", "d1,d2", "--top", "5", "--layers", "L3,L4", "--leak", "0.2", "--root-prior", "0.5", "--edge-types", "CAUSES,DEPENDS_ON"], "top", 5, id="graph-voi-args-top"),
            pytest.param(["graph", "voi", "--decision", "d1,d2", "--top", "5", "--layers", "L3,L4", "--leak", "0.2", "--root-prior", "0.5", "--edge-types", "CAUSES,DEPENDS_ON"], "layers", "L3,L4", id="graph-voi-args-layers"),
            pytest.param(["graph", "voi", "--decision", "d1,d2", "--top", "5", "--layers", "L3,L4", "--leak", "0.2", "--root-prior", "0.5", "--edge-types", "CAUSES,DEPENDS_ON"], "leak", 0.2, id="graph-voi-args-leak"),
            pytest.param(["graph", "voi", "--decision", "d1,d2", "--top", "5", "--layers", "L3,L4", "--leak", "0.2", "--root-prior", "0.5", "--edge-types", "CAUSES,DEPENDS_ON"], "root_prior", 0.5, id="graph-voi-args-rootprior"),
            pytest.param(["graph", "voi", "--decision", "d1,d2", "--top", "5", "--layers", "L3,L4", "--leak", "0.2", "--root-prior", "0.5", "--edge-types", "CAUSES,DEPENDS_ON"], "edge_types", "CAUSES,DEPENDS_ON", id="graph-voi-args-edgetypes"),
            pytest.param(["graph", "voi-tasks"], "graph_command", "voi-tasks", id="graph-voitasks-sub"),
            pytest.param(["graph", "voi-tasks"], "agent", None, id="graph-voitasks-agent-default"),
            pytest.param(["graph", "voi-tasks"], "top", 5, id="graph-voitasks-top-default"),
            pytest.param(["graph", "voi-tasks", "--agent", "metis", "--decision", "d1", "--top", "3", "--layers", "L3", "--leak", "0.2", "--root-prior", "0.5"], "graph_command", "voi-tasks", id="graph-voitasks-args-sub"),
            pytest.param(["graph", "voi-tasks", "--agent", "metis", "--decision", "d1", "--top", "3", "--layers", "L3", "--leak", "0.2", "--root-prior", "0.5"], "agent", "metis", id="graph-voitasks-args-agent"),
            pytest.param(["graph", "voi-tasks", "--agent", "metis", "--decision", "d1", "--top", "3", "--layers", "L3", "--leak", "0.2", "--root-prior", "0.5"], "decision", "d1", id="graph-voitasks-args-decision"),
            pytest.param(["graph", "voi-tasks", "--agent", "metis", "--decision", "d1", "--top", "3", "--layers", "L3", "--leak", "0.2", "--root-prior", "0.5"], "top", 3, id="graph-voitasks-args-top"),
            pytest.param(["graph", "voi-tasks", "--agent", "metis", "--decision", "d1", "--top", "3", "--layers", "L3", "--leak", "0.2", "--root-prior", "0.5"], "layers", "L3", id="graph-voitasks-args-layers"),
            pytest.param(["graph", "voi-tasks", "--agent", "metis", "--decision", "d1", "--top", "3", "--layers", "L3", "--leak", "0.2", "--root-prior", "0.5"], "leak", 0.2, id="graph-voitasks-args-leak"),
            pytest.param(["graph", "voi-tasks", "--agent", "metis", "--decision", "d1", "--top", "3", "--layers", "L3", "--leak", "0.2", "--root-prior", "0.5"], "root_prior", 0.5, id="graph-voitasks-args-rootprior"),
            pytest.param(["graph", "granger", "node_a", "node_b"], "graph_command", "granger", id="graph-granger-sub"),
            pytest.param(["graph", "granger", "node_a", "node_b"], "from_node", "node_a", id="graph-granger-from"),
            pytest.param(["graph", "granger", "node_a", "node_b"], "to_node", "node_b", id="graph-granger-to"),
            pytest.param(["graph", "granger", "node_a", "node_b"], "max_lag", 3, id="graph-granger-lag-default"),
            pytest.param(["graph", "granger", "node_a", "node_b", "--max-lag", "5", "--min-observations", "10"], "max_lag", 5, id="graph-granger-lag-args"),
            pytest.param(["graph", "granger", "node_a", "node_b", "--max-lag", "5", "--min-observations", "10"], "min_observations", 10, id="graph-granger-minobs"),
            pytest.param(["graph", "edge-stability"], "graph_command", "edge-stability", id="graph-edgestab-sub"),
            pytest.param(["graph", "edge-stability"], "window_days", 7, id="graph-edgestab-window-default"),
            pytest.param(["graph", "edge-stability", "--window-days", "14", "--min-windows", "5", "--layer", "L3"], "window_days", 14, id="graph-edgestab-window"),
            pytest.param(["graph", "edge-stability", "--window-days", "14", "--min-windows", "5", "--layer", "L3"], "min_windows", 5, id="graph-edgestab-minwin"),
            pytest.param(["graph", "edge-stability", "--window-days", "14", "--min-windows", "5", "--layer", "L3"], "layer", "L3", id="graph-edgestab-layer"),
            pytest.param(["graph", "policy", "dec1"], "graph_command", "policy", id="graph-policy-sub"),
            pytest.param(["graph", "policy", "dec1"], "target", "dec1", id="graph-policy-target"),
            pytest.param(["graph", "policy", "dec1"], "horizon", 1, id="graph-policy-horizon-default"),
            pytest.param(["graph", "policy", "dec1", "--horizon", "3", "--observation-cost", "0.5", "--layers", "L3", "--leak", "0.2"], "horizon", 3, id="graph-policy-horizon"),
            pytest.param(["graph", "policy", "dec1", "--horizon", "3", "--observation-cost", "0.5", "--layers", "L3", "--leak", "0.2"], "observation_cost", 0.5, id="graph-policy-obscost"),
            pytest.param(["graph", "policy", "dec1", "--horizon", "3", "--observation-cost", "0.5", "--layers", "L3", "--leak", "0.2"], "layers", "L3", id="graph-policy-layers"),
            pytest.param(["graph", "policy", "dec1", "--horizon", "3", "--observation-cost", "0.5", "--layers", "L3", "--leak", "0.2"], "leak", 0.2, id="graph-policy-leak"),
        ],
    )
    def test_graph_args(self, cli_args, attr, expected):
        parser = build_parser()
        args = parser.parse_args(cli_args)
        assert getattr(args, attr) == expected

    @pytest.mark.parametrize(
        "cli_args,attr,expected",
        [
            pytest.param(["state", "set", "researching", "AND→OR", "patterns"], "command", "state", id="state-set-command"),
            pytest.param(["state", "set", "researching", "AND→OR", "patterns"], "state_command", "set", id="state-set-sub"),
            pytest.param(["state", "set", "researching", "AND→OR", "patterns"], "focus", ["researching", "AND→OR", "patterns"], id="state-set-focus"),
            pytest.param(["state", "show", "clio"], "state_command", "show", id="state-show-sub"),
            pytest.param(["state", "show", "clio"], "agent", "clio", id="state-show-agent"),
            pytest.param(["state", "show"], "state_command", "show", id="state-show-self-sub"),
            pytest.param(["state", "show"], "agent", None, id="state-show-self-agent-none"),
            pytest.param(["state", "who-is-working-on", "democratic", "institutions"], "state_command", "who-is-working-on", id="state-who-sub"),
            pytest.param(["state", "who-is-working-on", "democratic", "institutions"], "topic", ["democratic", "institutions"], id="state-who-topic"),
            pytest.param(["state", "history"], "state_command", "history", id="state-history-sub"),
        ],
    )
    def test_state_args(self, cli_args, attr, expected):
        parser = build_parser()
        args = parser.parse_args(cli_args)
        assert getattr(args, attr) == expected

    @pytest.mark.parametrize(
        "cli_args,attr,expected",
        [
            pytest.param(["snapshot", "2026-05-15T14:30:00", "--node", "node_abc"], "command", "snapshot", id="snapshot-command"),
            pytest.param(["snapshot", "2026-05-15T14:30:00", "--node", "node_abc"], "timestamp", "2026-05-15T14:30:00", id="snapshot-ts"),
            pytest.param(["snapshot", "2026-05-15T14:30:00", "--node", "node_abc"], "node", "node_abc", id="snapshot-node"),
        ],
    )
    def test_snapshot_args(self, cli_args, attr, expected):
        parser = build_parser()
        args = parser.parse_args(cli_args)
        assert getattr(args, attr) == expected

    @pytest.mark.parametrize(
        "cli_args,attr,expected",
        [
            pytest.param(["diff", "2026-05-15", "2026-05-16", "--layer", "L3", "--agent", "metis"], "command", "diff", id="diff-command"),
            pytest.param(["diff", "2026-05-15", "2026-05-16", "--layer", "L3", "--agent", "metis"], "from_ts", "2026-05-15", id="diff-from"),
            pytest.param(["diff", "2026-05-15", "2026-05-16", "--layer", "L3", "--agent", "metis"], "to_ts", "2026-05-16", id="diff-to"),
            pytest.param(["diff", "2026-05-15", "2026-05-16", "--layer", "L3", "--agent", "metis"], "layer", "L3", id="diff-layer"),
            pytest.param(["diff", "2026-05-15", "2026-05-16", "--layer", "L3", "--agent", "metis"], "agent", "metis", id="diff-agent"),
        ],
    )
    def test_diff_args(self, cli_args, attr, expected):
        parser = build_parser()
        args = parser.parse_args(cli_args)
        assert getattr(args, attr) == expected

    def test_tenant_flag_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["--tenant", "acme_hvac", "graph", "schema"])
        assert args.tenant == "acme_hvac"

    def test_tenant_default_none(self):
        parser = build_parser()
        args = parser.parse_args(["graph", "schema"])
        assert args.tenant is None

    def test_db_takes_precedence_over_tenant(self, tmp_path):
        from ohm.cli import _get_db

        db_path = str(tmp_path / "custom.duckdb")
        parser = build_parser()
        args = parser.parse_args(["--db", db_path, "--tenant", "acme_hvac", "graph", "schema"])
        conn = _get_db(args)
        try:
            result = conn.execute("SELECT 1").fetchone()
            assert result[0] == 1
        finally:
            conn.close()

    def test_tenant_uses_tenant_manager(self, tmp_path):
        from ohm.cli import _get_db
        from ohm.tenant import TenantManager

        tenants_dir = tmp_path / "tenants"
        tm = TenantManager(tenants_dir, max_cached=5)
        tm.provision("acme_hvac")
        tm.close()

        parser = build_parser()
        args = parser.parse_args(["--tenant", "acme_hvac", "graph", "schema"])
        args.db = None
        import os

        os.environ["OHM_TENANTS_DIR"] = str(tenants_dir)
        try:
            conn = _get_db(args)
            total = conn.execute("SELECT COUNT(*) FROM ohm_nodes").fetchone()
            non_agent = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE type != 'agent'").fetchone()
            assert total[0] > 0, "Tenant DB should have seed agents"
            assert non_agent[0] == 0, "Tenant DB should have no user data, only seed agents"
        finally:
            conn.close()
            del os.environ["OHM_TENANTS_DIR"]

    def test_tenant_invalid_rejected(self):
        parser = build_parser()
        args = parser.parse_args(["--tenant", "../etc/passwd", "graph", "schema"])
        from ohm.cli import _get_db

        with pytest.raises(ValueError, match="path traversal"):
            _get_db(args)
