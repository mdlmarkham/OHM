"""OHM CLI — command tree and argument parsing.

Command structure (from docs/cli.md):
    ohm serve {start,stop,status,config}
    ohm graph {schema,layers,status,query,neighborhood,write,challenge,
               support,listen,confidence,impact,path,stats}
    ohm state {show,who-is-working-on,history}
    ohm snapshot
    ohm diff
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING, NoReturn

from ohm.exceptions import OHMError

if TYPE_CHECKING:
    import duckdb


def build_parser() -> argparse.ArgumentParser:
    """Build the full OHM CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="ohm",
        description="Shared awareness, individual judgment. Multi-agent knowledge graph.",
        epilog="See 'ohm <command> --help' for more information on a specific command.",
    )
    parser.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format (default: human)",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help="Agent name for audit trail (default: $OHM_ACTOR or detected)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Database path (default: auto-discover)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print version information",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── serve ────────────────────────────────────────────────────────────
    serve_parser = subparsers.add_parser("serve", help="Manage the ohmd daemon")
    serve_sub = serve_parser.add_subparsers(dest="serve_command", help="Daemon commands")

    serve_start = serve_sub.add_parser("start", help="Start ohmd (Quack server)")
    serve_start.add_argument("--port", type=int, default=9876, help="Quack server port")
    serve_start.add_argument("--config", default=None, help="Path to config file")

    serve_sub.add_parser("stop", help="Graceful shutdown")
    serve_sub.add_parser("status", help="Is ohmd running?")
    serve_sub.add_parser("config", help="Show current config")

    # ── graph ────────────────────────────────────────────────────────────
    graph_parser = subparsers.add_parser("graph", help="Graph operations")
    graph_sub = graph_parser.add_subparsers(dest="graph_command", help="Graph commands")

    # graph schema
    graph_sub.add_parser("schema", help="Show current node types, edge types, layers")

    # graph layers
    graph_sub.add_parser("layers", help="L1-L4 descriptions with examples")

    # graph status
    graph_sub.add_parser("status", help="Node count, edge count, schema version, active agents")

    # graph upgrade
    upgrade_parser = graph_sub.add_parser("upgrade", help="Apply pending schema migrations")
    upgrade_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be migrated without applying changes",
    )

    # graph stats
    graph_sub.add_parser("stats", help="Edge counts by layer, confidence distribution")

    # graph query
    query_parser = graph_sub.add_parser("query", help="Natural language or structured query")
    query_parser.add_argument("query_text", nargs="?", help="Query text")
    query_parser.add_argument("--type", dest="filter_type", help="Filter by edge type")
    query_parser.add_argument("--layer", choices=["L1", "L2", "L3", "L4"], help="Filter by layer")
    query_parser.add_argument("--owner", help="Filter by owning agent")
    query_parser.add_argument("--confidence-min", type=float, help="Minimum confidence (0-1)")
    query_parser.add_argument("--created-after", help="ISO timestamp filter")

    # graph neighborhood
    neighborhood_parser = graph_sub.add_parser("neighborhood", help="Bounded-depth traversal")
    neighborhood_parser.add_argument("node_id", help="Starting node ID")
    neighborhood_parser.add_argument("--depth", type=int, default=3, help="Max traversal depth")
    neighborhood_parser.add_argument(
        "--layer", choices=["L1", "L2", "L3", "L4"], help="Filter by layer",
    )
    neighborhood_parser.add_argument(
        "--direction", choices=["outgoing", "incoming", "both"], default="both",
    )

    # graph write
    write_parser = graph_sub.add_parser("write", help="Create nodes and edges with attribution")
    write_parser.add_argument("--from", dest="from_node", required=True, help="Source node ID")
    write_parser.add_argument("--to", dest="to_node", required=True, help="Target node ID")
    write_parser.add_argument("--type", dest="edge_type", required=True, help="Edge type")
    write_parser.add_argument(
        "--layer", choices=["L1", "L2", "L3", "L4"], default="L3", help="Layer",
    )
    write_parser.add_argument(
        "--confidence", type=float, default=0.7, help="Confidence score (0-1)",
    )
    write_parser.add_argument("--condition", help="Context condition string")
    write_parser.add_argument("--provenance", help="Source attribution")

    # graph challenge
    challenge_parser = graph_sub.add_parser("challenge", help="Challenge an existing edge")
    challenge_parser.add_argument("edge_id", help="ID of the edge to challenge")
    challenge_parser.add_argument("--reason", required=True, help="Challenge rationale")
    challenge_parser.add_argument(
        "--confidence", type=float, default=0.5, help="Challenge confidence",
    )

    # graph support
    support_parser = graph_sub.add_parser("support", help="Support an existing edge")
    support_parser.add_argument("edge_id", help="ID of the edge to support")
    support_parser.add_argument("--reason", required=True, help="Support rationale")
    support_parser.add_argument("--confidence", type=float, default=0.7, help="Support confidence")

    # graph confidence
    confidence_parser = graph_sub.add_parser("confidence", help="Provenance and challenge audit")
    confidence_parser.add_argument("edge_id", help="Edge ID to audit")

    # graph listen
    listen_parser = graph_sub.add_parser("listen", help="Change feed since last check")
    listen_parser.add_argument("--since", help="ISO timestamp or 'last-check'")

    # graph impact
    impact_parser = graph_sub.add_parser("impact", help="Downstream failure impact analysis")
    impact_parser.add_argument("node_id", help="Node ID to analyze")
    impact_parser.add_argument("--depth", type=int, default=5, help="Max propagation depth")

    # graph path
    path_parser = graph_sub.add_parser("path", help="Shortest path between two nodes")
    path_parser.add_argument("from_node", help="Starting node ID")
    path_parser.add_argument("to_node", help="Target node ID")
    path_parser.add_argument("--max-depth", type=int, default=10, help="Max path length")

    # graph update
    update_parser = graph_sub.add_parser("update", help="Update your own edge")
    update_parser.add_argument("edge_id", help="ID of the edge to update")
    update_parser.add_argument("--confidence", type=float, help="New confidence score (0-1)")
    update_parser.add_argument("--provenance", help="Updated provenance")
    update_parser.add_argument("--condition", help="Updated condition")

    # graph observe
    observe_parser = graph_sub.add_parser("observe", help="Record an observation on a node")
    observe_parser.add_argument("node_id", help="Node ID to observe")
    observe_parser.add_argument(
        "--type", dest="obs_type", required=True,
        choices=["anomaly", "measurement", "pattern", "challenge", "support"],
        help="Observation type",
    )
    observe_parser.add_argument("--value", type=float, help="Observation value")
    observe_parser.add_argument("--baseline", type=float, help="Baseline value")
    observe_parser.add_argument("--sigma", type=float, help="Standard deviation")
    observe_parser.add_argument(
        "--source",
        choices=["signal", "research", "conversation", "analysis"],
        default="analysis", help="Observation source",
    )

    # ── state ────────────────────────────────────────────────────────────
    state_parser = subparsers.add_parser("state", help="Hive mind awareness")
    state_sub = state_parser.add_subparsers(dest="state_command", help="State commands")

    state_set = state_sub.add_parser("set", help="Set current focus")
    state_set.add_argument("focus", nargs="+", help="Current focus description")

    state_show = state_sub.add_parser("show", help="Show agent state")
    state_show.add_argument("agent", nargs="?", default=None, help="Agent name (default: self)")

    state_who = state_sub.add_parser("who-is-working-on", help="Find collaborators by topic")
    state_who.add_argument("topic", nargs="+", help="Topic to search for")

    state_sub.add_parser("history", help="Focus history")

    # ── snapshot ─────────────────────────────────────────────────────────
    snapshot_parser = subparsers.add_parser("snapshot", help="Query graph state at timestamp")
    snapshot_parser.add_argument("timestamp", help="ISO timestamp")
    snapshot_parser.add_argument("--node", help="Single node ID")
    snapshot_parser.add_argument("--edge", help="Single edge ID")

    # ── diff ─────────────────────────────────────────────────────────────
    diff_parser = subparsers.add_parser("diff", help="What changed between two timestamps")
    diff_parser.add_argument("from_ts", help="Starting ISO timestamp")
    diff_parser.add_argument("to_ts", help="Ending ISO timestamp")
    diff_parser.add_argument("--layer", choices=["L1", "L2", "L3", "L4"], help="Filter by layer")
    diff_parser.add_argument("--agent", help="Filter by agent")

    return parser


