"""OHM-842: Tag-filtered search and APPLIES_TO cross-layer edges.

Tests for:
  - ?tags= query parameter on GET /search (AND semantics)
  - APPLIES_TO edge type in L3 and L4
"""

from __future__ import annotations

import pytest
from tests.conftest import _request


@pytest.mark.xdist_group("server")
class TestTagFilteredSearch:
    """OHM-842: ?tags= on GET /search uses AND-semantics json_contains filtering."""

    def test_single_tag_filter(self, test_server):
        port, store = test_server
        store.write_node("tag_alpha", "Alpha Concept", "concept", agent_name="test",
                         tags=["plant:RCC", "solar"])
        store.write_node("tag_beta", "Beta Concept", "concept", agent_name="test",
                         tags=["wind"])
        status, data = _request("GET", port, "/search?q=Concept&tags=plant:RCC")
        assert status == 200
        results = data.get("results", data) if isinstance(data, dict) else data
        ids = [r.get("id") for r in results]
        assert "tag_alpha" in ids
        assert "tag_beta" not in ids

    def test_multi_tag_and_semantics(self, test_server):
        port, store = test_server
        store.write_node("tag_both", "Both Tags Node", "concept", agent_name="test",
                         tags=["solar", "P0"])
        store.write_node("tag_solar_only", "Solar Only Node", "concept", agent_name="test",
                         tags=["solar"])
        store.write_node("tag_p0_only", "P0 Only Node", "concept", agent_name="test",
                         tags=["P0"])
        status, data = _request("GET", port, "/search?q=Tags&tags=solar&tags=P0")
        assert status == 200
        results = data.get("results", data) if isinstance(data, dict) else data
        ids = [r.get("id") for r in results]
        assert "tag_both" in ids
        assert "tag_solar_only" not in ids
        assert "tag_p0_only" not in ids

    def test_no_matching_tags_returns_empty(self, test_server):
        port, store = test_server
        store.write_node("tag_nomatch", "NoMatch Concept", "concept", agent_name="test",
                         tags=["solar"])
        status, data = _request("GET", port, "/search?q=NoMatchConcept&tags=nonexistent")
        assert status == 200
        results = data.get("results", data) if isinstance(data, dict) else data
        ids = [r.get("id") for r in results]
        assert "tag_nomatch" not in ids

    def test_empty_tags_param_returns_all(self, test_server):
        port, store = test_server
        store.write_node("tag_all1", "AllTags1 Concept", "concept", agent_name="test",
                         tags=["solar"])
        store.write_node("tag_all2", "AllTags2 Concept", "concept", agent_name="test",
                         tags=["wind"])
        status, data = _request("GET", port, "/search?q=AllTagsConcept")
        assert status == 200
        results = data.get("results", data) if isinstance(data, dict) else data
        ids = [r.get("id") for r in results]
        assert "tag_all1" in ids
        assert "tag_all2" in ids

    def test_tag_filter_combines_with_type(self, test_server):
        port, store = test_server
        store.write_node("tag_type1", "TypeFilterSolar Concept", "concept", agent_name="test",
                         tags=["solar"])
        store.write_node("tag_type2", "TypeFilterSolar Pattern", "pattern", agent_name="test",
                         tags=["solar"])
        status, data = _request("GET", port, "/search?q=TypeFilterSolar&tags=solar&type=concept")
        assert status == 200
        results = data.get("results", data) if isinstance(data, dict) else data
        assert len(results) >= 1
        assert all(r.get("type") == "concept" for r in results)

    def test_tag_filter_node_without_tags_excluded(self, test_server):
        """Nodes with NULL tags should not match any tag filter."""
        port, store = test_server
        store.write_node("tag_notags", "NoTags Concept", "concept", agent_name="test")
        store.write_node("tag_withtags", "WithTags Concept", "concept", agent_name="test",
                         tags=["solar"])
        status, data = _request("GET", port, "/search?q=Concept&tags=solar")
        assert status == 200
        results = data.get("results", data) if isinstance(data, dict) else data
        ids = [r.get("id") for r in results]
        assert "tag_notags" not in ids
        assert "tag_withtags" in ids

    def test_tag_filter_search_text_combined(self, test_server):
        port, store = test_server
        store.write_node("tag_text1", "Solar Farm Alpha", "concept", agent_name="test",
                         tags=["solar", "plant:RCC"])
        store.write_node("tag_text2", "Wind Farm Beta", "concept", agent_name="test",
                         tags=["solar"])
        store.write_node("tag_text3", "Solar Panel Gamma", "concept", agent_name="test",
                         tags=["wind"])
        status, data = _request("GET", port, "/search?q=Farm&tags=solar")
        assert status == 200
        results = data.get("results", data) if isinstance(data, dict) else data
        ids = [r.get("id") for r in results]
        assert "tag_text1" in ids
        assert "tag_text2" in ids
        assert "tag_text3" not in ids


@pytest.mark.xdist_group("server")
class TestAppliesToEdgeType:
    """OHM-842: APPLIES_TO is valid in both L3 and L4 layers."""

    def test_applies_to_l3(self, test_server):
        port, store = test_server
        store.write_node("at_src_l3", "APPLIES_TO L3 Source", "concept", agent_name="test")
        store.write_node("at_dst_l3", "APPLIES_TO L3 Target", "concept", agent_name="test")
        status, data = _request("POST", port, "/edge", body={
            "from": "at_src_l3",
            "to": "at_dst_l3",
            "edge_type": "APPLIES_TO",
            "layer": "L3",
        })
        assert status in (200, 201)

    def test_applies_to_l4(self, test_server):
        port, store = test_server
        store.write_node("at_src_l4", "APPLIES_TO L4 Source", "concept", agent_name="test")
        store.write_node("at_dst_l4", "APPLIES_TO L4 Target", "concept", agent_name="test")
        status, data = _request("POST", port, "/edge", body={
            "from": "at_src_l4",
            "to": "at_dst_l4",
            "edge_type": "APPLIES_TO",
            "layer": "L4",
        })
        assert status in (200, 201)


class TestAppliesToSchema:
    """OHM-842: APPLIES_TO is in L3 and L4 edge types, not L0-L2."""

    def test_applies_to_in_l3(self):
        from ohm.graph.schema import LAYER_EDGE_TYPES
        assert "APPLIES_TO" in LAYER_EDGE_TYPES["L3"]

    def test_applies_to_in_l4(self):
        from ohm.graph.schema import LAYER_EDGE_TYPES
        assert "APPLIES_TO" in LAYER_EDGE_TYPES["L4"]

    def test_applies_to_not_in_l0_l2(self):
        from ohm.graph.schema import LAYER_EDGE_TYPES
        for layer in ("L0", "L1", "L2"):
            assert "APPLIES_TO" not in LAYER_EDGE_TYPES[layer]


@pytest.mark.xdist_group("server")
class TestSchemaVersion:
    """OHM-842: Schema version is 0.53.0."""

    def test_schema_version(self):
        from ohm.graph.schema import SCHEMA_VERSION
        assert SCHEMA_VERSION == "0.53.0"

    def test_migration_entry_exists(self):
        from ohm.graph.schema import MIGRATIONS
        versions = [v for v, _, _ in MIGRATIONS]
        assert "0.53.0" in versions
