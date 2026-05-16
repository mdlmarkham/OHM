"""
OHM CLI — self-documenting command-line interface for the knowledge graph.

Usage:
    ohm graph write --from X --to Y --type CAUSES --confidence 0.94
    ohm graph neighborhood <node-id> [--depth N] [--layer L]
    ohm graph listen [--since <timestamp>]
    ohm graph challenge <edge-id> --reason "..." --confidence 0.5
    ohm state "researching AND→OR patterns"
    ohm graph status
    ohm graph schema
    ohm serve
"""

import click
import json
import os
import sys
from pathlib import Path

from .store import OhmStore
from .graph import (
    build_neighborhood_query,
    build_path_query,
    build_impact_query,
    build_confidence_audit_query,
    build_change_feed_query,
)
from .schema import EDGE_TYPES, NODE_TYPES, LAYER_DESCRIPTIONS


def _get_store(agent_name: str = "ohm") -> OhmStore:
    """Get a store instance."""
    return OhmStore(agent_name=agent_name)


def _format_output(data, fmt: str = "text"):
    """Format output for display."""
    if fmt == "json":
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        if isinstance(data, list):
            for row in data:
                _format_row(row)
        elif isinstance(data, dict):
            _format_row(data)
        else:
            click.echo(data)


def _format_row(row: dict):
    """Format a single row for human-readable output."""
    if "from_node" in row and "to_node" in row:
        # Edge row
        layer = row.get("layer", "?")
        edge_type = row.get("edge_type", "?")
        conf = row.get("confidence", "")
        agent = row.get("created_by", "?")
        depth = row.get("depth", "")
        challenge = row.get("challenge_type", "")

        conf_str = f"{conf:.2f}" if conf else ""
        agent_str = f"({agent})" if agent else ""
        depth_str = f"[d{depth}]" if depth else ""
        challenge_str = f" ← {challenge}" if challenge else ""

        click.echo(
            f"{layer}: {edge_type} {row['from_node']} → {row['to_node']} "
            f"{conf_str} {agent_str} {depth_str}{challenge_str}"
        )
    elif "impacted_node" in row:
        # Impact row
        click.echo(
            f"  {'  ' * (row.get('depth', 1) - 1)}→ {row['impacted_node']} "
            f"({row.get('edge_type', '?')}, {row.get('layer', '?')}, "
            f"conf: {row.get('confidence', '?')}, by: {row.get('created_by', '?')})"
        )
    elif "agent_name" in row and "current_focus" in row:
        # Agent state row
        focus = row.get("current_focus", "idle")
        patterns = row.get("active_patterns", [])
        last_sync = row.get("last_sync", "never")
        click.echo(f"  {row['agent_name']}: {focus} [patterns: {patterns}] (last sync: {last_sync})")
    elif "cnt" in row:
        # Count row
        click.echo(f"  {row.get('layer', row.get('type', 'total'))}: {row['cnt']}")
    else:
        # Generic row
        parts = [f"{k}={v}" for k, v in row.items() if v is not None]
        click.echo("  " + ", ".join(parts))


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """OHM — Shared awareness, individual judgment."""
    pass


# ── Graph commands ──────────────────────────────────────────

@cli.group()
def graph():
    """Knowledge graph operations."""
    pass


@graph.command("status")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_status(fmt):
    """Show graph status: node count, edge count, active agents."""
    with _get_store() as store:
        _format_output(store.status(), fmt)


@graph.command("schema")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_schema(fmt):
    """Show schema: node types, edge types, layers."""
    data = {
        "node_types": NODE_TYPES,
        "edge_types": EDGE_TYPES,
        "layers": LAYER_DESCRIPTIONS,
    }
    if fmt == "json":
        _format_output(data, fmt)
    else:
        click.echo("Node types:")
        for nt in NODE_TYPES:
            click.echo(f"  - {nt}")
        click.echo("\nEdge types by layer:")
        for layer, types in EDGE_TYPES.items():
            click.echo(f"  {layer}: {', '.join(types)}")
        click.echo("\nLayers:")
        for layer, desc in LAYER_DESCRIPTIONS.items():
            click.echo(f"  {layer} ({desc['name']}): {desc['question']}")
            click.echo(f"    Ownership: {desc['ownership']}")
            click.echo(f"    Confidence: {desc['confidence']}")
            click.echo(f"    Example: {desc['example']}")


