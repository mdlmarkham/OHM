"""
OHM CLI — self-documenting command-line interface for the knowledge graph.

Provides both a Click-style interface (via main()) and an argparse-style
interface (via build_parser()) for programmatic and test use.
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Optional

from .schema import (
    EDGE_TYPES,
    NODE_TYPES,
    LAYER_DESCRIPTIONS,
    VALID_LAYERS,
    VALID_NODE_TYPES,
    initialize_schema,
)
from .exceptions import (
    OHMError,
    Exit_codes,
    DaemonNotRunningError,
    GraphNotFoundError,
    AuthenticationError,
    PermissionDeniedError,
    NodeNotFoundError,
    EdgeNotFoundError,
    ValidationError,
    ConfigurationError,
)


def _get_store(agent_name: str = "ohm"):
    """Get a store instance."""
    from .store import OhmStore
    return OhmStore(agent_name=agent_name)


def _format_output(data, fmt: str = "text"):
    """Format output for display."""
    if fmt == "json":
        click_echo(json.dumps(data, indent=2, default=str))
    else:
        if isinstance(data, list):
            for row in data:
                _format_row(row)
        elif isinstance(data, dict):
            _format_row(data)
        else:
            print(data)


def _format_row(row: dict):
    """Format a single row for human-readable output."""
    if "from_node" in row and "to_node" in row:
        layer = row.get("layer", "?")
        edge_type = row.get("edge_type", "?")
        conf = row.get("confidence", "")
        agent = row.get("created_by", "?")
        depth = row.get("depth", "") or row.get("hop", "")
        challenge = row.get("challenge_type", "")

        conf_str = f"{conf:.2f}" if conf else ""
        agent_str = f"({agent})" if agent else ""
        depth_str = f"[d{depth}]" if depth else ""
        challenge_str = f" ← {challenge}" if challenge else ""

        print(
            f"{layer}: {edge_type} {row['from_node']} → {row['to_node']} "
            f"{conf_str} {agent_str} {depth_str}{challenge_str}"
        )
    elif "impacted_node" in row:
        print(
            f"  {'  ' * (row.get('depth', 1) - 1)}→ {row['impacted_node']} "
            f"({row.get('edge_type', '?')}, {row.get('layer', '?')}, "
            f"conf: {row.get('confidence', '?')}, by: {row.get('created_by', '?')})"
        )
    elif "agent_name" in row and "current_focus" in row:
        focus = row.get("current_focus", "idle")
        patterns = row.get("active_patterns", [])
        last_sync = row.get("last_sync", "never")
        print(f"  {row['agent_name']}: {focus} [patterns: {patterns}] (last sync: {last_sync})")
    elif "cnt" in row:
        print(f"  {row.get('layer', row.get('type', 'total'))}: {row['cnt']}")
    else:
        parts = [f"{k}={v}" for k, v in row.items() if v is not None]
        print("  " + ", ".join(parts))


def click_echo(msg):
    """Print without Click dependency."""
    print(msg)


# ── Argparse Builder (for tests) ───────────────────────────

def build_parser():
    """Build an argparse parser for the OHM CLI. Used by tests."""
    parser = argparse.ArgumentParser(prog="ohm", description="OHM — Shared awareness, individual judgment.")
    parser.add_argument("--version", action="store_true", help="Show version")
    parser.add_argument("--format", dest="format", choices=["text", "json"], default="text",
                        help="Output format")
    parser.add_argument("--agent", dest="agent", default="ohm", help="Agent name for attribution")

    subparsers = parser.add_subparsers(dest="command")

    # graph subcommand
    graph_parser = subparsers.add_parser("graph", help="Knowledge graph operations")
    graph_sub = graph_parser.add_subparsers(dest="graph_command")

    # graph status
    graph_sub.add_parser("status", help="Show graph status")

    # graph schema
    graph_sub.add_parser("schema", help="Show schema")

    # graph layers
    graph_sub.add_parser("layers", help="Show L1-L4 layers")

    # graph write
    write_p = graph_sub.add_parser("write", help="Write a node or edge")
    write_p.add_argument("--id", dest="node_id", help="Node ID")
    write_p.add_argument("--label", help="Node label")
    write_p.add_argument("--type", dest="node_type", help="Node type")
    write_p.add_argument("--content", help="Node content")
    write_p.add_argument("--from", dest="from_node", help="Source node ID")
    write_p.add_argument("--to", dest="to_node", help="Target node ID")
    write_p.add_argument("--edge-type", dest="edge_type", help="Edge type")
    write_p.add_argument("--layer", help="Layer")
    write_p.add_argument("--confidence", type=float, help="Confidence score")
    write_p.add_argument("--condition", help="Condition for edge")
    write_p.add_argument("--provenance", help="Provenance")
    write_p.add_argument("--tags", help="Comma-separated tags")
    write_p.add_argument("--visibility", default="team", help="Visibility")

    # graph neighborhood
    nb_p = graph_sub.add_parser("neighborhood", help="Show neighborhood of a node")
    nb_p.add_argument("node_id", help="Node ID")
    nb_p.add_argument("--depth", type=int, default=3, help="Traversal depth")
    nb_p.add_argument("--layer", help="Filter by layer")
    nb_p.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")

    # graph path
    path_p = graph_sub.add_parser("path", help="Find shortest path")
    path_p.add_argument("from_node", help="Source node ID")
    path_p.add_argument("to_node", help="Target node ID")
    path_p.add_argument("--max-depth", type=int, default=5, help="Maximum path length")

    # graph impact
    impact_p = graph_sub.add_parser("impact", help="Downstream impact analysis")
    impact_p.add_argument("node_id", help="Node ID")
    impact_p.add_argument("--depth", type=int, default=5, help="Downstream depth")

    # graph confidence
    conf_p = graph_sub.add_parser("confidence", help="Audit confidence for an edge")
    conf_p.add_argument("edge_id", help="Edge ID")

    # graph challenge
    chal_p = graph_sub.add_parser("challenge", help="Challenge an edge")
    chal_p.add_argument("edge_id", help="Edge ID")
    chal_p.add_argument("--reason", required=True, help="Reason for challenge")
    chal_p.add_argument("--confidence", type=float, default=0.5, help="Confidence score")
    chal_p.add_argument("--type", dest="challenge_type", default="CHALLENGED_BY",
                        choices=["CHALLENGED_BY", "CONTRADICTS", "REFINES"])

    # graph support
    sup_p = graph_sub.add_parser("support", help="Support an edge")
    sup_p.add_argument("edge_id", help="Edge ID")
    sup_p.add_argument("--reason", required=True, help="Reason for support")
    sup_p.add_argument("--confidence", type=float, default=0.85, help="Confidence score")

    # graph observe
    obs_p = graph_sub.add_parser("observe", help="Create an observation")
    obs_p.add_argument("node_id", help="Node ID")
    obs_p.add_argument("--type", dest="obs_type", required=True, help="Observation type")
    obs_p.add_argument("--value", type=float, help="Observed value")
    obs_p.add_argument("--baseline", type=float, help="Expected baseline")
    obs_p.add_argument("--sigma", type=float, help="Standard deviations from baseline")
    obs_p.add_argument("--source", help="Source of observation")

    # graph listen
    listen_p = graph_sub.add_parser("listen", help="Change feed since last check")
    listen_p.add_argument("--since", help="ISO timestamp")

    # graph stats
    graph_sub.add_parser("stats", help="Graph statistics")

    # state subcommand
    state_parser = subparsers.add_parser("state", help="Hive mind awareness")
    state_sub = state_parser.add_subparsers(dest="state_command")

    # state set
    set_p = state_sub.add_parser("set", help="Set agent state")
    set_p.add_argument("focus", nargs="+", help="Current focus")

    # state show
    show_p = state_sub.add_parser("show", help="Show agent state")
    show_p.add_argument("agent", nargs="?", default=None, help="Agent name")

    # state who-is-working-on
    who_p = state_sub.add_parser("who-is-working-on", help="Find agents by topic")
    who_p.add_argument("topic", nargs="+", help="Topic to search")

    # state history
    state_sub.add_parser("history", help="Show state history")

    # serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Daemon management")
    serve_sub = serve_parser.add_subparsers(dest="serve_command")

    # serve start
    start_p = serve_sub.add_parser("start", help="Start ohmd daemon")
    start_p.add_argument("--host", default="127.0.0.1", help="Bind address")
    start_p.add_argument("--port", type=int, default=8710, help="Port")
    start_p.add_argument("--db", default=None, help="DuckDB file path")
    start_p.add_argument("--config", default=None, help="Config file path")

    # serve status
    serve_sub.add_parser("status", help="Check if ohmd is running")

    # serve token
    token_p = serve_sub.add_parser("token", help="Generate auth token")
    token_p.add_argument("agent_name", help="Agent name for token")

    # snapshot subcommand
    snap_p = subparsers.add_parser("snapshot", help="Query graph at a point in time")
    snap_p.add_argument("timestamp", help="ISO timestamp")
    snap_p.add_argument("--node", help="Filter by node ID")

    # diff subcommand
    diff_p = subparsers.add_parser("diff", help="Show changes between timestamps")
    diff_p.add_argument("from_ts", help="From timestamp")
    diff_p.add_argument("to_ts", help="To timestamp")
    diff_p.add_argument("--layer", help="Filter by layer")
    diff_p.add_argument("--agent", help="Filter by agent")

    return parser


# ── Main entry point ────────────────────────────────────────

def main():
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print("ohm 0.1.0")
        return 0

    if not args.command:
        parser.print_help()
        return 0

    fmt = getattr(args, "format", "text")
    agent = getattr(args, "agent", "ohm")

    try:
        if args.command == "graph":
            return _handle_graph(args, fmt, agent)
        elif args.command == "state":
            return _handle_state(args, fmt, agent)
        elif args.command == "serve":
            return _handle_serve(args, fmt, agent)
        elif args.command == "snapshot":
            return _handle_snapshot(args, fmt)
        elif args.command == "diff":
            return _handle_diff(args, fmt)
    except OHMError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.exit_code

    return 0


def _handle_graph(args, fmt, agent):
    """Handle graph subcommands."""
    cmd = args.graph_command

    if cmd == "status":
        with _get_store(agent_name=agent) as store:
            _format_output(store.status(), fmt)

    elif cmd == "schema":
        data = {
            "node_types": sorted(VALID_NODE_TYPES),
            "edge_types": EDGE_TYPES,
            "layers": LAYER_DESCRIPTIONS,
        }
        _format_output(data, fmt)

    elif cmd == "layers":
        for layer, desc in LAYER_DESCRIPTIONS.items():
            print(f"\n{layer}: {desc['name']}")
            print(f"  Question: {desc['question']}")
            print(f"  Ownership: {desc['ownership']}")
            print(f"  Confidence: {desc['confidence']}")
            print(f"  Example: {desc['example']}")

    elif cmd == "write":
        with _get_store(agent_name=agent) as store:
            if args.node_id and args.label:
                tag_list = [t.strip() for t in args.tags.split(",")] if args.tags else None
                result = store.write_node(
                    id=args.node_id, label=args.label, type=args.node_type or "concept",
                    content=args.content, confidence=args.confidence or 1.0,
                    visibility=args.visibility, provenance=args.provenance, tags=tag_list,
                )
                _format_output(result, fmt)

            if args.from_node and args.to_node and args.edge_type:
                result = store.write_edge(
                    from_node=args.from_node, to_node=args.to_node,
                    edge_type=args.edge_type, layer=args.layer or "L3",
                    confidence=args.confidence, condition=args.condition,
                    provenance=args.provenance,
                )
                _format_output(result, fmt)

    elif cmd == "neighborhood":
        with _get_store(agent_name=agent) as store:
            from . import graph as _graph
            direction = getattr(args, "direction", None)
            sql, params = _graph.build_neighborhood_query(
                args.node_id, args.depth, args.layer, direction
            )
            results = store.execute(sql, params)
            _format_output(results, fmt)

    elif cmd == "path":
        with _get_store(agent_name=agent) as store:
            from . import graph as _graph
            sql, params = _graph.build_path_query(args.from_node, args.to_node, args.max_depth)
            results = store.execute(sql, params)
            if not results:
                print(f"No path found from {args.from_node} to {args.to_node}")
            else:
                _format_output(results, fmt)

    elif cmd == "impact":
        with _get_store(agent_name=agent) as store:
            from . import graph as _graph
            sql, params = _graph.build_impact_query(args.node_id, args.depth)
            results = store.execute(sql, params)
            if not results:
                print(f"No downstream impact from {args.node_id}")
            else:
                print(f"Impact analysis for {args.node_id}:")
                _format_output(results, fmt)

    elif cmd == "confidence":
        with _get_store(agent_name=agent) as store:
            from . import graph as _graph
            sql, params = _graph.build_confidence_audit_query(args.edge_id)
            results = store.execute(sql, params)
            if not results:
                print(f"Edge {args.edge_id} not found")
            else:
                _format_output(results, fmt)

    elif cmd == "challenge":
        with _get_store(agent_name=agent) as store:
            result = store.challenge_edge(
                args.edge_id, args.reason, args.confidence, args.challenge_type
            )
            if result:
                _format_output(result, fmt)
            else:
                print(f"Edge {args.edge_id} not found", file=sys.stderr)
                return 1

    elif cmd == "support":
        with _get_store(agent_name=agent) as store:
            result = store.challenge_edge(args.edge_id, args.reason, args.confidence, "SUPPORTS")
            if result:
                _format_output(result, fmt)
            else:
                print(f"Edge {args.edge_id} not found", file=sys.stderr)
                return 1

    elif cmd == "observe":
        with _get_store(agent_name=agent) as store:
            result = store.write_observation(
                args.node_id, args.obs_type, args.value, args.baseline,
                args.sigma, args.source,
            )
            _format_output(result, fmt)

    elif cmd == "listen":
        with _get_store(agent_name=agent) as store:
            since = args.since
            if not since:
                state = store.get_agent_state(agent)
                if state and state.get("last_sync"):
                    since = state["last_sync"]
                else:
                    print("No last-check timestamp. Use --since with an ISO timestamp.")
                    return 0
            from . import graph as _graph
            sql, params = _graph.build_change_feed_query(since, agent_name=agent)
            results = store.execute(sql, params)
            if not results:
                print(f"No changes since {since}")
            else:
                print(f"\nChanges since {since}:\n")
                for row in results:
                    layer = row.get("layer", "?")
                    op = row.get("operation", "?")
                    row_agent = row.get("agent_name", "?")
                    print(f"  [{layer}] {row_agent} {op} in {row.get('table_name', '?')}")
                print(f"\n{len(results)} changes, from {len(set(r['agent_name'] for r in results))} agents")

    elif cmd == "stats":
        from .queries import query_stats
        import duckdb
        db_path = _get_db_path()
        conn = duckdb.connect(db_path)
        try:
            stats = query_stats(conn)
            _format_output(stats, fmt)
        finally:
            conn.close()

    return 0


def _handle_state(args, fmt, agent):
    """Handle state subcommands."""
    cmd = args.state_command

    if cmd == "set":
        with _get_store(agent_name=agent) as store:
            focus = " ".join(args.focus)
            result = store.update_agent_state(current_focus=focus)
            _format_output(result, fmt)

    elif cmd == "show":
        with _get_store() as store:
            if args.agent:
                result = store.get_agent_state(args.agent)
                if not result:
                    print(f"Agent {args.agent} not found")
                else:
                    _format_output(result, fmt)
            else:
                results = store.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name")
                if not results:
                    print("No agents registered")
                else:
                    print("Active agents:\n")
                    _format_output(results, fmt)

    elif cmd == "who-is-working-on":
        with _get_store() as store:
            topic = " ".join(args.topic)
            results = store.who_is_working_on(topic)
            if not results:
                print(f"No agents working on '{topic}'")
            else:
                print(f"Agents working on '{topic}':\n")
                _format_output(results, fmt)

    elif cmd == "history":
        with _get_store(agent_name=agent) as store:
            results = store.execute(
                "SELECT * FROM ohm_agent_state ORDER BY agent_name"
            )
            _format_output(results, fmt)

    return 0


def _handle_serve(args, fmt, agent):
    """Handle serve subcommands."""
    cmd = args.serve_command

    if cmd == "start":
        from .server import load_config, run_server
        from .store import OhmStore

        cfg = load_config(getattr(args, "config", None))
        if args.host != "127.0.0.1":
            cfg["host"] = args.host
        if args.port != 8710:
            cfg["port"] = args.port
        if args.db:
            cfg["db_path"] = args.db

        store = OhmStore(db_path=cfg["db_path"], agent_name="ohmd")
        print(f"Starting OHM daemon on {cfg['host']}:{cfg['port']}")
        print(f"Database: {cfg['db_path']}")

        try:
            run_server(cfg, store)
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            store.close()

    elif cmd == "status":
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request("http://127.0.0.1:8710/status")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
                _format_output(data, fmt)
        except urllib.error.URLError:
            print("ohmd is not running (no response on port 8710)")
        except Exception as e:
            print(f"Error connecting to ohmd: {e}")

    elif cmd == "token":
        from .server import load_config
        import secrets
        cfg = load_config(getattr(args, "config", None))
        token = secrets.token_urlsafe(32)
        cfg.setdefault("tokens", {})[args.agent_name] = token

        import os
        from pathlib import Path
        config_path = Path(os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json")))
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=2)

        print(f"Token for {args.agent_name}: {token}")
        print(f"Config saved to {config_path}")
        print(f"\nUsage: OHM_TOKEN={token} ohm graph status")

    return 0


def _handle_snapshot(args, fmt):
    """Handle snapshot subcommand."""
    print(f"Snapshot at {args.timestamp} not yet implemented (Phase 2)")
    return 0


def _handle_diff(args, fmt):
    """Handle diff subcommand."""
    print(f"Diff from {args.from_ts} to {args.to_ts} not yet implemented (Phase 2)")
    return 0


def _get_db_path():
    """Get the database path from environment or default."""
    import os
    from pathlib import Path
    return os.environ.get("OHM_DB_PATH", str(Path.home() / ".ohm" / "ohm.duckdb"))


if __name__ == "__main__":
    sys.exit(main())