"""OHM domain seed templates.

Provides machine-readable seed content for `ohm standup` greenfield deployments.
Each template describes a small purpose-aligned graph (agents, values,
capabilities, concepts, sources, and edges) that can be POSTed to an OHM
daemon to bootstrap a minimum viable graph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TEMPLATE_DIR = Path(__file__).parent / "seeds"


@dataclass(frozen=True)
class SeedTemplate:
    """A loaded domain seed template."""

    name: str
    label: str
    description: str
    domain_schema: str
    agents: list[dict[str, Any]]
    values: list[dict[str, Any]]
    capabilities: list[dict[str, Any]]
    concepts: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    raw: dict[str, Any]

    @property
    def nodes(self) -> list[dict[str, Any]]:
        """All node-like records in one list."""
        return self.agents + self.values + self.capabilities + self.concepts + self.sources

    def node_ids(self) -> set[str]:
        """Return the set of node IDs defined in this template."""
        return {n["id"] for n in self.nodes}

    def validate(self) -> list[str]:
        """Return a list of validation errors; empty if valid."""
        errors: list[str] = []
        node_ids = self.node_ids()

        required_node_fields = {"id", "label", "type"}
        for category, nodes in [
            ("agents", self.agents),
            ("values", self.values),
            ("capabilities", self.capabilities),
            ("concepts", self.concepts),
            ("sources", self.sources),
        ]:
            for n in nodes:
                missing = required_node_fields - set(n.keys())
                if missing:
                    errors.append(f"{category} node {n.get('id', '?')} missing fields: {sorted(missing)}")

        required_edge_fields = {"from_node", "to_node", "edge_type", "layer"}
        for i, e in enumerate(self.edges):
            missing = required_edge_fields - set(e.keys())
            if missing:
                errors.append(f"edge {i} missing fields: {sorted(missing)}")
                continue
            if e["from_node"] not in node_ids:
                errors.append(f"edge {i} references unknown from_node: {e['from_node']}")
            if e["to_node"] not in node_ids:
                errors.append(f"edge {i} references unknown to_node: {e['to_node']}")

        return errors


def list_templates() -> list[str]:
    """Return available template names."""
    names: list[str] = []
    if not TEMPLATE_DIR.exists():
        return names
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        names.append(path.stem)
    return names


def load_template(name: str) -> SeedTemplate:
    """Load a seed template by name."""
    path = TEMPLATE_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Seed template not found: {name}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SeedTemplate(
        name=raw["name"],
        label=raw["label"],
        description=raw["description"],
        domain_schema=raw["domain_schema"],
        agents=raw.get("agents", []),
        values=raw.get("values", []),
        capabilities=raw.get("capabilities", []),
        concepts=raw.get("concepts", []),
        sources=raw.get("sources", []),
        edges=raw.get("edges", []),
        raw=raw,
    )


def seed_payload(name: str) -> dict[str, Any]:
    """Return a normalized seed payload: list of nodes and edges ready for POSTing."""
    template = load_template(name)
    errors = template.validate()
    if errors:
        raise ValueError(f"Template {name!r} is invalid:\n" + "\n".join(errors))

    nodes = []
    for n in template.nodes:
        node = {k: v for k, v in n.items() if k != "id"}
        node["id"] = n["id"]
        nodes.append(node)

    edges = []
    for e in template.edges:
        edge = {
            "from_node": e["from_node"],
            "to_node": e["to_node"],
            "edge_type": e["edge_type"],
            "layer": e["layer"],
        }
        if "confidence" in e:
            edge["confidence"] = e["confidence"]
        if "content" in e:
            edge["content"] = e["content"]
        edges.append(edge)

    return {"nodes": nodes, "edges": edges}
