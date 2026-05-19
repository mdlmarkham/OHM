"""Tests for the OHM visualization module (Mermaid diagram export)."""

import pytest

from ohm.visualization import to_mermaid, to_mermaid_path, _sanitize, _edge_style


class TestSanitize:
    """Tests for _sanitize node ID sanitizer."""

    def test_simple_id(self):
        assert _sanitize("node1") == "node1"

    def test_spaces_to_underscores(self):
        assert _sanitize("my node") == "my_node"

    def test_hyphens_to_underscores(self):
        assert _sanitize("my-node") == "my_node"

    def test_dots_to_underscores(self):
        assert _sanitize("schema.table") == "schema_table"

    def test_colons_to_underscores(self):
        assert _sanitize("ns:id") == "ns_id"

    def test_slashes_to_underscores(self):
        assert _sanitize("path/to/node") == "path_to_node"

    def test_parens_to_underscores(self):
        assert _sanitize("node(v2)") == "node_v2_"

    def test_quotes_to_underscores(self):
        assert _sanitize("node'x") == "node_x"
        assert _sanitize('node"x') == "node_x"

    def test_complex_id(self):
        result = _sanitize("my node (v2.1)/final")
        assert " " not in result
        assert "(" not in result
        assert ")" not in result
        assert "/" not in result


class TestEdgeStyle:
    """Tests for _edge_style Mermaid style suffix."""

    def test_challenged_by(self):
        assert _edge_style("L3", "CHALLENGED_BY") == ":::challenge"

    def test_supports(self):
        assert _edge_style("L3", "SUPPORTS") == ":::support"

    def test_l1_structure(self):
        assert _edge_style("L1", "CONTAINS") == ":::structure"

    def test_l2_flow(self):
        assert _edge_style("L2", "FEEDS") == ":::flow"

    def test_l3_no_special_style(self):
        assert _edge_style("L3", "CAUSES") == ""

    def test_l4_no_special_style(self):
        assert _edge_style("L4", "EXPECTS") == ""

    def test_challenged_by_takes_priority_over_layer(self):
        """CHALLENGED_BY style takes priority over layer style."""
        assert _edge_style("L1", "CHALLENGED_BY") == ":::challenge"


class TestToMermaid:
    """Tests for to_mermaid flowchart generation."""

    def test_empty_edges(self):
        result = to_mermaid([])
        assert "flowchart LR" in result
        assert "```" in result

    def test_single_edge(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES", "layer": "L3", "confidence": 0.8},
        ]
        result = to_mermaid(edges)
        assert "flowchart LR" in result
        assert "A" in result
        assert "B" in result
        assert "CAUSES" in result
        assert "c:0.80" in result

    def test_multiple_edges(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES", "layer": "L3"},
            {"from_node": "B", "to_node": "C", "edge_type": "SUPPORTS", "layer": "L3"},
        ]
        result = to_mermaid(edges)
        assert "CAUSES" in result
        assert "SUPPORTS" in result

    def test_no_duplicate_nodes(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES", "layer": "L3"},
            {"from_node": "B", "to_node": "C", "edge_type": "SUPPORTS", "layer": "L3"},
        ]
        result = to_mermaid(edges)
        # Node B should appear only once as a declaration
        assert result.count("B[B]") == 1

    def test_title(self):
        result = to_mermaid([], title="My Graph")
        assert "title[My Graph]" in result

    def test_direction_td(self):
        result = to_mermaid([], direction="TD")
        assert "flowchart TD" in result

    def test_direction_rl(self):
        result = to_mermaid([], direction="RL")
        assert "flowchart RL" in result

    def test_edge_without_confidence(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES", "layer": "L3"},
        ]
        result = to_mermaid(edges)
        assert "CAUSES" in result
        assert "c:" not in result  # No confidence label

    def test_challenged_by_style(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CHALLENGED_BY", "layer": "L3"},
        ]
        result = to_mermaid(edges)
        assert ":::challenge" in result

    def test_supports_style(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "SUPPORTS", "layer": "L3"},
        ]
        result = to_mermaid(edges)
        assert ":::support" in result

    def test_l1_structure_style(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CONTAINS", "layer": "L1"},
        ]
        result = to_mermaid(edges)
        assert ":::structure" in result

    def test_l2_flow_style(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "FEEDS", "layer": "L2"},
        ]
        result = to_mermaid(edges)
        assert ":::flow" in result

    def test_sanitizes_node_ids(self):
        edges = [
            {"from_node": "my node (v2)", "to_node": "other/node", "edge_type": "CAUSES", "layer": "L3"},
        ]
        result = to_mermaid(edges)
        # Should not contain spaces or special chars in node references
        assert "my node" not in result.split("flowchart LR")[1] or "my_node" in result

    def test_mermaid_code_fence(self):
        result = to_mermaid([])
        assert result.startswith("```mermaid")
        assert result.endswith("```")


class TestToMermaidPath:
    """Tests for to_mermaid_path path visualization."""

    def test_empty_path(self):
        result = to_mermaid_path([])
        assert "flowchart LR" in result
        assert "```" in result

    def test_single_step_path(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES"},
        ]
        result = to_mermaid_path(edges)
        assert "==>" in result  # Path uses thick arrows
        assert "1. CAUSES" in result  # Numbered steps

    def test_multi_step_path(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES"},
            {"from_node": "B", "to_node": "C", "edge_type": "SUPPORTS"},
            {"from_node": "C", "to_node": "D", "edge_type": "PREDICTS"},
        ]
        result = to_mermaid_path(edges)
        assert "1. CAUSES" in result
        assert "2. SUPPORTS" in result
        assert "3. PREDICTS" in result

    def test_path_title(self):
        result = to_mermaid_path([], title="Evidence Chain")
        assert "title[Evidence Chain]" in result

    def test_path_uses_thick_arrows(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES"},
        ]
        result = to_mermaid_path(edges)
        assert "==>" in result  # Thick arrow for path highlighting

    def test_path_no_duplicate_nodes(self):
        edges = [
            {"from_node": "A", "to_node": "B", "edge_type": "CAUSES"},
            {"from_node": "B", "to_node": "C", "edge_type": "SUPPORTS"},
        ]
        result = to_mermaid_path(edges)
        assert result.count("B[B]") == 1

    def test_path_mermaid_code_fence(self):
        result = to_mermaid_path([])
        assert result.startswith("```mermaid")
        assert result.endswith("```")