@graph.command("layers")
def graph_layers():
    """Show L1-L4 layer descriptions."""
    for layer, desc in LAYER_DESCRIPTIONS.items():
        click.echo(f"\n{layer}: {desc['name']}")
        click.echo(f"  Question: {desc['question']}")
        click.echo(f"  Ownership: {desc['ownership']}")
        click.echo(f"  Confidence: {desc['confidence']}")
        click.echo(f"  Example: {desc['example']}")


@graph.command("write")
@click.option("--id", "node_id", help="Node ID (for creating nodes)")
@click.option("--label", help="Node label")
@click.option("--type", "node_type", help="Node type")
@click.option("--content", help="Node content")
@click.option("--from", "from_node", help="Source node ID (for edges)")
@click.option("--to", "to_node", help="Target node ID (for edges)")
@click.option("--edge-type", help="Edge type (CAUSES, DERIVES_FROM, etc.)")
@click.option("--layer", help="Layer (L1, L2, L3, L4)")
@click.option("--confidence", type=float, help="Confidence score (0-1)")
@click.option("--condition", help="Condition for edge to hold")
@click.option("--provenance", help="Provenance (conversation, research, etc.)")
@click.option("--tags", help="Comma-separated tags")
@click.option("--visibility", default="team", help="Visibility (private, team, public)")
@click.option("--agent", envvar="OHM_AGENT", default="ohm", help="Agent name for attribution")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_write(node_id, label, node_type, content, from_node, to_node, edge_type,
                layer, confidence, condition, provenance, tags, visibility, agent, fmt):
    """Write a node or edge to the graph. Attributed to the calling agent."""
    with _get_store(agent_name=agent) as store:
        if node_id and label:
            # Create/update node
            tag_list = [t.strip() for t in tags.split(",")] if tags else None
            result = store.write_node(
                id=node_id,
                label=label,
                type=node_type or "concept",
                content=content,
                confidence=confidence or 1.0,
                visibility=visibility,
                provenance=provenance,
                tags=tag_list,
            )
            _format_output(result, fmt)

        if from_node and to_node and edge_type:
            # Create edge
            result = store.write_edge(
                from_node=from_node,
                to_node=to_node,
                edge_type=edge_type,
                layer=layer or "L3",
                confidence=confidence,
                condition=condition,
                provenance=provenance,
            )
            _format_output(result, fmt)


@graph.command("neighborhood")
@click.argument("node_id")
@click.option("--depth", default=3, help="Traversal depth (1-5)")
@click.option("--layer", help="Filter by layer (L1, L2, L3, L4)")
@click.option("--edge-type", help="Filter by edge type")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_neighborhood(node_id, depth, layer, edge_type, agent, fmt):
    """Show all edges within N hops of a node."""
    with _get_store(agent_name=agent) as store:
        sql, params = build_neighborhood_query(node_id, depth, layer, edge_type)
        results = store.execute(sql, params)
        _format_output(results, fmt)


@graph.command("path")
@click.argument("from_node")
@click.argument("to_node")
@click.option("--max-depth", default=5, help="Maximum path length")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_path(from_node, to_node, max_depth, agent, fmt):
    """Find shortest path between two nodes."""
    with _get_store(agent_name=agent) as store:
        sql, params = build_path_query(from_node, to_node, max_depth)
        results = store.execute(sql, params)
        if not results:
            click.echo(f"No path found from {from_node} to {to_node}")
        else:
            _format_output(results, fmt)