def main(argv: list[str] | None = None) -> NoReturn:
    """Entry point for the OHM CLI.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Never returns — calls sys.exit() with the appropriate exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from ohm import __version__
        print(f"ohm {__version__}")
        sys.exit(0)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        _dispatch(args)
    except OHMError as e:
        _print_error(e)
        sys.exit(e.exit_code)
    except Exception as e:
        _print_error(OHMError(str(e)))
        sys.exit(1)

    sys.exit(0)


def _dispatch(args: argparse.Namespace) -> None:
    """Route parsed arguments to the appropriate handler."""
    if args.command == "serve":
        _handle_serve(args)
    elif args.command == "graph":
        _handle_graph(args)
    elif args.command == "state":
        _handle_state(args)
    elif args.command == "agents":
        _handle_agents(args)
    elif args.command == "snapshot":
        _handle_snapshot(args)
    elif args.command == "diff":
        _handle_diff(args)


def _handle_serve(args: argparse.Namespace) -> None:
    """Handle 'ohm serve' subcommands."""
    cmd = args.serve_command
    if cmd == "start":
        print(f"Starting ohmd on port {args.port}...")
        # TODO: Implement ohmd daemon startup (OHM-y2i.3)
        print("ohmd started (placeholder)")
    elif cmd == "stop":
        print("Stopping ohmd...")
        # TODO: Implement graceful shutdown
        print("ohmd stopped (placeholder)")
    elif cmd == "status":
        print("ohmd status: not running (placeholder)")
    elif cmd == "config":
        print("ohmd config: (placeholder)")
    elif cmd == "token":
        _handle_serve_token(args)
    else:
        print(f"Unknown serve command: {cmd}")


def _handle_serve_token(args: argparse.Namespace) -> None:
    """Generate a token for an agent and save to config."""
    import json
    import secrets
    from pathlib import Path

    agent_name = args.agent_name
    token = secrets.token_urlsafe(32)

    config_path = args.config or os.environ.get(
        "OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json")
    )
    config_file = Path(config_path)
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config or start fresh
    if config_file.exists():
        with open(config_file) as f:
            config = json.load(f)
    else:
        config = {}

    # Add token
    config.setdefault("tokens", {})[agent_name] = token
    config.setdefault("roles", {})[agent_name] = args.role

    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"Token for {agent_name}: {token}")
    print(f"Role: {args.role}")
    print(f"Config saved to {config_file}")


def _handle_graph(args: argparse.Namespace) -> None:
    """Handle 'ohm graph' subcommands."""
    cmd = args.graph_command
    if cmd == "schema":
        _show_schema(args)
    elif cmd == "layers":
        _show_layers(args)
    elif cmd == "status":
        _show_status(args)
    elif cmd == "stats":
        _show_stats(args)
    elif cmd == "upgrade":
        _handle_upgrade(args)
    elif cmd == "query":
        _handle_query(args)
    elif cmd == "neighborhood":
        _handle_neighborhood(args)
    elif cmd == "write":
        _handle_write(args)
    elif cmd == "challenge":
        _handle_challenge(args)
    elif cmd == "support":
        _handle_support(args)
    elif cmd == "confidence":
        _handle_confidence(args)
    elif cmd == "listen":
        _handle_listen(args)
    elif cmd == "impact":
        _handle_impact(args)
    elif cmd == "path":
        _handle_path(args)
    elif cmd == "update":
        _handle_update(args)
    elif cmd == "observe":
        _handle_observe(args)
    else:
        print(f"Unknown graph command: {cmd}")


def _handle_state(args: argparse.Namespace) -> None:
    """Handle 'ohm state' subcommands."""
    cmd = args.state_command
    if cmd == "set":
        _handle_state_set(args)
    elif cmd == "show":
        _handle_state_show(args)
    elif cmd == "who-is-working-on":
        _handle_state_who(args)
    elif cmd == "history":
        print("Focus history: (placeholder)")
    else:
        print(f"Unknown state command: {cmd}")


def _handle_snapshot(args: argparse.Namespace) -> None:
    """Handle 'ohm snapshot' command."""
    print(f"Snapshot at {args.timestamp}: (placeholder)")


def _handle_diff(args: argparse.Namespace) -> None:
    """Handle 'ohm diff' command."""
    print(f"Diff from {args.from_ts} to {args.to_ts}: (placeholder)")


# ── Graph Command Implementations ───────────────────────────────────────────

def _get_db(args: argparse.Namespace) -> "duckdb.DuckDBPyConnection":
    """Open a database connection using args."""
    from ohm.db import connect
    return connect(args.db)


def _get_actor(args: argparse.Namespace) -> str:
    """Resolve the actor name from args or environment."""
    import os
    return args.actor or os.environ.get("OHM_ACTOR", "unknown")


def _show_status(args: argparse.Namespace) -> None:
    """Show graph status: node count, edge count, active agents, schema version."""
    from ohm.queries import query_stats
    from ohm.schema import get_schema_version

    conn = _get_db(args)
    try:
        stats = query_stats(conn)
        schema_version = get_schema_version(conn)
        if args.format == "json":
            import json
            stats["schema_version"] = schema_version
            print(json.dumps(stats, indent=2))
        else:
            print(f"Schema version: {schema_version}")
            print(f"Nodes:        {stats['total_nodes']}")
            print(f"Edges:        {stats['total_edges']}")
            print(f"Observations: {stats['total_observations']}")
            print(f"Active agents: {stats['active_agents']}")
            print(f"Challenge ratio: {stats['challenge_ratio']}")
    finally:
        conn.close()


def _handle_upgrade(args: argparse.Namespace) -> None:
    """Apply pending schema migrations."""
    from ohm.schema import SCHEMA_VERSION, MIGRATIONS, get_schema_version, initialize_schema

    conn = _get_db(args)
    try:
        current_version = get_schema_version(conn)
        if args.dry_run:
            pending = [(v, d) for v, d, _ in MIGRATIONS if current_version < v]
            if not pending:
                print(f"Schema is up to date (v{current_version})")
            else:
                print(f"Current schema: v{current_version}")
                print(f"Target schema:  v{SCHEMA_VERSION}")
                print(f"\nPending migrations:")
                for version, description in pending:
                    print(f"  v{version}: {description}")
        else:
            initialize_schema(conn)
            new_version = get_schema_version(conn)
            if new_version == current_version:
                print(f"Schema is up to date (v{current_version})")
            else:
                print(f"Schema upgraded: v{current_version} → v{new_version}")
    finally:
        conn.close()


def _show_stats(args: argparse.Namespace) -> None:
    """Show detailed graph statistics."""
    from ohm.queries import query_stats

    conn = _get_db(args)
    try:
        stats = query_stats(conn)
        if args.format == "json":
            import json
            print(json.dumps(stats, indent=2))
        else:
            print("── Edges by Layer ──")
            for layer, count in sorted(stats["edges_by_layer"].items()):
                print(f"  {layer}: {count}")
            print("\n── Edges by Type ──")
            for etype, count in sorted(stats["edges_by_type"].items(), key=lambda x: -x[1]):
                print(f"  {etype}: {count}")
            print("\n── Nodes by Type ──")
            for ntype, count in sorted(stats["nodes_by_type"].items(), key=lambda x: -x[1]):
                print(f"  {ntype}: {count}")
            print(f"\nTotal: {stats['total_nodes']} nodes, {stats['total_edges']} edges")
            print(f"Challenge ratio: {stats['challenge_ratio']}")
    finally:
        conn.close()


def _handle_query(args: argparse.Namespace) -> None:
    """Handle structured graph query."""
    conn = _get_db(args)
    try:
        # Structured query: use neighborhood as base, apply filters
        if args.filter_type or args.layer or args.owner or args.confidence_min:
            # For structured queries, scan all edges with filters
            conditions = []
            params = []
            if args.filter_type:
                conditions.append("edge_type = ?")
                params.append(args.filter_type)
            if args.layer:
                conditions.append("layer = ?")
                params.append(args.layer)
            if args.owner:
                conditions.append("created_by = ?")
                params.append(args.owner)
            if args.confidence_min is not None:
                conditions.append("confidence >= ?")
                params.append(args.confidence_min)

            where = " AND ".join(conditions) if conditions else "1=1"
            result = conn.execute(
                "SELECT * FROM ohm_edges WHERE " + where + " ORDER BY created_at DESC LIMIT 100",
                params,
            )
            from ohm.queries import _rows_to_dicts
            rows = _rows_to_dicts(result)
        elif args.query_text:
            # Natural language: search by label/content match
            result = conn.execute(
                "SELECT * FROM ohm_nodes WHERE label ILIKE ? OR content ILIKE ? LIMIT 50",
                [f"%{args.query_text}%", f"%{args.query_text}%"],
            )
            from ohm.queries import _rows_to_dicts
            rows = _rows_to_dicts(result)
        else:
            # No filters: show all nodes
            result = conn.execute("SELECT * FROM ohm_nodes ORDER BY created_at DESC LIMIT 100")
            from ohm.queries import _rows_to_dicts
            rows = _rows_to_dicts(result)

        if args.format == "json":
            import json
            print(json.dumps(rows, indent=2, default=str))
        else:
            for row in rows:
                if "label" in row:
                    print(f"  [{row.get('type', '?')}] {row['label']} ({row['id']})")
                elif "edge_type" in row:
                    print(
                        f"  [{row['layer']}] {row['edge_type']}: "
                        f"{row['from_node']} → {row['to_node']} "
                        f"(conf: {row.get('confidence', '?')})"
                    )
    finally:
        conn.close()


def _handle_neighborhood(args: argparse.Namespace) -> None:
    """Handle bounded-depth graph traversal."""
    from ohm.queries import query_neighborhood

    conn = _get_db(args)
    try:
        results = query_neighborhood(
            conn, args.node_id,
            depth=args.depth, layer=args.layer, direction=args.direction,
        )
        if args.format == "json":
            import json
            print(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                print(f"No edges found within {args.depth} hops of '{args.node_id}'")
                return
            for r in results:
                print(f"  [hop {r['hop']}] [{r['layer']}] {r['edge_type']}: "
                      f"{r['from_node']} → {r['to_node']} "
                      f"(conf: {r.get('confidence', '?')}, by: {r['created_by']})")
    finally:
        conn.close()


def _handle_write(args: argparse.Namespace) -> None:
    """Handle graph write: create nodes and edges."""
    from ohm.queries import create_edge, create_node, node_exists

    actor = _get_actor(args)
    conn = _get_db(args)
    try:
        # Auto-create nodes if they don't exist
        for node_id in [args.from_node, args.to_node]:
            if not node_exists(conn, node_id):
                create_node(conn, label=node_id, created_by=actor)

        edge_id = create_edge(
            conn,
            from_node=args.from_node,
            to_node=args.to_node,
            layer=args.layer,
            edge_type=args.edge_type,
            created_by=actor,
            confidence=args.confidence,
            condition=args.condition,
            provenance=args.provenance,
        )
        if args.format == "json":
            import json
            print(json.dumps({"edge_id": edge_id, "status": "created"}))
        else:
            print(f"Created edge: {args.from_node} --[{args.edge_type}]--> {args.to_node}")
            print(f"  ID: {edge_id}")
            print(f"  Layer: {args.layer}, Confidence: {args.confidence}")
    finally:
        conn.close()


def _handle_challenge(args: argparse.Namespace) -> None:
    """Handle edge challenge."""
    from ohm.queries import create_challenge

    actor = _get_actor(args)
    conn = _get_db(args)
    try:
        challenge_id = create_challenge(
            conn,
            edge_id=args.edge_id,
            reason=args.reason,
            created_by=actor,
            confidence=args.confidence,
        )
        if args.format == "json":
            import json
            print(json.dumps({"challenge_id": challenge_id, "status": "created"}))
        else:
            print(f"Challenged edge {args.edge_id}")
            print(f"  Challenge ID: {challenge_id}")
            print(f"  Reason: {args.reason}")
            print(f"  Confidence: {args.confidence}")
    finally:
        conn.close()


def _handle_support(args: argparse.Namespace) -> None:
    """Handle edge support."""
    from ohm.queries import create_support

    actor = _get_actor(args)
    conn = _get_db(args)
    try:
        support_id = create_support(
            conn,
            edge_id=args.edge_id,
            reason=args.reason,
            created_by=actor,
            confidence=args.confidence,
        )
        if args.format == "json":
            import json
            print(json.dumps({"support_id": support_id, "status": "created"}))
        else:
            print(f"Supported edge {args.edge_id}")
            print(f"  Support ID: {support_id}")
            print(f"  Reason: {args.reason}")
            print(f"  Confidence: {args.confidence}")
    finally:
        conn.close()


def _handle_confidence(args: argparse.Namespace) -> None:
    """Handle confidence audit for an edge."""
    from ohm.queries import query_confidence

    conn = _get_db(args)
    try:
        result = query_confidence(conn, args.edge_id)
        if args.format == "json":
            import json
            print(json.dumps(result, indent=2, default=str))
        else:
            if result["original"] is None:
                print(f"Edge not found: {args.edge_id}")
                return
            o = result["original"]
            print(f"── Edge {args.edge_id} ──")
            print(f"  Type:       {o['edge_type']}")
            print(f"  Layer:      {o['layer']}")
            print(f"  Confidence: {o['confidence']}")
            print(f"  Owner:      {o['created_by']}")
            print(f"  Created:    {o['created_at']}")
            if o.get("provenance"):
                print(f"  Provenance: {o['provenance']}")
            if o.get("condition"):
                print(f"  Condition:  {o['condition']}")

            if result["challenges"]:
                print(f"\n  Challenges ({len(result['challenges'])}):")
                for c in result["challenges"]:
                    print(
                        f"    • {c['created_by']} (conf: {c['confidence']}): "
                        f"{c.get('condition', '')}"
                    )
            if result["supports"]:
                print(f"\n  Support ({len(result['supports'])}):")
                for s in result["supports"]:
                    print(
                        f"    • {s['created_by']} (conf: {s['confidence']}): "
                        f"{s.get('condition', '')}"
                    )
    finally:
        conn.close()


def _handle_listen(args: argparse.Namespace) -> None:
    """Handle change feed query."""
    from ohm.queries import query_change_feed

    conn = _get_db(args)
    try:
        results = query_change_feed(conn, since=args.since)
        if args.format == "json":
            import json
            print(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                print("No changes found.")
                return
            print(f"Changes ({len(results)}):")
            for r in results:
                print(f"  [{r['occurred_at']}] {r['agent_name']} {r['operation']} "
                      f"{r['table_name']}.{r['row_id']}")
    finally:
        conn.close()


def _handle_impact(args: argparse.Namespace) -> None:
    """Handle downstream impact analysis."""
    from ohm.queries import query_impact

    conn = _get_db(args)
    try:
        results = query_impact(conn, args.node_id, depth=args.depth)
        if args.format == "json":
            import json
            print(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                print(f"No downstream impact found for '{args.node_id}'")
                return
            print(f"Impact analysis for '{args.node_id}' (depth ≤ {args.depth}):")
            for r in results:
                print(f"  [depth {r['depth']}] [{r['layer']}] {r['edge_type']}: "
                      f"{r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")
    finally:
        conn.close()


def _handle_path(args: argparse.Namespace) -> None:
    """Handle shortest path query."""
    from ohm.queries import query_path

    conn = _get_db(args)
    try:
        results = query_path(conn, args.from_node, args.to_node, max_depth=args.max_depth)
        if args.format == "json":
            import json
            print(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                print(f"No path found from '{args.from_node}' to '{args.to_node}' "
                      f"(max depth: {args.max_depth})")
                return
            print(f"Path from '{args.from_node}' to '{args.to_node}':")
            for r in results:
                print(f"  [{r['layer']}] {r['edge_type']}: "
                      f"{r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")
    finally:
        conn.close()


def _handle_update(args: argparse.Namespace) -> None:
    """Handle edge update — only the owning agent can update their own edges."""
    from ohm.boundary import enforce_write_boundary
    from ohm.queries import edge_exists

    actor = _get_actor(args)
    conn = _get_db(args)
    try:
        if not edge_exists(conn, args.edge_id):
            print(f"Edge not found: {args.edge_id}")
            return

        enforce_write_boundary(conn, actor, args.edge_id)

        updates = []
        params = []
        if args.confidence is not None:
            updates.append("confidence = ?")
            params.append(args.confidence)
        if args.provenance is not None:
            updates.append("provenance = ?")
            params.append(args.provenance)
        if args.condition is not None:
            updates.append("condition = ?")
            params.append(args.condition)

        if not updates:
            print("No updates specified. Use --confidence, --provenance, or --condition.")
            return

        updates.append("updated_at = CURRENT_TIMESTAMP")
        updates.append("updated_by = ?")
        params.append(actor)
        params.append(args.edge_id)

        conn.execute(
            "UPDATE ohm_edges SET " + ", ".join(updates) + " WHERE id = ?",
            params,
        )
        if args.format == "json":
            import json
            print(json.dumps({"edge_id": args.edge_id, "status": "updated"}))
        else:
            print(f"Updated edge {args.edge_id}")
    finally:
        conn.close()


def _handle_observe(args: argparse.Namespace) -> None:
    """Handle observation creation on a node."""
    from ohm.queries import create_observation, node_exists

    actor = _get_actor(args)
    conn = _get_db(args)
    try:
        if not node_exists(conn, args.node_id):
            print(f"Node not found: {args.node_id}")
            return

        obs_id = create_observation(
            conn,
            node_id=args.node_id,
            obs_type=args.obs_type,
            value=args.value,
            baseline=args.baseline,
            sigma=args.sigma,
            source=args.source,
            created_by=actor,
        )
        if args.format == "json":
            import json
            print(json.dumps({"observation_id": obs_id, "status": "created"}))
        else:
            print(f"Recorded {args.obs_type} observation on {args.node_id}")
            print(f"  ID: {obs_id}")
            if args.value is not None:
                print(f"  Value: {args.value}")
            if args.sigma is not None:
                print(f"  Sigma: {args.sigma}")
    finally:
        conn.close()


# ── State Command Implementations ───────────────────────────────────────────

def _handle_state_set(args: argparse.Namespace) -> None:
    """Set agent focus."""
    from ohm.queries import set_agent_state

    actor = _get_actor(args)
    focus = " ".join(args.focus)
    conn = _get_db(args)
    try:
        set_agent_state(conn, agent_name=actor, focus=focus)
        print(f"Focus set for {actor}: {focus}")
    finally:
        conn.close()


def _handle_state_show(args: argparse.Namespace) -> None:
    """Show agent state."""
    from ohm.queries import query_agent_state

    actor = _get_actor(args)
    target = args.agent or actor
    conn = _get_db(args)
    try:
        results = query_agent_state(conn, agent_name=target)
        if args.format == "json":
            import json
            print(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                print(f"No state found for agent '{target}'")
                return
            for s in results:
                print(f"Agent: {s['agent_name']}")
                print(f"  Focus:       {s.get('current_focus', '(none)')}")
                print(f"  Confidence:  {s.get('confidence_threshold', 0.7)}")
                print(f"  Last sync:   {s.get('last_sync', 'never')}")
                print(f"  Services:    {s.get('available_services', '')}")
    finally:
        conn.close()


def _handle_state_who(args: argparse.Namespace) -> None:
    """Find agents working on a topic."""
    from ohm.queries import query_agent_state

    topic = " ".join(args.topic)
    conn = _get_db(args)
    try:
        results = query_agent_state(conn)
        matches = [
            r for r in results
            if r.get("current_focus") and topic.lower() in r["current_focus"].lower()
        ]
        if args.format == "json":
            import json
            print(json.dumps(matches, indent=2, default=str))
        else:
            if not matches:
                print(f"No agents found working on '{topic}'")
                return
            print(f"Agents working on '{topic}':")
            for m in matches:
                print(f"  • {m['agent_name']}: {m['current_focus']}")
    finally:
        conn.close()


def _show_schema(args: argparse.Namespace) -> None:
    """Display the current graph schema."""
    from ohm.schema import LAYER_EDGE_TYPES, VALID_NODE_TYPES

    lines = ["── Node Types ──"]
    for nt in sorted(VALID_NODE_TYPES):
        lines.append(f"  • {nt}")
    lines.append("")
    lines.append("── Edge Types by Layer ──")
    for layer in ["L1", "L2", "L3", "L4"]:
        types = ", ".join(sorted(LAYER_EDGE_TYPES[layer]))
        lines.append(f"  {layer}: {types}")
    print("\n".join(lines))


def _show_layers(args: argparse.Namespace) -> None:
    """Display layer descriptions."""
    layers = [
        ("L1: Structure", "Fully shared", "Communal",
         "CONTAINS, BELONGS_TO, HAS_COMPONENT",
         '"Hungary has a constitution"'),
        ("L2: Flow", "Shared + attributed", "Proposing agent",
         "DERIVES_FROM, INFLUENCES, REFERENCES, USES",
         '"This idea derives from that source"'),
        ("L3: Knowledge", "Agent-owned, challengeable", "Creating agent",
         "CAUSES, CORRELATES_WITH, PREDICTS, EXPLAINS, CHALLENGED_BY, SUPPORTS",
         '"AND→OR conversion conf: 0.94 (Métis)"'),
        ("L4: Prospect", "Agent-owned, visible", "Forecasting agent",
         "EXPECTS, PLANS, RISKS, DEPENDS_ON",
         '"Democratic institutions will hold conf: 0.65 (Clio)"'),
    ]
    lines: list[str] = []
    for name, sharing, owner, types, example in layers:
        lines.append(f"── {name} ──")
        lines.append(f"  Sharing:    {sharing}")
        lines.append(f"  Ownership:  {owner}")
        lines.append(f"  Edge types: {types}")
        lines.append(f"  Example:    {example}")
        lines.append("")
    print("\n".join(lines))


def _print_error(error: OHMError) -> None:
    """Print a formatted error message to stderr."""
    prefix = f"[{error.exit_code}]" if error.exit_code else "[!]"
    msg = f"{prefix} {error}"
    if error.correlation_id:
        msg += f" (correlation_id: {error.correlation_id})"
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    main()
