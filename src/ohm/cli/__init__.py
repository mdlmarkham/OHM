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
import sys
from typing import NoReturn

from ohm.exceptions import EXIT_CODES, OHMError


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
    graph_sub.add_parser("status", help="Node count, edge count, last sync, active agents")

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
    neighborhood_parser.add_argument("--layer", choices=["L1", "L2", "L3", "L4"], help="Filter by layer")
    neighborhood_parser.add_argument(
        "--direction", choices=["outgoing", "incoming", "both"], default="both",
    )

    # graph write
    write_parser = graph_sub.add_parser("write", help="Create nodes and edges with attribution")
    write_parser.add_argument("--from", dest="from_node", required=True, help="Source node ID")
    write_parser.add_argument("--to", dest="to_node", required=True, help="Target node ID")
    write_parser.add_argument("--type", dest="edge_type", required=True, help="Edge type")
    write_parser.add_argument("--layer", choices=["L1", "L2", "L3", "L4"], default="L3", help="Layer")
    write_parser.add_argument("--confidence", type=float, default=0.7, help="Confidence score (0-1)")
    write_parser.add_argument("--condition", help="Context condition string")
    write_parser.add_argument("--provenance", help="Source attribution")

    # graph challenge
    challenge_parser = graph_sub.add_parser("challenge", help="Challenge an existing edge")
    challenge_parser.add_argument("edge_id", help="ID of the edge to challenge")
    challenge_parser.add_argument("--reason", required=True, help="Challenge rationale")
    challenge_parser.add_argument("--confidence", type=float, default=0.5, help="Challenge confidence")

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
    elif args.command == "snapshot":
        _handle_snapshot(args)
    elif args.command == "diff":
        _handle_diff(args)


def _handle_serve(args: argparse.Namespace) -> None:
    """Handle 'ohm serve' subcommands."""
    cmd = args.serve_command
    if cmd == "start":
        print(f"Starting ohmd on port {args.port}...")
        # TODO: Implement ohmd daemon startup (OHM-346.1)
        print("ohmd started (placeholder)")
    elif cmd == "stop":
        print("Stopping ohmd...")
        # TODO: Implement graceful shutdown
        print("ohmd stopped (placeholder)")
    elif cmd == "status":
        print("ohmd status: not running (placeholder)")
    elif cmd == "config":
        print("ohmd config: (placeholder)")
    else:
        print(f"Unknown serve command: {cmd}")


def _handle_graph(args: argparse.Namespace) -> None:
    """Handle 'ohm graph' subcommands."""
    cmd = args.graph_command
    if cmd == "schema":
        _show_schema(args)
    elif cmd == "layers":
        _show_layers(args)
    elif cmd == "status":
        print("Graph status: (placeholder)")
    elif cmd == "stats":
        print("Graph stats: (placeholder)")
    elif cmd == "query":
        print(f"Query: {args.query_text or '(all)'} (placeholder)")
    elif cmd == "neighborhood":
        print(f"Neighborhood of {args.node_id} (depth={args.depth}): (placeholder)")
    elif cmd == "write":
        print(f"Writing edge: {args.from_node} --[{args.edge_type}]--> {args.to_node} (placeholder)")
    elif cmd == "challenge":
        print(f"Challenging edge {args.edge_id}: {args.reason} (placeholder)")
    elif cmd == "support":
        print(f"Supporting edge {args.edge_id}: {args.reason} (placeholder)")
    elif cmd == "confidence":
        print(f"Confidence audit for edge {args.edge_id}: (placeholder)")
    elif cmd == "listen":
        since = args.since or "last-check"
        print(f"Change feed since {since}: (placeholder)")
    elif cmd == "impact":
        print(f"Impact analysis for {args.node_id} (depth={args.depth}): (placeholder)")
    elif cmd == "path":
        print(f"Path from {args.from_node} to {args.to_node}: (placeholder)")
    else:
        print(f"Unknown graph command: {cmd}")


def _handle_state(args: argparse.Namespace) -> None:
    """Handle 'ohm state' subcommands."""
    cmd = args.state_command
    if cmd == "set":
        focus = " ".join(args.focus)
        print(f"Setting focus: {focus} (placeholder)")
    elif cmd == "show":
        agent = args.agent or "(self)"
        print(f"State for {agent}: (placeholder)")
    elif cmd == "who-is-working-on":
        topic = " ".join(args.topic)
        print(f"Who is working on '{topic}': (placeholder)")
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