@graph.command("impact")
@click.argument("node_id")
@click.option("--depth", default=5, help="Downstream depth (1-5)")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_impact(node_id, depth, agent, fmt):
    """Analyze downstream impact of a node (failure impact analysis)."""
    with _get_store(agent_name=agent) as store:
        sql, params = build_impact_query(node_id, depth)
        results = store.execute(sql, params)
        if not results:
            click.echo(f"No downstream impact from {node_id}")
        else:
            click.echo(f"Impact analysis for {node_id}:")
            _format_output(results, fmt)


@graph.command("confidence")
@click.argument("edge_id")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_confidence(edge_id, agent, fmt):
    """Audit confidence for an edge: challenges, supports, provenance."""
    with _get_store(agent_name=agent) as store:
        sql, params = build_confidence_audit_query(edge_id)
        results = store.execute(sql, params)
        if not results:
            click.echo(f"Edge {edge_id} not found")
        else:
            _format_output(results, fmt)


@graph.command("challenge")
@click.argument("edge_id")
@click.option("--reason", required=True, help="Reason for the challenge")
@click.option("--confidence", required=True, type=float, help="Your confidence (0-1)")
@click.option("--type", "challenge_type", default="CHALLENGED_BY",
              type=click.Choice(["CHALLENGED_BY", "CONTRADICTS", "REFINES"]),
              help="Challenge type")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_challenge(edge_id, reason, confidence, challenge_type, agent, fmt):
    """Challenge an existing edge. Creates a new edge, does not modify original."""
    with _get_store(agent_name=agent) as store:
        try:
            result = store.challenge_edge(edge_id, reason, confidence, challenge_type)
            if result:
                _format_output(result, fmt)
            else:
                click.echo(f"Edge {edge_id} not found", err=True)
                sys.exit(1)
        except PermissionError as e:
            click.echo(str(e), err=True)
            sys.exit(4)


@graph.command("support")
@click.argument("edge_id")
@click.option("--reason", required=True, help="Reason for supporting")
@click.option("--confidence", required=True, type=float, help="Your confidence (0-1)")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_support(edge_id, reason, confidence, agent, fmt):
    """Support an existing edge. Creates a SUPPORTS edge."""
    with _get_store(agent_name=agent) as store:
        result = store.challenge_edge(edge_id, reason, confidence, "SUPPORTS")
        if result:
            _format_output(result, fmt)
        else:
            click.echo(f"Edge {edge_id} not found", err=True)
            sys.exit(1)


@graph.command("observe")
@click.argument("node_id")
@click.option("--type", "obs_type", required=True, help="Observation type (anomaly, measurement, pattern, etc.)")
@click.option("--value", type=float, help="Observed value")
@click.option("--baseline", type=float, help="Expected baseline value")
@click.option("--sigma", type=float, help="Standard deviations from baseline")
@click.option("--source", help="Source of observation")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_observe(node_id, obs_type, value, baseline, sigma, source, agent, fmt):
    """Create an observation on a node."""
    with _get_store(agent_name=agent) as store:
        result = store.write_observation(
            node_id=node_id,
            type=obs_type,
            value=value,
            baseline=baseline,
            sigma=sigma,
            source=source,
        )
        _format_output(result, fmt)


@graph.command("listen")
@click.option("--since", help="ISO timestamp or 'last-check'")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def graph_listen(since, agent, fmt):
    """Change feed: what changed since last check."""
    with _get_store(agent_name=agent) as store:
        # Determine timestamp
        if since == "last-check" or since is None:
            # Check agent's last sync
            state = store.get_agent_state(agent)
            if state and state.get("last_sync"):
                ts = state["last_sync"]
            else:
                click.echo("No last-check timestamp found. Use --since with an ISO timestamp.")
                return
        else:
            ts = since

        sql, params = build_change_feed_query(ts, agent_name=agent)
        results = store.execute(sql, params)

        if not results:
            click.echo(f"No changes since {ts}")
        else:
            click.echo(f"\nChanges since {ts}:\n")
            for row in results:
                table = row["table_name"]
                op = row["operation"]
                row_agent = row["agent_name"]
                layer = row.get("layer", "?")
                click.echo(f"  [{layer}] {row_agent} {op} in {table}")

            click.echo(f"\n{len(results)} changes, from {len(set(r['agent_name'] for r in results))} agents")


