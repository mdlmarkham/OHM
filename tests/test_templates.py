"""Tests for OHM domain seed templates (ADR-022)."""

from __future__ import annotations

import pytest

from ohm.templates import list_templates, load_template, seed_payload


def test_list_templates_returns_known_templates():
    names = list_templates()
    expected = {"personal-knowledge", "devsecops", "trading-research", "data-pipelines"}
    assert expected.issubset(set(names))


@pytest.mark.parametrize(
    "name",
    [
        "personal-knowledge",
        "devsecops",
        "trading-research",
        "data-pipelines",
    ],
)
def test_each_template_loads_and_validates(name: str):
    template = load_template(name)
    errors = template.validate()
    assert not errors, f"Template {name!r} validation errors: {errors}"
    assert template.nodes
    assert template.edges


@pytest.mark.parametrize(
    "name",
    [
        "personal-knowledge",
        "devsecops",
        "trading-research",
        "data-pipelines",
    ],
)
def test_seed_payload_contains_nodes_and_edges(name: str):
    payload = seed_payload(name)
    assert "nodes" in payload
    assert "edges" in payload
    assert len(payload["nodes"]) >= 5
    assert len(payload["edges"]) >= 3
    # Every edge endpoint must appear in the node list
    node_ids = {n["id"] for n in payload["nodes"]}
    for edge in payload["edges"]:
        assert edge["from_node"] in node_ids
        assert edge["to_node"] in node_ids


def test_personal_knowledge_minimum_viable_graph():
    """ADR-022 targets ≥8 nodes and ≥6 edges for a minimum viable graph."""
    payload = seed_payload("personal-knowledge")
    assert len(payload["nodes"]) >= 8
    assert len(payload["edges"]) >= 6


def test_load_missing_template_raises():
    with pytest.raises(FileNotFoundError):
        load_template("nonexistent-template")
