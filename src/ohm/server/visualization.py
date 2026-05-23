"""OHM Graph Visualization — Mermaid diagram export.

Converts graph query results to Mermaid.js markup for rendering
in markdown files, notebooks, and documentation.

Usage:
    from ohm.visualization import to_mermaid

    edges = graph.neighborhood("node-id", depth=2)
    print(to_mermaid(edges))
"""

from __future__ import annotations

from typing import Any


def to_mermaid(
    edges: list[dict[str, Any]],
    *,
    title: str | None = None,
    direction: str = "LR",
) -> str:
    """Convert a list of edge dicts to a Mermaid flowchart.

    Args:
        edges: List of edge records with from_node, to_node, edge_type, layer.
        title: Optional diagram title.
        direction: Flow direction: 'LR' (left-right), 'TD' (top-down),
                   'RL', 'BT'.

    Returns:
        Mermaid.js flowchart markup string.
    """
    lines = ["```mermaid", f"flowchart {direction}"]
    if title:
        lines.append(f"    title[{title}]")

    seen_nodes: set[str] = set()
    for edge in edges:
        from_n = _sanitize(edge.get("from_node", "?"))
        to_n = _sanitize(edge.get("to_node", "?"))
        etype = edge.get("edge_type", "?")
        layer = edge.get("layer", "")
        conf = edge.get("confidence")

        # Register nodes
        if from_n not in seen_nodes:
            lines.append(f"    {from_n}[{from_n}]")
            seen_nodes.add(from_n)
        if to_n not in seen_nodes:
            lines.append(f"    {to_n}[{to_n}]")
            seen_nodes.add(to_n)

        # Edge with label
        label_parts = [etype]
        if conf is not None:
            label_parts.append(f"c:{conf:.2f}")
        label = " ".join(label_parts)
        style = _edge_style(layer, etype)
        lines.append(f"    {from_n} -->|{label}| {to_n}{style}")

    lines.append("```")
    return "\n".join(lines)


def to_mermaid_path(
    edges: list[dict[str, Any]],
    *,
    title: str | None = None,
) -> str:
    """Convert a path result to a Mermaid flowchart highlighting the path.

    Args:
        edges: Ordered list of edges forming a path.
        title: Optional diagram title.

    Returns:
        Mermaid.js flowchart markup string.
    """
    lines = ["```mermaid", "flowchart LR"]
    if title:
        lines.append(f"    title[{title}]")

    seen_nodes: set[str] = set()
    for i, edge in enumerate(edges):
        from_n = _sanitize(edge.get("from_node", "?"))
        to_n = _sanitize(edge.get("to_node", "?"))
        etype = edge.get("edge_type", "?")

        if from_n not in seen_nodes:
            lines.append(f"    {from_n}[{from_n}]")
            seen_nodes.add(from_n)
        if to_n not in seen_nodes:
            lines.append(f"    {to_n}[{to_n}]")
            seen_nodes.add(to_n)

        # Highlight path edges
        lines.append(f"    {from_n} ==>|{i + 1}. {etype}| {to_n}")

    lines.append("```")
    return "\n".join(lines)


def _sanitize(node_id: str) -> str:
    """Sanitize a node ID for use as a Mermaid identifier."""
    return node_id.replace(" ", "_").replace("-", "_").replace(".", "_").replace(":", "_").replace("/", "_").replace("(", "_").replace(")", "_").replace("'", "_").replace('"', "_")


def _edge_style(layer: str, edge_type: str) -> str:
    """Return Mermaid style suffix based on layer and edge type."""
    if edge_type == "CHALLENGED_BY":
        return ":::challenge"
    if edge_type == "SUPPORTS":
        return ":::support"
    if layer == "L1":
        return ":::structure"
    if layer == "L2":
        return ":::flow"
    return ""
