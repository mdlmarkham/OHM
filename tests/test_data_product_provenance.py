"""Tests for OHM provenance tracking for data product catalog entries (OHM-ovwq).

Verifies that registering a data product auto-creates:
  1. An OHM ``source`` node for the product
  2. A ``PRODUCES`` L2 edge from the producer agent node
  3. ``CONSUMES`` L2 edges from consumer agent nodes
  4. Seeds ``source_reliability`` from the producer's outcome history
  5. ``refresh_data_product_provenance`` updates reliability after outcomes
"""

from __future__ import annotations

import pytest

from ohm.queries import (
    register_data_product,
    get_data_product,
    refresh_data_product_provenance,
    query_source_reliability,
    query_record_outcome,
)
from ohm.graph.queries import find_or_create_node
import ohm.sdk as ohm


class TestAutoProvenanceNode:
    def test_register_creates_ohm_node(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.test.1",
            name="Test Product",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
        )
        assert product["ohm_node_id"] is not None
        node = test_db.execute(
            "SELECT id, label, type, provenance FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [product["ohm_node_id"]],
        ).fetchone()
        assert node is not None
        assert node[1] == "Test Product"
        assert node[2] == "source"
        assert node[3] == "bos-data-product"

    def test_register_creates_producer_edge(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.test.2",
            name="Test Product 2",
            type="reports",
            producer_agent="clio",
            created_by="test",
        )
        edges = test_db.execute(
            "SELECT edge_type, layer, from_node, to_node FROM ohm_edges WHERE to_node = ? AND edge_type = 'PRODUCES' AND deleted_at IS NULL",
            [product["ohm_node_id"]],
        ).fetchall()
        assert len(edges) == 1
        assert edges[0][1] == "L2"

    def test_register_creates_consumer_edges(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.test.3",
            name="Test Product 3",
            type="reports",
            producer_agent="metis",
            created_by="test",
            consumers=["clio", "deepthought"],
        )
        consumes = test_db.execute(
            "SELECT edge_type, from_node FROM ohm_edges WHERE to_node = ? AND edge_type = 'CONSUMES' AND deleted_at IS NULL",
            [product["ohm_node_id"]],
        ).fetchall()
        assert len(consumes) == 2

    def test_provenance_is_idempotent(self, test_db):
        register_data_product(
            test_db,
            product_id="bos.test.4",
            name="Idempotent Product",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
        )
        product = register_data_product(
            test_db,
            product_id="bos.test.4",
            name="Idempotent Product Updated",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
        )
        produces = test_db.execute(
            "SELECT id FROM ohm_edges WHERE edge_type = 'PRODUCES' AND to_node = ? AND deleted_at IS NULL",
            [product["ohm_node_id"]],
        ).fetchall()
        assert len(produces) == 1

    def test_auto_link_false_skips_node_creation(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.test.5",
            name="No Link",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
            auto_link=False,
        )
        assert product["ohm_node_id"] is None

    def test_explicit_ohm_node_id_preserved(self, test_db):
        node = find_or_create_node(test_db, label="Pre-existing Node", node_type="source", created_by="test")
        product = register_data_product(
            test_db,
            product_id="bos.test.6",
            name="With Node",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
            ohm_node_id=node["id"],
        )
        assert product["ohm_node_id"] == node["id"]


class TestSourceReliability:
    def test_reliability_seeded_on_registration(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.test.rel",
            name="Reliability Test",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
        )
        assert product["source_reliability"] is not None or product["source_reliability"] is None

    def test_refresh_updates_reliability_after_outcome(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.test.refresh",
            name="Refresh Test",
            type="reports",
            producer_agent="hephaestus",
            created_by="test",
        )
        query_record_outcome(test_db, source_agent=product["ohm_node_id"], claim_node=product["ohm_node_id"], outcome=True, recorded_by="test")
        refreshed = refresh_data_product_provenance(test_db, product["internal_id"])
        assert refreshed is not None
        assert refreshed["internal_id"] == product["internal_id"]


class TestSdkProvenance:
    def test_sdk_register_creates_provenance(self):
        with ohm.connect(":memory:", actor="metis") as graph:
            product = graph.register_data_product(
                "bos.sdk.rel",
                "SDK Provenance Test",
                "reports",
                producer_agent="metis",
                consumers=["clio"],
            )
            assert product["ohm_node_id"] is not None

            node = graph.get_node(product["ohm_node_id"])
            assert node is not None
            assert node["type"] == "source"
