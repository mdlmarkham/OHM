"""OHM CLI — command tree and argument parsing.

Command structure (from docs/cli.md):
    ohm serve {start,stop,status,config}
    ohm graph {schema,layers,status,query,neighborhood,write,challenge,
               support,listen,confidence,impact,path,stats}
    ohm state {show,who-is-working-on,history}
    ohm topo {schema,failure-analysis,compliance-map,impact-study}
    ohm snapshot
    ohm diff
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
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
        "--tenant",
        default=None,
        help="Tenant ID for tenant-scoped operations (uses TenantManager)",
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
    serve_start.add_argument(
        "--quack",
        action="store_true",
        help="Enable Quack protocol for concurrent multi-writer access",
    )
    serve_start.add_argument(
        "--quack-uri",
        default=None,
        help="Quack server URI (default: quack:localhost)",
    )
    serve_start.add_argument(
        "--quack-token-env",
        default=None,
        help="Environment variable for Quack token (default: QUACK_TOKEN)",
    )

    serve_sub.add_parser("stop", help="Graceful shutdown")
    serve_sub.add_parser("status", help="Is ohmd running?")
    serve_config_parser = serve_sub.add_parser("config", help="Show current config")
    serve_config_parser.add_argument(
        "--config",
        default=None,
        help="Path to config file (default: ~/.ohm/ohmd.json)",
    )

    # ── graph ────────────────────────────────────────────────────────────
    graph_parser = subparsers.add_parser("graph", help="Graph operations")
    graph_sub = graph_parser.add_subparsers(dest="graph_command", help="Graph commands")

    # graph schema
    graph_sub.add_parser("schema", help="Show current node types, edge types, layers")

    # graph layers
    graph_sub.add_parser("layers", help="L0-L4 layer descriptions")

    # graph status
    graph_sub.add_parser("status", help="Node count, edge count, schema version, active agents")

    # graph upgrade
    upgrade_parser = graph_sub.add_parser("upgrade", help="Apply pending schema migrations")
    upgrade_parser.add_argument(
        "--dry-run",
        action="store_true",
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
        "--layer",
        choices=["L1", "L2", "L3", "L4"],
        help="Filter by layer",
    )
    neighborhood_parser.add_argument(
        "--direction",
        choices=["outgoing", "incoming", "both"],
        default="both",
    )
    neighborhood_parser.add_argument("--mermaid", action="store_true", help="Output as Mermaid diagram")

    # graph write
    write_parser = graph_sub.add_parser("write", help="Create nodes and edges with attribution")
    write_parser.add_argument("--from", dest="from_node", required=True, help="Source node ID")
    write_parser.add_argument("--to", dest="to_node", required=True, help="Target node ID")
    write_parser.add_argument("--type", dest="edge_type", required=True, help="Edge type")
    write_parser.add_argument(
        "--layer",
        choices=["L1", "L2", "L3", "L4"],
        default="L3",
        help="Layer",
    )
    write_parser.add_argument(
        "--confidence",
        type=float,
        default=0.7,
        help="Confidence score (0-1)",
    )
    write_parser.add_argument("--condition", help="Context condition string")
    write_parser.add_argument("--provenance", help="Source attribution")

    # graph challenge
    challenge_parser = graph_sub.add_parser("challenge", help="Challenge an existing edge")
    challenge_parser.add_argument("edge_id", help="ID of the edge to challenge")
    challenge_parser.add_argument("--reason", required=True, help="Challenge rationale")
    challenge_parser.add_argument(
        "--confidence",
        type=float,
        default=0.5,
        help="Challenge confidence",
    )

    # graph support
    support_parser = graph_sub.add_parser("support", help="Support an existing edge")
    support_parser.add_argument("edge_id", help="ID of the edge to support")
    support_parser.add_argument("--reason", required=True, help="Support rationale")
    support_parser.add_argument("--confidence", type=float, default=0.7, help="Support confidence")

    # graph confidence
    confidence_parser = graph_sub.add_parser("confidence", help="Provenance and challenge audit")
    confidence_parser.add_argument("edge_id", help="Edge ID to audit")

    # graph confidence-chain
    chain_parser = graph_sub.add_parser(
        "confidence-chain",
        help="Trace evidence chain and compute aggregate confidence",
    )
    chain_parser.add_argument("node_id", help="Node ID to trace evidence for")
    chain_parser.add_argument(
        "--max-depth",
        type=int,
        default=5,
        help="Maximum chain depth (default: 5)",
    )

    # graph listen
    listen_parser = graph_sub.add_parser("listen", help="Change feed since last check")
    listen_parser.add_argument("--since", help="ISO timestamp or 'last-check'")
    listen_parser.add_argument("--node-type", help="Filter changes by node type (e.g., concept, pattern)")

    # graph events (SSE client for real-time change feed)
    events_parser = graph_sub.add_parser("events", help="Stream change feed events via Server-Sent Events (SSE)")
    events_parser.add_argument("--since", help="ISO timestamp to stream from (default: last sync)")
    events_parser.add_argument("--topics", help="Comma-separated topic labels to filter")
    events_parser.add_argument("--agent", help="Filter to changes by this agent")
    events_parser.add_argument("--node-type", help="Filter to changes affecting nodes of this type (e.g., concept)")

    # graph impact
    impact_parser = graph_sub.add_parser("impact", help="Downstream failure impact analysis")
    impact_parser.add_argument("node_id", help="Node ID to analyze")
    impact_parser.add_argument("--depth", type=int, default=5, help="Max propagation depth")
    impact_parser.add_argument("--mermaid", action="store_true", help="Output as Mermaid diagram")

    # graph path
    path_parser = graph_sub.add_parser("path", help="Shortest path between two nodes")
    path_parser.add_argument("from_node", help="Starting node ID")
    path_parser.add_argument("to_node", help="Target node ID")
    path_parser.add_argument("--max-depth", type=int, default=10, help="Max path length")
    path_parser.add_argument("--mermaid", action="store_true", help="Output as Mermaid diagram")

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
        "--type",
        dest="obs_type",
        required=True,
        choices=["anomaly", "measurement", "pattern", "challenge", "support"],
        help="Observation type",
    )
    observe_parser.add_argument("--value", type=float, help="Observation value")
    observe_parser.add_argument("--baseline", type=float, help="Baseline value")
    observe_parser.add_argument("--sigma", type=float, help="Standard deviation")
    observe_parser.add_argument(
        "--source",
        choices=["signal", "research", "conversation", "analysis"],
        default="analysis",
        help="Observation source",
    )

    # graph aggregate
    aggregate_parser = graph_sub.add_parser("aggregate", help="Combine observations on a node")
    aggregate_parser.add_argument("node_id", help="Node ID to aggregate observations for")
    aggregate_parser.add_argument(
        "--method",
        choices=["weighted", "mean", "max_confidence", "consensus"],
        default="weighted",
        help="Aggregation strategy",
    )

    # graph pert-auto
    pert_auto_parser = graph_sub.add_parser(
        "pert-auto",
        help="Auto-derive PERT triple from observations or edge probabilities",
    )
    pert_auto_parser.add_argument(
        "node_id",
        help="Node ID to derive PERT for",
    )
    pert_auto_parser.add_argument(
        "--source",
        choices=["observations", "edges"],
        default="observations",
        help="Source for PERT derivation: observations (default) or edges",
    )
    pert_auto_parser.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format",
    )

    # graph anomalies
    anomalies_parser = graph_sub.add_parser("anomalies", help="Detect anomalous observations")
    anomalies_parser.add_argument(
        "--sigma",
        type=float,
        default=2.0,
        dest="sigma_threshold",
        help="Sigma threshold for flagging (default: 2.0)",
    )
    anomalies_parser.add_argument(
        "--layer",
        choices=["L1", "L2", "L3", "L4"],
        help="Filter by layer",
    )
    anomalies_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum results (default: 50)",
    )

    # granger causality
    granger_parser = graph_sub.add_parser("granger", help="Granger causality test between two nodes")
    granger_parser.add_argument("from_node", help="Source node ID (potential cause)")
    granger_parser.add_argument("to_node", help="Target node ID (potential effect)")
    granger_parser.add_argument(
        "--max-lag",
        type=int,
        default=3,
        help="Maximum lag order for VAR (default: 3)",
    )
    granger_parser.add_argument(
        "--min-observations",
        type=int,
        default=5,
        help="Minimum overlapping observations (default: 5)",
    )

    # edge stability
    edge_stability_parser = graph_sub.add_parser("edge-stability", help="Compute edge stability scores across time windows")
    edge_stability_parser.add_argument(
        "--edge-types",
        help="Comma-separated edge types (default: CAUSES,INFLUENCES,ENABLES,DEPENDS_ON)",
    )
    edge_stability_parser.add_argument(
        "--layer",
        choices=["L1", "L2", "L3", "L4"],
        help="Filter by layer",
    )
    edge_stability_parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Time window size in days (default: 7)",
    )
    edge_stability_parser.add_argument(
        "--min-windows",
        type=int,
        default=3,
        help="Minimum windows for stability (default: 3)",
    )

    # graph health
    graph_sub.add_parser("health", help="Graph structural health metrics")

    # graph cleanup
    cleanup_parser = graph_sub.add_parser("cleanup", help="Find and remove orphan agent nodes")
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show orphan agents without deleting",
    )

    # graph decay
    decay_parser = graph_sub.add_parser("decay", help="Apply confidence decay to stale edges")
    decay_parser.add_argument(
        "--threshold",
        type=float,
        default=0.1,
        help="Effective confidence below this is stale (default: 0.1)",
    )
    decay_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would decay without updating",
    )
    decay_parser.add_argument(
        "--layer",
        choices=["L1", "L2", "L3", "L4"],
        help="Only decay edges in this layer",
    )

    # graph composite-score
    score_parser = graph_sub.add_parser(
        "composite-score",
        help="Compute composite decision score for a node",
    )
    score_parser.add_argument("node_id", help="Node ID to score")
    score_parser.add_argument(
        "--obs-weight",
        type=float,
        default=0.5,
        help="Weight for observation signal (default: 0.5)",
    )
    score_parser.add_argument(
        "--evidence-weight",
        type=float,
        default=0.5,
        help="Weight for evidence signal (default: 0.5)",
    )
    score_parser.add_argument(
        "--method",
        choices=["arithmetic", "geometric"],
        default="arithmetic",
        help="Composition method: arithmetic (weighted mean) or geometric (multiplicative factors)",
    )
    score_parser.add_argument(
        "--baseline",
        type=float,
        default=1.0,
        help="Baseline for geometric mode (default: 1.0 = no change)",
    )

    # graph handoff
    handoff_parser = graph_sub.add_parser(
        "handoff",
        help="Transfer a ticket between agents",
    )
    handoff_parser.add_argument("--from-agent", required=True, help="Agent node ID transferring from")
    handoff_parser.add_argument("--to-agent", required=True, help="Agent node ID transferring to")
    handoff_parser.add_argument("--ticket", required=True, help="Ticket/case node ID")
    handoff_parser.add_argument("--reason", required=True, help="Reason for the handoff")
    handoff_parser.add_argument(
        "--type",
        choices=["TRANSFERRED_TO", "ESCALATED_TO", "DELEGATED_TO"],
        default="TRANSFERRED_TO",
        dest="edge_type",
        help="Handoff edge type (default: TRANSFERRED_TO)",
    )
    handoff_parser.add_argument(
        "--confidence",
        type=float,
        default=0.8,
        help="Confidence for the handoff edge (default: 0.8)",
    )

    # graph escalate
    escalate_parser = graph_sub.add_parser(
        "escalate",
        help="Escalate a ticket to a higher tier",
    )
    escalate_parser.add_argument("--ticket", required=True, help="Ticket/case node ID")
    escalate_parser.add_argument("--to-tier", required=True, help="Agent/tier node ID to escalate to")
    escalate_parser.add_argument("--reason", required=True, help="Reason for escalation")
    escalate_parser.add_argument("--from-agent", default=None, help="Agent node ID escalating from")
    escalate_parser.add_argument(
        "--confidence",
        type=float,
        default=0.9,
        help="Confidence for the escalation edge (default: 0.9)",
    )

    # graph ticket-provenance
    provenance_parser = graph_sub.add_parser(
        "ticket-provenance",
        help="Show handoff and state history for a ticket",
    )
    provenance_parser.add_argument("ticket_node", help="Ticket/case node ID")
    provenance_parser.add_argument(
        "--max-depth",
        type=int,
        default=10,
        help="Maximum traversal depth (default: 10)",
    )

    # graph record-outcome
    outcome_parser = graph_sub.add_parser(
        "record-outcome",
        help="Record whether a source's claim was correct",
    )
    outcome_parser.add_argument("--source", required=True, help="Source agent node ID")
    outcome_parser.add_argument("--claim", required=True, help="Claim node ID")
    outcome_parser.add_argument("--correct", action="store_true", help="Claim was correct")
    outcome_parser.add_argument("--incorrect", action="store_true", help="Claim was incorrect")
    outcome_parser.add_argument("--notes", default=None, help="Optional notes about the outcome")

    # graph source-reliability
    reliability_parser = graph_sub.add_parser(
        "source-reliability",
        help="Compute source reliability metrics",
    )
    reliability_parser.add_argument("source_agent", help="Source agent node ID")

    # graph trend
    trend_parser = graph_sub.add_parser(
        "trend",
        help="Detect temporal trends in observations",
    )
    trend_parser.add_argument("node_id", help="Node ID to analyze")
    trend_parser.add_argument(
        "--window",
        type=int,
        default=60,
        help="Lookback window in days (default: 60)",
    )
    trend_parser.add_argument(
        "--min-obs",
        type=int,
        default=3,
        help="Minimum observations needed (default: 3)",
    )

    # graph voi
    voi_parser = graph_sub.add_parser(
        "voi",
        help="Value of Information: rank nodes by research priority",
    )
    voi_parser.add_argument(
        "--decision",
        default=None,
        help="Comma-separated decision node IDs (auto-detects if omitted)",
    )
    voi_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Maximum results to return (default: 10)",
    )
    voi_parser.add_argument(
        "--layers",
        default=None,
        help="Comma-separated layer filter (e.g. L3,L4)",
    )
    voi_parser.add_argument(
        "--leak",
        type=float,
        default=0.15,
        help="Leak probability for Bayesian network (default: 0.15)",
    )
    voi_parser.add_argument(
        "--root-prior",
        type=float,
        default=0.3,
        help="Default prior for root nodes (default: 0.3)",
    )
    voi_parser.add_argument(
        "--edge-types",
        default=None,
        help="Comma-separated edge types to include (default: CAUSES,INFLUENCES,ENABLES,DEPENDS_ON)",
    )

    # graph voi-tasks
    voi_tasks_parser = graph_sub.add_parser(
        "voi-tasks",
        help="Generate research tasks from VoI rankings matched to agent expertise",
    )

    # graph policy
    policy_parser = graph_sub.add_parser("policy", help="Belief-state decision: observe vs. act")
    policy_parser.add_argument("target", help="Decision node ID to compute policy for")
    policy_parser.add_argument(
        "--observation-cost",
        type=float,
        default=None,
        help="Cost of one observation (auto-derived from utility if not set)",
    )
    policy_parser.add_argument(
        "--horizon",
        type=int,
        default=1,
        help="Decision horizon in steps (default: 1)",
    )
    policy_parser.add_argument(
        "--layers",
        default=None,
        help="Comma-separated layer filter",
    )
    policy_parser.add_argument(
        "--leak",
        type=float,
        default=0.15,
        help="Leak probability for Bayesian network (default: 0.15)",
    )
    voi_tasks_parser.add_argument(
        "--agent",
        default=None,
        help="Agent name to filter tasks by expertise match",
    )
    voi_tasks_parser.add_argument(
        "--decision",
        default=None,
        help="Comma-separated decision node IDs (auto-detects if omitted)",
    )
    voi_tasks_parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Maximum tasks to return (default: 5)",
    )
    voi_tasks_parser.add_argument(
        "--layers",
        default=None,
        help="Comma-separated layer filter (e.g. L3,L4)",
    )
    voi_tasks_parser.add_argument(
        "--leak",
        type=float,
        default=0.15,
        help="Leak probability for Bayesian network (default: 0.15)",
    )
    voi_tasks_parser.add_argument(
        "--root-prior",
        type=float,
        default=0.3,
        help="Default prior for root nodes (default: 0.3)",
    )

    # ── fragments ──────────────────────────────────────────────────────────
    fragments_parser = graph_sub.add_parser("fragments", help="List L0 thinking fragments")
    fragments_parser.add_argument("--agent", help="Filter by agent name")
    fragments_parser.add_argument("--since", help="ISO timestamp (created_at >= since)")
    fragments_parser.add_argument("--until", help="ISO timestamp (created_at <= until)")
    fragments_parser.add_argument("--q", help="Text search in label/content")
    fragments_parser.add_argument("--limit", type=int, default=50, help="Max results")

    # graph scratch
    scratch_parser = graph_sub.add_parser("scratch", help="Write a thinking fragment")
    scratch_parser.add_argument("content", nargs="+", help="Fragment text content")
    scratch_parser.add_argument("--tags", help="Comma-separated tags")
    scratch_parser.add_argument("--agent", default="cli", help="Agent name (default: cli)")
    scratch_parser.add_argument("--connects-to", help="Comma-separated node IDs to link to")

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

    # ── topo ─────────────────────────────────────────────────────────────
    sync_parser = subparsers.add_parser("sync", help="DuckLake sync operations (OHM-qiio)")
    sync_parser.add_argument("--health", action="store_true", help="Check DuckLake sync health")
    sync_parser.add_argument("--repair", action="store_true", help="Repair local DB from DuckLake mirror")
    sync_parser.add_argument("--alias", default="ohm_lake", help="DuckLake alias (default: ohm_lake)")

    topo_parser = subparsers.add_parser(
        "topo",
        help="Industrial knowledge graph commands (TOPO schema)",
    )
    topo_sub = topo_parser.add_subparsers(dest="topo_command", help="TOPO commands")

    # topo schema
    topo_sub.add_parser("schema", help="Show TOPO schema (industrial node/edge types)")

    # topo failure-analysis
    topo_fa = topo_sub.add_parser(
        "failure-analysis",
        help="Trace failure propagation from a node (industrial impact)",
    )
    topo_fa.add_argument("node_id", help="Starting node ID (equipment, system, etc.)")
    topo_fa.add_argument(
        "--depth",
        type=int,
        default=5,
        help="Max propagation depth (default: 5)",
    )
    topo_fa.add_argument(
        "--edge-type",
        dest="edge_types",
        action="append",
        help="Filter by edge type (repeatable, default: FEEDS,FLOWS_TO,DEPENDS_ON)",
    )

    # topo compliance-map
    topo_cm = topo_sub.add_parser(
        "compliance-map",
        help="Map compliance relationships around a node",
    )
    topo_cm.add_argument("node_id", help="Node ID to map compliance around")
    topo_cm.add_argument(
        "--depth",
        type=int,
        default=3,
        help="Neighborhood depth (default: 3)",
    )
    topo_cm.add_argument(
        "--direction",
        choices=["outgoing", "incoming", "both"],
        default="both",
        help="Edge direction (default: both)",
    )

    # topo impact-study
    topo_is = topo_sub.add_parser(
        "impact-study",
        help="Comprehensive impact study combining failure analysis and neighborhood",
    )
    topo_is.add_argument("node_id", help="Node ID to study")
    topo_is.add_argument(
        "--depth",
        type=int,
        default=5,
        help="Max traversal depth (default: 5)",
    )

    # ── hooks ────────────────────────────────────────────────────────────
    hooks_parser = subparsers.add_parser("hooks", help="Hook management (OHM-tjkx)")
    hooks_sub = hooks_parser.add_subparsers(dest="hooks_command", help="Hook commands")

    # hooks list
    hooks_list = hooks_sub.add_parser("list", help="List registered hooks")
    hooks_list.add_argument("--event", default=None, help="Filter by event (e.g., pre_fetch)")
    hooks_list.add_argument("--db", default=None, help="Database path (default: ~/.ohm/ohm.duckdb)")

    # hooks run
    hooks_run = hooks_sub.add_parser("run", help="Run hooks for a stage event")
    hooks_run.add_argument("event", help="Hook event to run (e.g., pre_fetch, post_commit)")
    hooks_run.add_argument("--payload", default=None, help="Path to JSON payload file (default: stdin)")
    hooks_run.add_argument("--db", default=None, help="Database path (default: ~/.ohm/ohm.duckdb)")

    # ── instances ──────────────────────────────────────────────────────
    # OHM-yzyk.5: instance registry and discovery
    instances_parser = subparsers.add_parser("instances", help="OHM instance discovery and registry (OHM-yzyk.5)")
    instances_sub = instances_parser.add_subparsers(dest="instances_command", help="Instance commands")

    # instances list
    inst_list = instances_sub.add_parser("list", help="List discovered OHM instances")
    inst_list.add_argument("--registry", default=None, help="Path to registry JSON (default: ~/.ohm/registry.json)")

    # instances discover
    inst_discover = instances_sub.add_parser("discover", help="Scan local config and probe for OHM instances")
    inst_discover.add_argument("--output", default=None, help="Write registry JSON to this path (default: ~/.ohm/registry.json)")
    inst_discover.add_argument("--timeout", type=float, default=3.0, help="Probe timeout in seconds (default: 3)")

    # instances health
    inst_health = instances_sub.add_parser("health", help="Check health of all discovered instances")
    inst_health.add_argument("--registry", default=None, help="Path to registry JSON")
    inst_health.add_argument("--timeout", type=float, default=5.0, help="Health check timeout in seconds")

    # instances show
    inst_show = instances_sub.add_parser("show", help="Show details of a specific instance")
    inst_show.add_argument("instance_id", help="Instance ID to show")
    inst_show.add_argument("--registry", default=None, help="Path to registry JSON")

    from ohm.cli import standup

    standup.build_parser(subparsers)

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
    elif args.command == "sync":
        _handle_sync(args)
    elif args.command == "topo":
        _handle_topo(args)
    elif args.command == "hooks":
        _handle_hooks(args)
    elif args.command == "instances":
        _handle_instances(args)
    elif args.command == "standup":
        from ohm.cli import standup
        standup.run(args)


def _handle_sync(args: argparse.Namespace) -> None:
    """Handle 'ohm sync' subcommands (OHM-qiio)."""
    from ohm.store import OhmStore

    db_path = args.db or os.environ.get("OHM_DB_PATH", "ohm.duckdb")
    store = OhmStore(db_path=db_path, agent_name=args.actor or "cli")

    try:
        if args.health:
            result = store.check_ducklake_health(alias=args.alias)
            if args.format == "json":
                import json

                print(json.dumps(result, indent=2, default=str))
            else:
                status = "✓ HEALTHY" if result.get("healthy") else "✗ DEGRADED"
                print(f"DuckLake Sync Health: {status}")
                if result.get("sync_degraded"):
                    print(f"  ⚠ Sync degraded: {result.get('errors', [])}")
                for table in (dlt.name for dlt in store.schema.ducklake_tables if dlt.name != "ohm_change_feed"):
                    lc = result.get("local_counts", {}).get(table, "?")
                    dc = result.get("ducklake_counts", {}).get(table, "?")
                    oc = result.get("orphan_counts", {}).get(table, "?")
                    print(f"  {table}: local={lc} ducklake={dc} orphans={oc}")
                if result.get("staleness_seconds") is not None:
                    print(f"  Staleness: {result['staleness_seconds']:.0f}s")
        elif args.repair:
            result = store.repair_from_ducklake(alias=args.alias)
            if args.format == "json":
                import json

                print(json.dumps(result, indent=2, default=str))
            else:
                print("DuckLake Repair Results:")
                print(f"  Inserted:     {result.get('inserted', 0)}")
                print(f"  Updated:      {result.get('updated', 0)}")
                print(f"  Soft-deleted: {result.get('soft_deleted', 0)}")
                print(f"  Verified:     {'✓' if result.get('verified') else '✗'}")
                if result.get("errors"):
                    for e in result["errors"]:
                        print(f"  ⚠ {e}")
        else:
            # Default: run sync_heartbeat
            result = store.sync_heartbeat()
            if args.format == "json":
                import json

                print(json.dumps(result, indent=2, default=str))
            else:
                print(f"Synced: pushed={result.get('pushed', 0)} pulled={result.get('pulled', 0)}")
                if result.get("last_sync"):
                    print(f"Last sync: {result['last_sync']}")
    finally:
        store.close()


def _get_pid_file() -> Path:
    """Return the PID file path for the ohmd daemon."""
    return Path(os.environ.get("OHM_STATE_DIR", str(Path.home() / ".ohm"))) / "ohmd.pid"


def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    import signal

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _handle_serve(args: argparse.Namespace) -> None:
    """Handle 'ohm serve' subcommands."""
    import sys

    cmd = args.serve_command
    pid_file = _get_pid_file()

    if cmd == "start":
        config_path = args.config or os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json"))

        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _is_process_running(pid):
                    print(f"ohmd already running (PID {pid})")
                    return
                else:
                    print(f"Stale PID file (process {pid} not running). Removing.")
                    pid_file.unlink()
            except (ValueError, OSError):
                pid_file.unlink()

        server_cmd = [
            sys.executable,
            "-m",
            "ohm.server",
            "--port",
            str(args.port),
            "--config",
            config_path,
        ]
        if getattr(args, "quack", False):
            server_cmd.append("--quack")
        if getattr(args, "quack_uri", None):
            server_cmd.extend(["--quack-uri", args.quack_uri])
        if getattr(args, "quack_token_env", None):
            server_cmd.extend(["--quack-token-env", args.quack_token_env])

        print(f"Starting ohmd on port {args.port}...")
        proc = subprocess.Popen(
            server_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid))
        print(f"ohmd started (PID {proc.pid})")

    elif cmd == "stop":
        if not pid_file.exists():
            print("ohmd not running (no PID file)")
            return

        try:
            pid = int(pid_file.read_text().strip())
            if not _is_process_running(pid):
                print("ohmd not running (stale PID file)")
                pid_file.unlink()
                return

            print(f"Stopping ohmd (PID {pid})...")
            try:
                on_windows = sys.platform == "win32"
                if on_windows:
                    os.kill(pid, signal.CTRL_BREAK_EVENT if hasattr(signal, "CTRL_BREAK_EVENT") else signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            except OSError as e:
                print(f"Failed to send signal: {e}")
                return

            for _ in range(10):
                if not _is_process_running(pid):
                    break
                time.sleep(0.2)

            if _is_process_running(pid):
                print("ohmd did not stop gracefully, forcing...")
                try:
                    if sys.platform == "win32":
                        os.kill(pid, signal.SIGTERM)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

            pid_file.unlink()
            print("ohmd stopped")

        except (ValueError, OSError) as e:
            print(f"Error stopping ohmd: {e}")
            if pid_file.exists():
                pid_file.unlink()

    elif cmd == "status":
        if not pid_file.exists():
            print("ohmd: not running (no PID file)")
            return

        try:
            pid = int(pid_file.read_text().strip())
            if _is_process_running(pid):
                print(f"ohmd: running (PID {pid})")
            else:
                print(f"ohmd: not running (stale PID file for process {pid})")
        except (ValueError, OSError):
            print("ohmd: not running (invalid PID file)")

    elif cmd == "config":
        config_path = args.config or os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json"))
        config_file = Path(config_path)
        if config_file.exists():
            import json

            with open(config_file) as f:
                config = json.load(f)
            print(f"Config: {config_path}")
            print(json.dumps(config, indent=2))
        else:
            print(f"Config file not found: {config_path}")

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

    config_path = args.config or os.environ.get("OHM_CONFIG", str(Path.home() / ".ohm" / "ohmd.json"))
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
    elif cmd == "confidence-chain":
        _handle_confidence_chain(args)
    elif cmd == "listen":
        _handle_listen(args)
    elif cmd == "events":
        _handle_events(args)
    elif cmd == "impact":
        _handle_impact(args)
    elif cmd == "path":
        _handle_path(args)
    elif cmd == "update":
        _handle_update(args)
    elif cmd == "observe":
        _handle_observe(args)
    elif cmd == "aggregate":
        _handle_aggregate(args)
    elif cmd == "pert-auto":
        _handle_pert_auto(args)
    elif cmd == "anomalies":
        _handle_anomalies(args)
    elif cmd == "granger":
        _handle_granger(args)
    elif cmd == "edge-stability":
        _handle_edge_stability(args)
    elif cmd == "health":
        _handle_health(args)
    elif cmd == "cleanup":
        _handle_cleanup(args)
    elif cmd == "decay":
        _handle_decay(args)
    elif cmd == "composite-score":
        _handle_composite_score(args)
    elif cmd == "handoff":
        _handle_handoff(args)
    elif cmd == "escalate":
        _handle_escalate(args)
    elif cmd == "ticket-provenance":
        _handle_ticket_provenance(args)
    elif cmd == "record-outcome":
        _handle_record_outcome(args)
    elif cmd == "source-reliability":
        _handle_source_reliability(args)
    elif cmd == "trend":
        _handle_trend(args)
    elif cmd == "voi":
        _handle_voi(args)
    elif cmd == "voi-tasks":
        _handle_voi_tasks(args)
    elif cmd == "policy":
        _handle_policy(args)
    elif cmd == "scratch":
        _handle_scratch(args)
    elif cmd == "fragments":
        _handle_fragments(args)
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


def _handle_agents(args: argparse.Namespace) -> None:
    """Handle 'ohm agents' command."""
    conn = _get_db(args)
    try:
        if args.agent_name:
            # Show details for a specific agent
            agent = conn.execute(
                "SELECT * FROM ohm_nodes WHERE type = 'agent' AND LOWER(label) = LOWER(?)",
                [args.agent_name],
            ).fetchone()
            if not agent:
                print(f"Agent not found: {args.agent_name}")
                return

            agent_id = agent[0]
            agent_label = agent[1]

            # Get values, goals, capabilities edges
            edges = conn.execute(
                """SELECT e.edge_type, n.label AS concept
                   FROM ohm_edges e
                   JOIN ohm_nodes n ON n.id = e.to_node
                   WHERE e.from_node = ? AND e.edge_type IN ('VALUES', 'GOALS', 'CAPABLE_OF')
                   ORDER BY e.edge_type""",
                [agent_id],
            ).fetchall()

            if args.format == "json":
                import json

                result = {"agent": agent_label, "id": agent_id, "edges": []}
                for row in edges:
                    result["edges"].append({"type": row[0], "concept": row[1]})
                print(json.dumps(result, indent=2))
            else:
                print(f"Agent: {agent_label} ({agent_id})")
                values = [r[1] for r in edges if r[0] == "VALUES"]
                goals = [r[1] for r in edges if r[0] == "GOALS"]
                capabilities = [r[1] for r in edges if r[0] == "CAPABLE_OF"]
                if values:
                    print(f"  Values: {', '.join(values)}")
                if goals:
                    print(f"  Goals:  {', '.join(goals)}")
                if capabilities:
                    print(f"  Capable of: {', '.join(capabilities)}")
        else:
            # List all registered agents
            agents = conn.execute("SELECT id, label FROM ohm_nodes WHERE type = 'agent' ORDER BY label").fetchall()

            if args.format == "json":
                import json

                agent_list: list[dict[str, str]] = []
                for row in agents:
                    agent_list.append({"id": row[0], "label": row[1]})
                print(json.dumps(agent_list, indent=2))
            else:
                if not agents:
                    print("No agents registered. Use graph.register_agent() in the SDK.")
                    return
                print(f"Registered agents ({len(agents)}):")
                for row in agents:
                    print(f"  {row[1]} ({row[0]})")
    finally:
        conn.close()


def _handle_snapshot(args: argparse.Namespace) -> None:
    """Handle 'ohm snapshot' command."""
    from ohm.queries import query_snapshot

    conn = _get_db(args)
    try:
        result = query_snapshot(
            conn,
            timestamp=args.timestamp,
            node_id=getattr(args, "node", None),
            edge_id=getattr(args, "edge", None),
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            s = result["summary"]
            print(f"Snapshot at {result['timestamp']}")
            print(f"  Nodes:         {s['nodes']}")
            print(f"  Edges:         {s['edges']}")
            print(f"  Observations:  {s['observations']}")
    finally:
        conn.close()


def _handle_diff(args: argparse.Namespace) -> None:
    """Handle 'ohm diff' command."""
    from ohm.queries import query_diff

    conn = _get_db(args)
    try:
        result = query_diff(
            conn,
            from_ts=args.from_ts,
            to_ts=args.to_ts,
            layer=getattr(args, "layer", None),
            agent_name=getattr(args, "agent", None),
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            s = result["summary"]
            print(f"Diff: {result['from']} → {result['to']}")
            print(f"  Nodes added:     {s['nodes_added']}")
            print(f"  Nodes updated:   {s['nodes_updated']}")
            print(f"  Edges added:     {s['edges_added']}")
            print(f"  Edges updated:   {s['edges_updated']}")
            print(f"  Observations:    {s['observations_added']}")
            print(f"  Total changes:   {s['total_changes']}")
    finally:
        conn.close()


# ── Graph Command Implementations ───────────────────────────────────────────


def _get_db(args: argparse.Namespace) -> "duckdb.DuckDBPyConnection":
    """Open a database connection using args.

    When --tenant is specified, uses TenantManager.get_store() for LRU cache,
    lazy migration, and meta.json awareness. When --db is specified, opens
    the database directly (--db takes precedence over --tenant).
    """
    from ohm.db import connect

    if args.db:
        return connect(args.db)

    tenant = getattr(args, "tenant", None)
    if tenant:
        from pathlib import Path

        from ohm.framework.validation import validate_customer_id
        from ohm.tenant import TenantManager

        customer_id = validate_customer_id(tenant)
        tenants_dir = os.environ.get("OHM_TENANTS_DIR", "")
        if not tenants_dir:
            db_default = os.environ.get("OHM_DB", "./ohm.db")
            tenants_dir = str(Path(db_default).parent / "tenants")
        tm = TenantManager(tenants_dir)
        try:
            store = tm.get_store(customer_id)
            return store.conn
        except Exception:
            tm.close()
            raise

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
                print("\nPending migrations:")
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
            # Build WHERE clause from hardcoded column names + parameterized values.
            # Column names (edge_type, layer, created_by, confidence) are not
            # user-provided — only values use ? placeholders.
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
                    print(f"  [{row['layer']}] {row['edge_type']}: {row['from_node']} → {row['to_node']} (conf: {row.get('confidence', '?')})")
    finally:
        conn.close()


def _handle_neighborhood(args: argparse.Namespace) -> None:
    """Handle bounded-depth graph traversal."""
    from ohm.queries import query_neighborhood

    conn = _get_db(args)
    try:
        results = query_neighborhood(
            conn,
            args.node_id,
            depth=args.depth,
            layer=args.layer,
            direction=args.direction,
        )
        if args.format == "json":
            import json

            print(json.dumps(results, indent=2, default=str))
        elif getattr(args, "mermaid", False):
            from ohm.visualization import to_mermaid

            print(to_mermaid(results, title=f"Neighborhood of {args.node_id}"))
        else:
            if not results:
                print(f"No edges found within {args.depth} hops of '{args.node_id}'")
                return
            for r in results:
                print(f"  [hop {r['hop']}] [{r['layer']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')}, by: {r['created_by']})")
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
                    print(f"    • {c['created_by']} (conf: {c['confidence']}): {c.get('condition', '')}")
            if result["supports"]:
                print(f"\n  Support ({len(result['supports'])}):")
                for s in result["supports"]:
                    print(f"    • {s['created_by']} (conf: {s['confidence']}): {s.get('condition', '')}")
    finally:
        conn.close()


def _handle_confidence_chain(args: argparse.Namespace) -> None:
    """Handle confidence chain — trace evidence and compute aggregate confidence."""
    from ohm.queries import query_confidence_chain

    conn = _get_db(args)
    try:
        result = query_confidence_chain(conn, args.node_id, max_depth=args.max_depth)
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"── Confidence Chain: {args.node_id} ──")
            print(f"  Aggregate confidence: {result['aggregate_confidence']}")
            print(f"  Evidence count:       {result['evidence_count']}")
            print(f"  Max depth:            {result['max_depth']}")
            if result["evidence_chain"]:
                print(f"\n  Evidence chain ({len(result['evidence_chain'])} edges):")
                for e in result["evidence_chain"]:
                    indent = "  " * e["depth"]
                    print(f"{indent}[d{e['depth']}] {e['edge_type']}: {e['from_node']} → {e['to_node']} (conf: {e['confidence']}, by: {e['created_by']})")
    finally:
        conn.close()


def _handle_listen(args: argparse.Namespace) -> None:
    """Handle change feed query."""
    from ohm.queries import query_change_feed

    conn = _get_db(args)
    try:
        node_type = getattr(args, "node_type", None)
        results = query_change_feed(conn, since=args.since, node_type=node_type)
        if args.format == "json":
            import json

            print(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                print("No changes found.")
                return
            print(f"Changes ({len(results)}):")
            for r in results:
                print(f"  [{r['occurred_at']}] {r['agent_name']} {r['operation']} {r['table_name']}.{r['row_id']}")
    finally:
        conn.close()


def _handle_events(args: argparse.Namespace) -> None:
    """Handle SSE event streaming — streams change feed via Server-Sent Events.

    This connects to the ohmd server's /events endpoint and streams changes
    in real-time using SSE (Server-Sent Events). Press Ctrl+C to stop.
    """
    import urllib.request
    import urllib.error

    # Build URL
    base_url = getattr(args, "url", "http://localhost:8710")
    token = getattr(args, "token", None) or os.environ.get("OHM_TOKEN", "")

    params = []
    if getattr(args, "since", None):
        params.append(f"since={args.since}")
    if getattr(args, "topics", None):
        params.append(f"topics={args.topics}")
    if getattr(args, "agent", None):
        params.append(f"agent={args.agent}")
    if getattr(args, "node_type", None):
        params.append(f"node_type={args.node_type}")

    url = f"{base_url}/events"
    if params:
        url += "?" + "&".join(params)

    headers = {"Accept": "text/event-stream"}
    if token:
        token_header = f"Bearer {token}"
        try:
            token_header.encode("latin-1")
        except UnicodeEncodeError:
            from urllib.parse import quote

            token_header = f"Bearer {quote(token, safe='-._~')}"
        headers["Authorization"] = token_header

    print(f"Connecting to {url}...")
    print("Press Ctrl+C to stop streaming.\n")

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            for line in resp:
                line = line.decode("utf-8").rstrip()
                if line.startswith("data: "):
                    import json

                    data = json.loads(line[6:])
                    print(f"  [{data.get('occurred_at', '?')}] {data.get('agent_name', '?')} {data.get('operation', '?')} {data.get('table_name', '?')}.{data.get('row_id', '?')}")
    except KeyboardInterrupt:
        print("\nStopped streaming.")
    except Exception as e:
        print(f"Error: {e}")


def _handle_path(args: argparse.Namespace) -> None:
    """Handle shortest path query."""
    from ohm.queries import query_path

    conn = _get_db(args)
    try:
        results = query_path(conn, args.from_node, args.to_node, max_depth=args.max_depth)
        if args.format == "json":
            import json

            print(json.dumps(results, indent=2, default=str))
        elif getattr(args, "mermaid", False):
            from ohm.visualization import to_mermaid_path

            print(to_mermaid_path(results, title=f"Path: {args.from_node} → {args.to_node}"))
        else:
            if not results:
                print(f"No path found from '{args.from_node}' to '{args.to_node}' (max depth: {args.max_depth})")
                return
            print(f"Path from '{args.from_node}' to '{args.to_node}':")
            for r in results:
                print(f"  [{r['layer']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")
    finally:
        conn.close()


def _handle_impact(args: argparse.Namespace) -> None:
    """Handle downstream failure impact analysis."""
    from ohm.queries import query_impact

    conn = _get_db(args)
    try:
        results = query_impact(conn, args.node_id, depth=args.depth)
        if args.format == "json":
            import json

            print(json.dumps(results, indent=2, default=str))
        elif getattr(args, "mermaid", False):
            from ohm.visualization import to_mermaid

            print(to_mermaid(results, title=f"Impact from {args.node_id}"))
        else:
            if not results:
                print(f"No downstream impact from '{args.node_id}' (max depth: {args.depth})")
                return
            print(f"Downstream impact from '{args.node_id}':")
            for r in results:
                print(f"  [depth {r['depth']}] [{r['layer']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")
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

        obs = create_observation(
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

            print(json.dumps({"observation_id": obs["id"], "status": "created"}))
        else:
            print(f"Recorded {args.obs_type} observation on {args.node_id}")
            print(f"  ID: {obs['id']}")
            if args.value is not None:
                print(f"  Value: {args.value}")
            if args.sigma is not None:
                print(f"  Sigma: {args.sigma}")
    finally:
        conn.close()


def _handle_aggregate(args: argparse.Namespace) -> None:
    """Handle observation aggregation."""
    from ohm.methods import aggregate_observations

    conn = _get_db(args)
    try:
        result = aggregate_observations(conn, args.node_id, method=args.method)
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            if result["observation_count"] == 0:
                print(f"No observations found for node {args.node_id}")
                return
            print(f"Aggregating {result['observation_count']} observations on {args.node_id} ({result['method_used']})")
            if result.get("disagreement"):
                print(f"  ⚠ No consensus — CV={result['coefficient_of_variation']}")
            else:
                print(f"  Combined value: {result['combined_value']}")
                print(f"  Combined confidence: {result['combined_confidence']}")
    finally:
        conn.close()


def _handle_pert_auto(args: argparse.Namespace) -> None:
    """Handle auto-pert from observations or edges."""
    import json

    from ohm.sdk import Graph

    conn = _get_db(args)
    try:
        graph = Graph(conn, actor=args.actor)
        if args.source == "observations":
            result = graph.auto_pert_from_observations(args.node_id)
        else:
            result = graph.auto_pert_from_edges(args.node_id)

        if args.format == "json":
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"── Auto-PERT for {args.node_id} ──")
            print(f"  Source:  {result['method']}")
            print(f"  n:       {result['n']}")
            if result["n"] > 0:
                print(f"  p05:     {result['p05']}")
                print(f"  p50:     {result['p50']}")
                print(f"  p95:     {result['p95']}")
                print(f"  mean:    {result['mean']}")
                print(f"  variance: {result['variance']}")
            else:
                print("  (no data)")
    finally:
        conn.close()


def _handle_anomalies(args: argparse.Namespace) -> None:
    """Handle anomaly detection."""
    from ohm.methods import detect_anomalies

    conn = _get_db(args)
    try:
        results = detect_anomalies(
            conn,
            sigma_threshold=args.sigma_threshold,
            layer=args.layer,
            limit=args.limit,
        )
        if args.format == "json":
            import json

            print(json.dumps(results, indent=2, default=str))
        else:
            if not results:
                print("No anomalies detected.")
                return
            print(f"Found {len(results)} anomalies:")
            for a in results:
                if a["anomaly_type"] == "observation":
                    print(f"  [{a['node_label']}] value={a['value']}, baseline={a['baseline']}, σ={a['sigma']}, distance={a['sigma_distance']}σ")
                elif a["anomaly_type"] == "high_variance":
                    print(f"  [{a['node_label']}] {a['observation_count']} obs, σ={a['stddev']}, mean={a['mean_value']}")
                elif a["anomaly_type"] == "low_confidence":
                    print(f"  Edge {a['edge_id']}: confidence={a['confidence']} ({a['layer']}/{a['edge_type']})")
    finally:
        conn.close()


def _handle_granger(args: argparse.Namespace) -> None:
    """Handle Granger causality test."""
    from ohm.methods import granger_causality

    conn = _get_db(args)
    try:
        result = granger_causality(
            conn,
            args.from_node,
            args.to_node,
            max_lag=args.max_lag,
            min_observations=args.min_observations,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            if result.get("error"):
                print(f"Granger test failed: {result['error']}")
                return
            from_label = result["from_node"]
            to_label = result["to_node"]
            gc = "YES" if result["granger_causes"] else "no"
            print(f"── Granger Causality: {from_label} → {to_label} ──")
            print(f"  F-statistic:  {result['f_statistic']}")
            print(f"  p-value:      {result['p_value']}")
            print(f"  Granger causes: {gc}")
            print(f"  Lag order:    {result['lag_order']}")
            print(f"  Observations: {result['n_observations']}")
    finally:
        conn.close()


def _handle_edge_stability(args: argparse.Namespace) -> None:
    """Handle edge stability analysis."""
    from ohm.methods import compute_edge_stability

    edge_types = args.edge_types.split(",") if args.edge_types else None
    conn = _get_db(args)
    try:
        result = compute_edge_stability(
            conn,
            edge_types=edge_types,
            layer=args.layer,
            window_days=args.window_days,
            min_windows=args.min_windows,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print("── Edge Stability Analysis ──")
            print(f"  Total edges: {result['n_edges']}")
            print(f"  Stable:      {result['n_stable']}")
            print(f"  Unstable:    {result['n_unstable']}")
            print(f"  Unknown:     {result['n_unknown']}")
            if result.get("edges"):
                print("\n  Top unstable edges:")
                for e in result["summary"]["most_unstable"]:
                    print(f"    {e['from_label']} → {e['to_label']} ({e['edge_type']}) stability={e['stability']} variance={e['variance']}")
    finally:
        conn.close()


def _handle_health(args: argparse.Namespace) -> None:
    """Handle graph health check."""
    from ohm.queries import query_graph_health

    conn = _get_db(args)
    try:
        result = query_graph_health(conn)
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Graph Health: {result['health_score']}/100")
            print(f"  Nodes: {result['total_nodes']} (orphans: {result['orphan_nodes']})")
            print(f"  Edges: {result['total_edges']} (unchallenged low-confidence: {result['unchallenged_low_confidence']})")
            print(f"  Dense clusters: {result['dense_cluster_nodes']}")
            print(f"  Stale observations: {result['stale_observations']}")
    finally:
        conn.close()


def _handle_cleanup(args: argparse.Namespace) -> None:
    """Handle orphan agent node cleanup (OHM-7pf)."""
    from ohm.queries import query_find_orphan_agents

    conn = _get_db(args)
    try:
        orphans = query_find_orphan_agents(conn)

        if args.format == "json":
            import json

            print(json.dumps({"orphan_agents": orphans, "count": len(orphans)}, indent=2, default=str))
        elif not orphans:
            print("No orphan agent nodes found.")
        elif args.dry_run:
            print(f"Found {len(orphans)} orphan agent node(s) (dry-run):")
            for o in orphans:
                print(f"  {o['id']} (label={o['label']}, created_by={o['created_by']})")
            print("Run without --dry-run to delete them.")
        else:
            print(f"Deleting {len(orphans)} orphan agent node(s)...")
            for o in orphans:
                # Delete associated edges first, then the node
                conn.execute("DELETE FROM ohm_edges WHERE from_node = ? OR to_node = ?", [o["id"], o["id"]])
                conn.execute("DELETE FROM ohm_nodes WHERE id = ?", [o["id"]])
                print(f"  Deleted {o['id']} (label={o['label']})")
            print("Done.")
    finally:
        conn.close()


def _handle_decay(args: argparse.Namespace) -> None:
    """Apply confidence decay to stale edges.

    Reads effective confidence using decay formula, then updates the stored
    confidence for edges whose effective_confidence < stale_threshold.

    L1/L2 edges are never decayed (permanent).
    L3 edges decay with 90-day half-life.
    L4 edges decay with 30-day half-life.
    """
    from ohm.queries import apply_confidence_decay

    conn = _get_db(args)
    try:
        result = apply_confidence_decay(
            conn,
            stale_threshold=args.threshold,
            layer=args.layer,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            if not result["decayed"]:
                print("No edges would be decayed.")
            else:
                print(f"Would decay {len(result['decayed'])} edges:")
                for e in result["decayed"]:
                    print(f"  {e['id']}: {e['confidence']} -> {e['new_confidence']} ({e['layer']}/{e['edge_type']})")
        else:
            print(f"Decayed {result['updated']} edges")
            if result.get("skipped"):
                print(f"Skipped {result['skipped']} L1/L2 edges")
    finally:
        conn.close()


def _handle_composite_score(args: argparse.Namespace) -> None:
    """Compute composite decision score combining observations and evidence."""
    from ohm.methods import composite_score

    conn = _get_db(args)
    try:
        result = composite_score(
            conn,
            args.node_id,
            observation_weight=args.obs_weight,
            evidence_weight=args.evidence_weight,
            method=args.method,
            baseline=args.baseline,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"── Composite Score: {args.node_id} ──")
            print(f"  Method:        {result['method']}")
            if result.get("baseline") and result["baseline"] != 1.0:
                print(f"  Baseline:      {result['baseline']}")
            print(f"  Composite:     {result['composite_score']}")
            print(f"  Observation:   {result['observation_score']} ({result['observation_count']} obs)")
            print(f"  Evidence:      {result['evidence_score']} ({result['evidence_count']} edges)")
            print(f"  Weights:       obs={result['weights']['observation']}, evidence={result['weights']['evidence']}")
    finally:
        conn.close()


def _handle_trend(args: argparse.Namespace) -> None:
    """Detect temporal trends in observations for a node."""
    from ohm.methods import detect_trend

    conn = _get_db(args)
    try:
        result = detect_trend(
            conn,
            args.node_id,
            window_days=args.window,
            min_observations=args.min_obs,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"── Trend: {args.node_id} ──")
            print(f"  Direction:  {result['trend']}")
            print(f"  Slope/day:  {result['slope_per_day']}")
            print(f"  R-squared:  {result['r_squared']}")
            print(f"  Obs count:  {result['observation_count']} (window: {result['window_days']}d)")
    finally:
        conn.close()


def _handle_voi(args: argparse.Namespace) -> None:
    """Value of Information: rank nodes by research priority."""
    from ohm.bayesian import compute_voi

    conn = _get_db(args)
    try:
        decision_nodes = None
        if args.decision:
            decision_nodes = [d.strip() for d in args.decision.split(",") if d.strip()]
        layers = None
        if args.layers:
            layers = [lyr.strip() for lyr in args.layers.split(",") if lyr.strip()]
        edge_types = None
        if args.edge_types:
            edge_types = [e.strip() for e in args.edge_types.split(",") if e.strip()]
        result = compute_voi(
            conn,
            decision_nodes=decision_nodes,
            edge_types=edge_types,
            layers=layers,
            top=args.top,
            leak_probability=args.leak,
            root_prior=args.root_prior,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print("── Value of Information ──")
            rankings = result.get("rankings", [])
            if not rankings:
                print("  No actionable VoI results found.")
            else:
                for i, entry in enumerate(rankings, 1):
                    node_id = entry.get("node_id", "?")
                    voi = entry.get("voi", 0)
                    uncertainty = entry.get("uncertainty", 0)
                    sensitivity = entry.get("sensitivity", 0)
                    print(f"  {i}. {node_id}: VoI={voi:.4f} (uncertainty={uncertainty:.4f}, sensitivity={sensitivity:.4f})")
            if result.get("decision_nodes"):
                print(f"  Decision nodes: {', '.join(result['decision_nodes'])}")
    finally:
        conn.close()


def _handle_voi_tasks(args: argparse.Namespace) -> None:
    """Generate research tasks from VoI rankings matched to agent expertise."""
    from ohm.bayesian import generate_voi_tasks

    conn = _get_db(args)
    try:
        decision_nodes = None
        if args.decision:
            decision_nodes = [d.strip() for d in args.decision.split(",") if d.strip()]
        layers = None
        if args.layers:
            layers = [lyr.strip() for lyr in args.layers.split(",") if lyr.strip()]
        result = generate_voi_tasks(
            conn,
            agent=args.agent,
            decision_nodes=decision_nodes,
            layers=layers,
            top=args.top,
            leak_probability=args.leak,
            root_prior=args.root_prior,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print("── VoI Research Tasks ──")
            tasks = result.get("tasks", [])
            if not tasks:
                print("  No research tasks found.")
                if result.get("message"):
                    print(f"  {result['message']}")
            else:
                for i, task in enumerate(tasks, 1):
                    node_id = task.get("node_id", "?")
                    label = task.get("label", node_id)
                    gap = task.get("gap_score", 0)
                    voi = task.get("voi_score", 0)
                    obs = task.get("observation_count", 0)
                    matched = task.get("matched_tags", [])
                    research = task.get("suggested_research", "")
                    print(f"  {i}. [{label}] gap={gap:.4f} voi={voi:.4f} obs={obs}")
                    if matched:
                        print(f"     Tags: {', '.join(matched)}")
                    print(f"     → {research}")
            if result.get("agent"):
                print(f"  Agent: {result['agent']}")
    finally:
        conn.close()


def _handle_policy(args: argparse.Namespace) -> None:
    """Belief-state decision: observe vs. act."""
    from ohm.methods import belief_state_decision

    conn = _get_db(args)
    try:
        layers = None
        if args.layers:
            layers = [lyr.strip() for lyr in args.layers.split(",") if lyr.strip()]
        result = belief_state_decision(
            conn,
            args.target,
            observation_cost=args.observation_cost,
            horizon=args.horizon,
            layers=layers,
            leak_probability=args.leak,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"── Policy: {result['target']} ──")
            print(f"  Action:     {result['action'].upper()}")
            print(f"  EVPI:       {result['evpi']}")
            print(f"  Obs cost:   {result['observation_cost']}")
            print(f"  Reason:     {result['reason']}")
            if result.get("top_target"):
                tt = result["top_target"]
                print(f"  Best obs:   {tt['label']} (VoI={tt['voi_score']})")
    finally:
        conn.close()


def _handle_handoff(args: argparse.Namespace) -> None:
    """Transfer a ticket between agents with full context tracking."""
    from ohm.sdk import connect as sdk_connect

    graph = sdk_connect(args.db, actor=_get_actor(args))
    try:
        result = graph.handoff(
            from_agent=args.from_agent,
            to_agent=args.to_agent,
            ticket_node=args.ticket,
            reason=args.reason,
            edge_type=args.edge_type,
            confidence=args.confidence,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            edge = result["edge"]
            print(f"── Handoff: {args.edge_type} ──")
            print(f"  Edge ID:     {edge['id']}")
            print(f"  From:        {args.from_agent}")
            print(f"  To:          {args.to_agent}")
            print(f"  Reason:      {args.reason}")
            print(f"  Confidence:  {args.confidence}")
            chain = result.get("handoff_chain", [])
            if chain:
                print(f"  Chain ({len(chain)} steps):")
                for step in chain:
                    print(f"    {step.get('edge_type', '?')}: {step.get('from_label', step.get('from_node', '?'))} → {step.get('to_label', step.get('to_node', '?'))}")
    finally:
        graph.close()


def _handle_escalate(args: argparse.Namespace) -> None:
    """Escalate a ticket to a higher tier with urgency."""
    from ohm.sdk import connect as sdk_connect

    graph = sdk_connect(args.db, actor=_get_actor(args))
    try:
        result = graph.escalate(
            ticket_node=args.ticket,
            to_tier=args.to_tier,
            reason=args.reason,
            from_agent=args.from_agent,
            confidence=args.confidence,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            edge = result["edge"]
            ticket = result.get("ticket", {})
            print("── Escalation ──")
            print(f"  Edge ID:     {edge['id']}")
            print(f"  Ticket:      {args.ticket}")
            if ticket:
                print(f"  Urgency:     {ticket.get('urgency', 'N/A')}")
                print(f"  Priority:    {ticket.get('priority', 'N/A')}")
            print(f"  To tier:     {args.to_tier}")
            print(f"  Reason:      {args.reason}")
    finally:
        graph.close()


def _handle_ticket_provenance(args: argparse.Namespace) -> None:
    """Show handoff and state history for a ticket."""
    from ohm.sdk import connect as sdk_connect

    graph = sdk_connect(args.db, actor=_get_actor(args))
    try:
        chain = graph.ticket_provenance(
            args.ticket_node,
            max_depth=args.max_depth,
        )
        if args.format == "json":
            import json

            print(json.dumps(chain, indent=2, default=str))
        else:
            print(f"── Ticket Provenance: {args.ticket_node} ──")
            if not chain:
                print("  (no handoff or state history found)")
            for step in chain:
                edge_type = step.get("edge_type", "?")
                from_label = step.get("from_label", step.get("from_node", "?"))
                to_label = step.get("to_label", step.get("to_node", "?"))
                reason = step.get("reason", "")
                ts = step.get("created_at", "")
                print(f"  [{ts}] {edge_type}: {from_label} → {to_label}" + (f" ({reason})" if reason else ""))
    finally:
        graph.close()


def _handle_record_outcome(args: argparse.Namespace) -> None:
    """Record whether a source's claim was correct or incorrect."""
    from ohm.sdk import connect as sdk_connect

    if args.correct and args.incorrect:
        print("Error: specify --correct or --incorrect, not both")
        return
    if not args.correct and not args.incorrect:
        print("Error: specify --correct or --incorrect")
        return

    outcome = args.correct

    graph = sdk_connect(args.db, actor=_get_actor(args))
    try:
        result = graph.record_outcome(
            source_agent=args.source,
            claim_node=args.claim,
            outcome=outcome,
        )
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            status = "correct" if outcome else "incorrect"
            print("── Outcome Recorded ──")
            print(f"  Source:     {result['source_agent']}")
            print(f"  Claim:      {result['claim_node']}")
            print(f"  Outcome:    {status}")
            print(f"  Recorded by: {result['recorded_by']}")
    finally:
        graph.close()