# ── State commands ──────────────────────────────────────────

@cli.group()
def state():
    """Hive mind awareness: agent state and coordination."""
    pass


@state.command("set")
@click.argument("focus")
@click.option("--patterns", help="Comma-separated active patterns/topics")
@click.option("--services", help="Comma-separated available services")
@click.option("--session-id", help="Current session ID")
@click.option("--agent", envvar="OHM_AGENT", default="ohm")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def state_set(focus, patterns, services, session_id, agent, fmt):
    """Set your current focus in the hive mind."""
    with _get_store(agent_name=agent) as store:
        pattern_list = [p.strip() for p in patterns.split(",")] if patterns else None
        service_list = [s.strip() for s in services.split(",")] if services else None
        result = store.update_agent_state(
            current_focus=focus,
            active_patterns=pattern_list,
            available_services=service_list,
            session_id=session_id,
        )
        _format_output(result, fmt)


@state.command("show")
@click.argument("agent_name", required=False)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def state_show(agent_name, fmt):
    """Show agent state. Omit agent_name to see your own."""
    with _get_store() as store:
        if agent_name:
            result = store.get_agent_state(agent_name)
            if not result:
                click.echo(f"Agent {agent_name} not found")
                return
            _format_output(result, fmt)
        else:
            results = store.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name")
            if not results:
                click.echo("No agents registered")
            else:
                click.echo("Active agents:\n")
                _format_output(results, fmt)


@state.command("who-is-working-on")
@click.argument("topic")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def state_who(topic, fmt):
    """Find agents working on a topic."""
    with _get_store() as store:
        results = store.who_is_working_on(topic)
        if not results:
            click.echo(f"No agents working on '{topic}'")
        else:
            click.echo(f"Agents working on '{topic}':\n")
            _format_output(results, fmt)


# ── Serve commands ──────────────────────────────────────────

@cli.group()
def serve():
    """Daemon management (ohmd)."""
    pass


@serve.command("start")
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8710, type=int, help="Port")
@click.option("--db", default=None, help="Path to DuckDB file")
@click.option("--config", default=None, help="Path to config file")
def serve_start(host, port, db, config):
    """Start the ohmd daemon."""
    from .server import load_config, run_server

    cfg = load_config(config)
    if host != "127.0.0.1":
        cfg["host"] = host
    if port != 8710:
        cfg["port"] = port
    if db:
        cfg["db_path"] = db

    store = OhmStore(db_path=cfg["db_path"], agent_name="ohmd")
    click.echo(f"Starting OHM daemon on {cfg['host']}:{cfg['port']}")
    click.echo(f"Database: {cfg['db_path']}")

    try:
        run_server(cfg, store)
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
    finally:
        store.close()


@serve.command("status")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def serve_status(fmt):
    """Check if ohmd is running and show database status."""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request("http://127.0.0.1:8710/status")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            _format_output(data, fmt)
    except urllib.error.URLError:
        click.echo("ohmd is not running (no response on port 8710)")
    except Exception as e:
        click.echo(f"Error connecting to ohmd: {e}")


@serve.command("token")
@click.argument("agent_name")
@click.option("--config", default=None, help="Path to config file")
def serve_token(agent_name, config):
    """Generate an authentication token for an agent."""
    from .server import load_config

    cfg = load_config(config)
    import secrets
    token = secrets.token_urlsafe(32)
    cfg.setdefault("tokens", {})[agent_name] = token

    config_path = Path(os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json")))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)

    click.echo(f"Token for {agent_name}: {token}")
    click.echo(f"Config saved to {config_path}")
    click.echo(f"\nUsage: OHM_TOKEN={token} ohm graph status")


# ── Entry point ─────────────────────────────────────────────

def main():
    cli()


if __name__ == "__main__":
    main()