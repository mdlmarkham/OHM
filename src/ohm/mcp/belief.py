"""Belief statement parsing and comparison (OHM-766).

Parses free-text belief statements like 'P(target=bad) = 0.3' and
compares them to the graph's posterior, producing a divergence score
and a calibration log entry.
"""

from __future__ import annotations

import re
from typing import Any


def parse_belief_statement(statement: str) -> dict[str, Any] | None:
    """Parse a belief statement into structured components.

    Supports formats:
    - 'P(target=bad) = 0.3'
    - 'P(target=bad) ≈ 0.3'
    - 'I believe P(node-id=bad) is about 0.3'
    - '0.3' (just a probability, no target)

    Returns None if no probability can be extracted.
    """
    if not statement:
        return None

    # Try to extract target and state from P(target=state) pattern
    target_match = re.search(
        r"P?\s*\(?\s*([a-zA-Z0-9_-]+)\s*=\s*([a-zA-Z0-9_-]+)\s*\)?",
        statement,
        re.IGNORECASE,
    )
    target = target_match.group(1) if target_match else None
    state = target_match.group(2).lower() if target_match else "bad"

    # Try to extract probability value
    prob_match = re.search(r"(?:=|≈|is\s+about|~)\s*([0-9]+\.?[0-9]*)", statement)
    if not prob_match:
        # Try bare number
        prob_match = re.search(r"\b([01]\.?[0-9]*)\b", statement)

    if not prob_match:
        return None

    try:
        probability = float(prob_match.group(1))
    except ValueError:
        return None

    if not 0.0 <= probability <= 1.0:
        return None

    return {
        "target": target,
        "state": state,
        "claimed_probability": probability,
        "raw": statement,
    }


def compare_belief_to_posterior(
    claimed: float,
    graph_posterior: dict[str, float],
    state: str = "bad",
) -> dict[str, Any]:
    """Compare a claimed probability to the graph's posterior.

    Returns divergence, severity level (0-3), and whether to nudge.

    Severity levels:
    - 0: silent (|diff| < 0.15)
    - 1: context signal (0.15 <= |diff| < 0.25)
    - 2: soft nudge (0.25 <= |diff| < 0.35)
    - 3: firm flag (|diff| >= 0.35)
    """
    graph_p = graph_posterior.get(f"P({state})", graph_posterior.get(state, 0.0))
    diff = abs(claimed - graph_p)

    if diff < 0.15:
        severity = 0
    elif diff < 0.25:
        severity = 1
    elif diff < 0.35:
        severity = 2
    else:
        severity = 3

    return {
        "claimed_probability": claimed,
        "graph_probability": graph_p,
        "divergence": round(diff, 4),
        "severity": severity,
        "agree": diff < 0.15,
        "direction": "overconfident" if claimed > graph_p else "underconfident",
    }


def build_belief_log_entry(
    agent_name: str,
    target: str | None,
    claimed: float,
    graph_p: float,
    tool_name: str,
    edge_or_node_id: str | None = None,
) -> dict[str, Any]:
    """Build a calibration log entry for later scoring (OHM-768)."""
    import time

    return {
        "agent_name": agent_name,
        "target_node": target,
        "claimed_probability": claimed,
        "graph_posterior": graph_p,
        "divergence": round(abs(claimed - graph_p), 4),
        "tool": tool_name,
        "edge_or_node_id": edge_or_node_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actual_state": None,  # filled when target resolves
    }