def _handle_source_reliability(args: argparse.Namespace) -> None:
    """Compute source reliability metrics from historical outcomes."""
    from ohm.sdk import connect as sdk_connect

    graph = sdk_connect(args.db, actor=_get_actor(args))
    try:
        result = graph.source_reliability(args.source_agent)
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"── Source Reliability: {args.source_agent} ──")
            print(f"  P(accurate):          {result['p_accurate']}")
            print(f"  False positive rate:  {result['false_positive_rate']}")
            print(f"  Total outcomes:       {result['total_outcomes']}")
            print(f"  Accurate:             {result['accurate_count']}")
            print(f"  False positives:      {result['false_positive_count']}")
            if result.get("low_confidence_warning"):
                print("  ⚠ Low confidence: fewer than 5 outcomes")
    finally:
        graph.close()


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
        matches = [r for r in results if r.get("current_focus") and topic.lower() in r["current_focus"].lower()]
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

    if args.format == "json":
        import json

        result = {
            "node_types": sorted(VALID_NODE_TYPES),
            "edge_types_by_layer": {layer: sorted(types) for layer, types in LAYER_EDGE_TYPES.items()},
        }
        print(json.dumps(result, indent=2))
    else:
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
    if args.format == "json":
        import json
        from ohm.schema import LAYER_EDGE_TYPES

        result = [
            {
                "name": "L1",
                "label": "L1: Structure",
                "sharing": "Fully shared",
                "ownership": "Communal",
                "edge_types": sorted(LAYER_EDGE_TYPES["L1"]),
                "example": '"Hungary has a constitution"',
            },
            {
                "name": "L2",
                "label": "L2: Flow",
                "sharing": "Shared + attributed",
                "ownership": "Proposing agent",
                "edge_types": sorted(LAYER_EDGE_TYPES["L2"]),
                "example": '"This idea derives from that source"',
            },
            {
                "name": "L3",
                "label": "L3: Knowledge",
                "sharing": "Agent-owned, challengeable",
                "ownership": "Creating agent",
                "edge_types": sorted(LAYER_EDGE_TYPES["L3"]),
                "example": '"Pattern X causes outcome Y conf: 0.94 (agent-alpha)"',
            },
            {
                "name": "L4",
                "label": "L4: Prospect",
                "sharing": "Agent-owned, visible",
                "ownership": "Forecasting agent",
                "edge_types": sorted(LAYER_EDGE_TYPES["L4"]),
                "example": '"Outcome Z expected conf: 0.65 (agent-beta)"',
            },
        ]
        print(json.dumps(result, indent=2))
    else:
        layers = [
            ("L1: Structure", "Fully shared", "Communal", "CONTAINS, BELONGS_TO, HAS_COMPONENT", '"Hungary has a constitution"'),
            ("L2: Flow", "Shared + attributed", "Proposing agent", "DERIVES_FROM, INFLUENCES, REFERENCES, USES", '"This idea derives from that source"'),
            ("L3: Knowledge", "Agent-owned, challengeable", "Creating agent", "CAUSES, CORRELATES_WITH, PREDICTS, EXPLAINS, CHALLENGED_BY, SUPPORTS", '"Pattern X causes outcome Y conf: 0.94 (agent-alpha)"'),
            ("L4: Prospect", "Agent-owned, visible", "Forecasting agent", "EXPECTS, PLANS, RISKS, DEPENDS_ON", '"Outcome Z expected conf: 0.65 (agent-beta)"'),
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


# ── TOPO Command Implementations ────────────────────────────────────────────

# Default edge types for failure analysis in industrial contexts
_TOPO_FAILURE_EDGE_TYPES = {"FEEDS", "FLOWS_TO", "DEPENDS_ON"}


def _handle_topo(args: argparse.Namespace) -> None:
    """Handle 'ohm topo' subcommands."""
    cmd = args.topo_command
    if cmd == "schema":
        _handle_topo_schema(args)
    elif cmd == "failure-analysis":
        _handle_topo_failure_analysis(args)
    elif cmd == "compliance-map":
        _handle_topo_compliance_map(args)
    elif cmd == "impact-study":
        _handle_topo_impact_study(args)
    else:
        print("Unknown topo command. Use 'ohm topo --help' for available commands.")


def _handle_topo_schema(args: argparse.Namespace) -> None:
    """Show the TOPO schema configuration."""
    from ohm.schema import TOPO_SCHEMA

    config = TOPO_SCHEMA
    if args.format == "json":
        import json

        print(json.dumps(config.to_dict(), indent=2))
    else:
        print(f"── TOPO Schema: {config.name} ──")
        print("\n── Node Types ──")
        for nt in sorted(config.node_types):
            print(f"  • {nt}")
        print("\n── Edge Types by Layer ──")
        for layer in sorted(config.layer_edge_types.keys()):
            types = ", ".join(sorted(config.layer_edge_types[layer]))
            desc = config.layer_descriptions.get(layer, "")
            print(f"  {layer}: {types}")
            print(f"      {desc}")
        print("\n── Observation Types ──")
        for ot in sorted(config.observation_types):
            print(f"  • {ot}")
        print("\n── Observation Sources ──")
        for os_ in sorted(config.observation_sources):
            print(f"  • {os_}")
        print("\n── Provenances ──")
        for p in sorted(config.provenances):
            print(f"  • {p}")


def _handle_topo_failure_analysis(args: argparse.Namespace) -> None:
    """Trace failure propagation from a node using industrial edge types.

    Uses query_impact to find downstream effects, filtered by
    industrial-relevant edge types (FEEDS, FLOWS_TO, DEPENDS_ON).
    """
    from ohm.queries import query_impact

    conn = _get_db(args)
    try:
        results = query_impact(conn, args.node_id, depth=args.depth)

        # Filter by edge types if specified, otherwise use TOPO defaults
        edge_types = set(args.edge_types) if args.edge_types else _TOPO_FAILURE_EDGE_TYPES
        filtered = [r for r in results if r.get("edge_type", "").upper() in edge_types]

        if args.format == "json":
            import json

            output = {
                "node_id": args.node_id,
                "depth": args.depth,
                "edge_types": sorted(edge_types),
                "total_impacts": len(results),
                "filtered_impacts": len(filtered),
                "impacts": filtered,
            }
            print(json.dumps(output, indent=2, default=str))
        else:
            if not filtered:
                if results:
                    print(f"No failure propagation found for '{args.node_id}' using edge types: {', '.join(sorted(edge_types))}")
                    print(f"  (Total downstream impacts: {len(results)}, but none matched the specified edge types)")
                else:
                    print(f"No downstream impact found for '{args.node_id}' (depth ≤ {args.depth})")
                return

            print(f"Failure analysis for '{args.node_id}' (depth ≤ {args.depth})")
            print(f"Edge types: {', '.join(sorted(edge_types))}")
            print(f"Impacts: {len(filtered)} of {len(results)} total downstream")
            print()
            for r in filtered:
                print(f"  [depth {r['depth']}] [{r['layer']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")
    finally:
        conn.close()


def _handle_topo_compliance_map(args: argparse.Namespace) -> None:
    """Map compliance relationships around a node.

    Uses query_neighborhood to find all edges within depth hops,
    highlighting compliance-relevant connections (BELONGS_TO, CONTAINS,
    DEPENDS_ON, RISKS, etc.).
    """
    from ohm.queries import query_neighborhood

    conn = _get_db(args)
    try:
        results = query_neighborhood(
            conn,
            args.node_id,
            depth=args.depth,
            direction=args.direction,
        )

        # Compliance-relevant edge types in industrial contexts
        compliance_types = {
            "BELONGS_TO",
            "CONTAINS",
            "HAS_COMPONENT",
            "PART_OF",
            "DEPENDS_ON",
            "RISKS",
            "THREATENS",
            "ENABLES",
            "REFERENCES",
            "NOTIFIES",
            "SERVES",
        }

        compliance_edges = [r for r in results if r.get("edge_type", "").upper() in compliance_types]
        other_edges = [r for r in results if r.get("edge_type", "").upper() not in compliance_types]

        if args.format == "json":
            import json

            output = {
                "node_id": args.node_id,
                "depth": args.depth,
                "direction": args.direction,
                "compliance_edges": compliance_edges,
                "other_edges": other_edges,
                "total_edges": len(results),
            }
            print(json.dumps(output, indent=2, default=str))
        else:
            if not results:
                print(f"No edges found within {args.depth} hops of '{args.node_id}'")
                return

            print(f"Compliance map for '{args.node_id}' (depth ≤ {args.depth}, {args.direction})")
            print(f"Total edges: {len(results)} ({len(compliance_edges)} compliance-relevant)")
            print()

            if compliance_edges:
                print("── Compliance-relevant ──")
                for r in compliance_edges:
                    print(f"  [hop {r['hop']}] [{r['layer']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')}, by: {r['created_by']})")
                print()

            if other_edges:
                print("── Other connections ──")
                for r in other_edges:
                    print(f"  [hop {r['hop']}] [{r['layer']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")
    finally:
        conn.close()


def _handle_topo_impact_study(args: argparse.Namespace) -> None:
    """Comprehensive impact study combining failure analysis and neighborhood.

    Runs both query_impact (downstream failure propagation) and
    query_neighborhood (local context) to produce a combined report.
    """
    from ohm.queries import query_impact, query_neighborhood

    conn = _get_db(args)
    try:
        impact_results = query_impact(conn, args.node_id, depth=args.depth)
        neighborhood_results = query_neighborhood(
            conn,
            args.node_id,
            depth=args.depth,
            direction="both",
        )

        # Categorize impact by layer
        impact_by_layer: dict[str, list] = {}
        for r in impact_results:
            layer = r.get("layer", "unknown")
            impact_by_layer.setdefault(layer, []).append(r)

        # Categorize neighborhood by direction
        incoming = [r for r in neighborhood_results if r.get("from_node") != args.node_id]
        outgoing = [r for r in neighborhood_results if r.get("to_node") != args.node_id]

        if args.format == "json":
            import json

            output = {
                "node_id": args.node_id,
                "depth": args.depth,
                "impact": {
                    "total": len(impact_results),
                    "by_layer": {k: len(v) for k, v in impact_by_layer.items()},
                    "edges": impact_results,
                },
                "neighborhood": {
                    "total": len(neighborhood_results),
                    "incoming": len(incoming),
                    "outgoing": len(outgoing),
                    "edges": neighborhood_results,
                },
            }
            print(json.dumps(output, indent=2, default=str))
        else:
            print(f"── Impact Study: {args.node_id} ──")
            print(f"Depth: {args.depth}")
            print()

            # Impact section
            print(f"── Downstream Impact ({len(impact_results)} edges) ──")
            if not impact_results:
                print("  No downstream impact found.")
            else:
                for layer, edges in sorted(impact_by_layer.items()):
                    print(f"\n  {layer} ({len(edges)} edges):")
                    for r in edges:
                        print(f"    [depth {r['depth']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")

            # Neighborhood section
            print(f"\n── Local Context ({len(neighborhood_results)} edges) ──")
            if not neighborhood_results:
                print("  No local connections found.")
            else:
                print(f"  Incoming: {len(incoming)}")
                print(f"  Outgoing: {len(outgoing)}")
                for r in neighborhood_results:
                    print(f"  [hop {r['hop']}] [{r['layer']}] {r['edge_type']}: {r['from_node']} → {r['to_node']} (conf: {r.get('confidence', '?')})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()


def topo_main(argv: list[str] | None = None) -> NoReturn:
    """Entry point for the TOPO CLI.

    Behaves like ``main()`` but defaults the command to ``topo`` so that
    ``topo failure-analysis <node>`` works as a shorthand for
    ``ohm topo failure-analysis <node>``.

    Global flags (--format, --actor, --db) must come before the topo
    subcommand, just like with ``ohm``.
    """
    if argv is None:
        argv = sys.argv[1:]
    # Find where the topo subcommand starts. Global flags like --db,
    # --format, --actor, --version take arguments, so we need to skip
    # past them before inserting 'topo'.
    global_flags_with_args = {"--db", "--actor", "--format"}
    i = 0
    insert_at = 0
    while i < len(argv):
        if argv[i] in global_flags_with_args:
            i += 2  # skip flag + its value
            insert_at = i
        elif argv[i] == "--version":
            i += 1
            insert_at = i
        else:
            break
    # Insert 'topo' before the subcommand
    main(argv[:insert_at] + ["topo"] + argv[insert_at:])


def _handle_scratch(args: argparse.Namespace) -> None:
    """Handle 'ohm scratch' — write a thinking fragment (OHM-a5rz.16)."""
    conn = _get_db(args)
    try:
        content = " ".join(args.content)
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
        connects_to = [c.strip() for c in args.connects_to.split(",") if c.strip()] if args.connects_to else None
        from ohm.graph.queries import scratch

        result = scratch(conn, content=content, created_by=args.agent, tags=tags, connects_to=connects_to)
        print(f"Created fragment: {result.get('id', '?')}")
        if args.format == "json":
            import json

            print(json.dumps(result, indent=2, default=str))
    finally:
        conn.close()


def _handle_fragments(args: argparse.Namespace) -> None:
    """Handle 'ohm fragments' — list L0 fragments (OHM-a5rz.16)."""
    conn = _get_db(args)
    try:
        import json

        conditions = ["type = 'fragment'", "deleted_at IS NULL"]
        params: list = []
        if args.agent:
            conditions.append("created_by = ?")
            params.append(args.agent)
        if args.since:
            conditions.append("created_at >= ?::TIMESTAMP")
            params.append(args.since)
        if args.until:
            conditions.append("created_at <= ?::TIMESTAMP")
            params.append(args.until)
        if args.q:
            conditions.append("(label ILIKE ? OR content ILIKE ?)")
            params.append(f"%{args.q}%")
            params.append(f"%{args.q}%")
        params.append(args.limit)
        where = " AND ".join(conditions)
        nodes = conn.execute(
            f"SELECT id, label, created_by, created_at FROM ohm_nodes WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        if args.format == "json":
            cols = ["id", "label", "created_by", "created_at"]
            rows = [dict(zip(cols, row)) for row in nodes]
            print(json.dumps({"fragments": rows}, indent=2, default=str))
        else:
            print(f"Fragments ({len(nodes)}):")
            for row in nodes:
                fid, label, creator, created = row
                created_str = str(created)[:19] if created else "?"
                print(f"  [{created_str}] {label} ({fid}) by {creator}")
    finally:
        conn.close()


def _handle_hooks(args: argparse.Namespace) -> None:
    """Handle 'ohm hooks' subcommands (OHM-tjkx)."""
    import duckdb as _duckdb
    import json as _json
    import sys as _sys

    db_path = args.db or os.environ.get("OHM_DB_PATH", str(Path.home() / ".ohm" / "ohm.duckdb"))

    if args.hooks_command == "list":
        conn = _duckdb.connect(db_path)
        try:
            from ohm.hooks import VALID_HOOK_EVENTS
            from ohm.schema import initialize_schema

            initialize_schema(conn)

            event_filter = args.event
            sql = "SELECT id, event, command, timeout_ms, enabled, created_by FROM ohm_hooks"
            params = []
            if event_filter:
                sql += " WHERE event = ?"
                params.append(event_filter)
            sql += " ORDER BY event, id"
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                print("No hooks registered.")
                print(f"Valid events: {', '.join(sorted(VALID_HOOK_EVENTS))}")
                return
            print(f"Hooks ({len(rows)}):")
            for row in rows:
                hid, event, command, timeout, enabled, creator = row
                status = "enabled" if enabled else "disabled"
                print(f"  [{event}] {hid} ({status}, {timeout}ms) by {creator}")
                print(f"    command: {command[:80]}")
            print(f"\nValid events: {', '.join(sorted(VALID_HOOK_EVENTS))}")
        finally:
            conn.close()

    elif args.hooks_command == "run":
        conn = _duckdb.connect(db_path)
        try:
            from ohm.hooks import HookRunner, VALID_HOOK_EVENTS
            from ohm.schema import initialize_schema

            initialize_schema(conn)

            event = args.event
            if event not in VALID_HOOK_EVENTS:
                print(f"Error: invalid event '{event}'. Valid: {', '.join(sorted(VALID_HOOK_EVENTS))}")
                _sys.exit(1)

            # Load payload from file or stdin
            if args.payload:
                with open(args.payload, "r") as f:
                    payload = _json.load(f)
            else:
                payload_text = _sys.stdin.read()
                payload = _json.loads(payload_text) if payload_text.strip() else {}

            runner = HookRunner(conn)
            results = runner.run_hooks(event, payload)
            print(
                _json.dumps(
                    {
                        "event": event,
                        "hooks_run": len(results),
                        "results": [
                            {
                                "hook_id": r.hook_id,
                                "exit_code": r.exit_code,
                                "success": r.success,
                                "stdout": r.stdout[:500],
                                "stderr": r.stderr[:500],
                                "duration_ms": round(r.duration_ms, 2),
                                "timed_out": r.timed_out,
                            }
                            for r in results
                        ],
                    },
                    indent=2,
                )
            )
        finally:
            conn.close()
    else:
        print("Usage: ohm hooks [list|run] ...")
        _sys.exit(1)


def _registry_path(args) -> str:
    """Resolve the registry JSON path."""
    return args.registry or str(Path.home() / ".ohm" / "registry.json")


def _discover_instances(timeout: float = 3.0) -> list[dict]:
    """Scan local config locations and probe for OHM instances (OHM-yzyk.5).

    Checks well-known locations for OHM endpoints, probes each with
    GET /instance, and returns a list of instance metadata dicts.
    """
    import json as _json
    import urllib.request

    candidates: list[str] = []

    # Default ohmd port
    candidates.append("http://127.0.0.1:8710")

    # From environment
    env_url = os.environ.get("OHM_URL")
    if env_url and env_url not in candidates:
        candidates.append(env_url)

    # From per-agent config dirs
    ohm_dir = Path.home() / ".ohm"
    if ohm_dir.exists():
        for agent_dir in ohm_dir.iterdir():
            cfg = agent_dir / "ohm.json"
            if cfg.exists():
                try:
                    data = _json.loads(cfg.read_text())
                    url = data.get("ohm_url") or data.get("url")
                    if url and url not in candidates:
                        candidates.append(url)
                except Exception:
                    pass

    # From /etc/ohm/ config files
    etc_ohm = Path("/etc/ohm")
    if etc_ohm.exists():
        for cfg_file in etc_ohm.glob("ohmd*.json"):
            try:
                data = _json.loads(cfg_file.read_text())
                host = data.get("host", "127.0.0.1")
                port = data.get("port", 8710)
                url = f"http://{host}:{port}"
                if url not in candidates:
                    candidates.append(url)
            except Exception:
                pass

    # Probe each candidate
    instances: list[dict] = []
    for url in candidates:
        try:
            req = urllib.request.Request(
                f"{url}/instance",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _json.loads(resp.read())
                data["discovered_url"] = url
                data["health"] = "ok"
                instances.append(data)
        except Exception as e:
            instances.append({
                "discovered_url": url,
                "health": "unreachable",
                "error": str(e)[:200],
            })

    return instances


def _handle_instances(args: argparse.Namespace) -> None:
    """Handle 'ohm instances' subcommands (OHM-yzyk.5)."""
    import json as _json
    import sys as _sys

    if args.instances_command == "discover":
        timeout = getattr(args, "timeout", 3.0)
        instances = _discover_instances(timeout=timeout)
        output_path = args.output or str(Path.home() / ".ohm" / "registry.json")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        registry = {
            "version": "1",
            "discovered_at": _json.dumps(None),
            "instances": instances,
        }
        from datetime import datetime, timezone

        registry["discovered_at"] = datetime.now(timezone.utc).isoformat()
        Path(output_path).write_text(_json.dumps(registry, indent=2))
        print(f"Discovered {len(instances)} instance(s). Registry: {output_path}")
        for inst in instances:
            status = inst.get("health", "unknown")
            url = inst.get("discovered_url", "?")
            iid = inst.get("instance_id", "?")
            purpose = inst.get("purpose", "")
            print(f"  {iid:30s} {url:35s} {status:12s} {purpose}")

    elif args.instances_command == "list":
        reg_path = _registry_path(args)
        try:
            registry = _json.loads(Path(reg_path).read_text())
            instances = registry.get("instances", [])
        except FileNotFoundError:
            print(f"No registry found at {reg_path}. Run 'ohm instances discover' first.")
            _sys.exit(1)
        if not instances:
            print("Registry is empty.")
            return
        print(f"{'Instance ID':30s} {'URL':35s} {'Health':12s} {'Purpose'}")
        print("-" * 90)
        for inst in instances:
            iid = inst.get("instance_id", "?")
            url = inst.get("discovered_url", inst.get("listen_url", "?"))
            health = inst.get("health", "unknown")
            purpose = inst.get("purpose", "")
            print(f"{iid:30s} {url:35s} {health:12s} {purpose}")

    elif args.instances_command == "health":
        reg_path = _registry_path(args)
        timeout = getattr(args, "timeout", 5.0)
        try:
            registry = _json.loads(Path(reg_path).read_text())
            instances = registry.get("instances", [])
        except FileNotFoundError:
            print(f"No registry found at {reg_path}. Run 'ohm instances discover' first.")
            _sys.exit(1)
        # Re-probe each
        import urllib.request

        for inst in instances:
            url = inst.get("discovered_url", inst.get("listen_url"))
            if not url:
                continue
            try:
                req = urllib.request.Request(f"{url}/instance", headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = _json.loads(resp.read())
                    inst["health"] = "ok"
                    inst.update(data)
                    print(f"  {inst.get('instance_id', '?'):30s} {url:35s} ok")
            except Exception as e:
                inst["health"] = "unreachable"
                print(f"  {inst.get('instance_id', '?'):30s} {url:35s} UNREACHABLE ({str(e)[:80]})")

    elif args.instances_command == "show":
        reg_path = _registry_path(args)
        try:
            registry = _json.loads(Path(reg_path).read_text())
            instances = registry.get("instances", [])
        except FileNotFoundError:
            print(f"No registry found at {reg_path}. Run 'ohm instances discover' first.")
            _sys.exit(1)
        for inst in instances:
            if inst.get("instance_id") == args.instance_id:
                print(_json.dumps(inst, indent=2))
                return
        print(f"Instance '{args.instance_id}' not found in registry.")

    else:
        print("Usage: ohm instances [list|discover|health|show] ...")
        _sys.exit(1)
