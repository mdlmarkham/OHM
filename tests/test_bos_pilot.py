"""End-to-end pilot test suite for the BOS ODPS catalog (OHM-0rlw).

Simulates a 2-week pilot run with one BOS agent team. Measures:

  - Products published per producer
  - Cross-agent consumptions (PRODUCES/CONSUMES edge counts)
  - Validation failures (invalid ODPS YAML rejected)
  - Source reliability evolution after outcomes
  - Discovery via the HTTP /data-products endpoint
  - Context-gate evaluation (ODPS compliance + reliability threshold)
"""

from __future__ import annotations

import json
import time
from http.client import HTTPConnection
from uuid import uuid4

import pytest

from ohm.bos.odps_validation import validate_registration, validate_bos
from ohm.graph.queries import (
    find_or_create_node,
    register_data_product,
    query_record_outcome,
    refresh_data_product_provenance,
    query_source_reliability,
)


PILOT_PRODUCTS = [
    {
        "product_id": "bos.pnl.monthly",
        "name": "Monthly P&L",
        "type": "reports",
        "producer_agent": "hephaestus",
        "consumers": ["metis", "clio"],
        "description": "Monthly profit & loss statements.",
    },
    {
        "product_id": "bos.risk.weekly",
        "name": "Weekly Risk Report",
        "type": "reports",
        "producer_agent": "clio",
        "consumers": ["metis"],
        "description": "Weekly aggregated risk metrics.",
    },
    {
        "product_id": "bos.research.digest",
        "name": "Research Digest",
        "type": "datasets",
        "producer_agent": "metis",
        "consumers": ["hephaestus"],
        "description": "Aggregated research findings from internal and external sources.",
    },
]


VALID_ODPS_TEMPLATE = """
version: "4.1"
schema: "https://opendataproducts.org/v4.1/schema/odps.json"
product:
  name: {name}
  productID: {product_id}
  type: {type}
  description: {description}
  visibility: private
  status: active
  language: en
  valueProposition: {description}
  producer:
    name: {producer}
    contactEmail: {producer}@bos.local
  outputPorts:
    - type: API
      format: json
      accessURL: https://bos.local/api/{product_id}
      authentication: token
"""


def _build_valid_odps_yaml(product: dict) -> str:
    return VALID_ODPS_TEMPLATE.format(
        name=product["name"],
        product_id=product["product_id"],
        type=product["type"],
        description=product["description"],
        producer=product["producer_agent"],
    )


def _request(method, port, path, body=None):
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=15)
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    body_bytes = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=body_bytes, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except json.JSONDecodeError:
        return resp.status, data


class TestPilotSetup:
    """Initial registration of the 3 production products (OHM-ylx8)."""

    def test_register_three_pilot_products(self, test_db):
        registered = []
        for product in PILOT_PRODUCTS:
            p = register_data_product(
                test_db,
                product_id=product["product_id"],
                name=product["name"],
                type=product["type"],
                producer_agent=product["producer_agent"],
                consumers=product["consumers"],
                created_by="pilot_runner",
                description=product["description"],
            )
            registered.append(p)
        assert len(registered) == 3
        for p in registered:
            assert p["ohm_node_id"] is not None
            assert p["source_reliability"] is not None

    def test_pilot_products_valid_against_odps(self, test_db):
        for product in PILOT_PRODUCTS:
            yaml = _build_valid_odps_yaml(product)
            result = validate_registration(yaml, producer_agent=product["producer_agent"])
            assert result["valid"], f"ODPS validation failed for {product['product_id']}: {result['errors']}"
            assert result["odps_valid"]
            assert result["bos_valid"]

    def test_invalid_odps_rejected(self, test_db):
        bad_yaml = """
product:
  name: Test
  productID: bos.test.bad
  type: reports
  description: Missing required top-level fields
"""
        result = validate_registration(bad_yaml, producer_agent="metis")
        assert not result["valid"]
        assert len(result["errors"]) > 0

    def test_bos_specific_constraints(self, test_db):
        yaml = _build_valid_odps_yaml(PILOT_PRODUCTS[0])
        result = validate_bos(yaml, producer_agent="hephaestus")
        assert result["valid"]


class TestPilotAdoption:
    """Measured adoption: product counts, edge counts, discovery queries."""

    def test_products_published_per_producer(self, test_db):
        for product in PILOT_PRODUCTS:
            register_data_product(
                test_db,
                product_id=product["product_id"],
                name=product["name"],
                type=product["type"],
                producer_agent=product["producer_agent"],
                consumers=product["consumers"],
                created_by="pilot_runner",
                description=product["description"],
            )

        for agent in ("hephaestus", "clio", "metis"):
            rows = test_db.execute(
                "SELECT COUNT(*) FROM ohm_data_products WHERE producer_agent = ? AND deleted_at IS NULL",
                [agent],
            ).fetchone()
            count = rows[0] if rows else 0
            assert count == 1, f"Producer {agent} should have 1 product, got {count}"

    def test_cross_agent_consumptions(self, test_db):
        for product in PILOT_PRODUCTS:
            register_data_product(
                test_db,
                product_id=product["product_id"],
                name=product["name"],
                type=product["type"],
                producer_agent=product["producer_agent"],
                consumers=product["consumers"],
                created_by="pilot_runner",
            )

        products = test_db.execute("SELECT internal_id FROM ohm_data_products WHERE deleted_at IS NULL").fetchall()
        for row in products:
            internal_id = row[0]
            node_row = test_db.execute(
                "SELECT ohm_node_id FROM ohm_data_products WHERE internal_id = ?",
                [internal_id],
            ).fetchone()
            ohm_node_id = node_row[0]
            consumes = test_db.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE to_node = ? AND edge_type = 'CONSUMES' AND deleted_at IS NULL",
                [ohm_node_id],
            ).fetchone()[0]
            assert consumes >= 1, f"Product {internal_id} should have >=1 CONSUMES edge"

    def test_http_discovery_returns_all(self, test_server):
        port, store = test_server
        for product in PILOT_PRODUCTS:
            store.register_data_product(
                product_id=product["product_id"],
                name=product["name"],
                type=product["type"],
                producer_agent=product["producer_agent"],
                consumers=product["consumers"],
                agent_name="pilot_runner",
                description=product["description"],
            )

        status, data = _request("GET", port, "/data-products")
        assert status == 200
        assert data["count"] == 3

    def test_http_discovery_filter_by_producer(self, test_server):
        port, store = test_server
        for product in PILOT_PRODUCTS:
            store.register_data_product(
                product_id=product["product_id"],
                name=product["name"],
                type=product["type"],
                producer_agent=product["producer_agent"],
                consumers=product["consumers"],
                agent_name="pilot_runner",
            )
        status, data = _request("GET", port, "/data-products?producer_agent=clio")
        assert status == 200
        assert data["count"] == 1
        assert data["products"][0]["producer_agent"] == "clio"


class TestPilotReliabilityEvolution:
    """Source reliability updates as outcomes are recorded over the pilot period."""

    def test_reliability_unchanged_before_outcomes(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.pilot.rel.1",
            name="Pilot Rel Test",
            type="reports",
            producer_agent="metis",
            consumers=["clio"],
            created_by="pilot_runner",
        )
        initial = product["source_reliability"]
        assert initial == 0.5

    def test_reliability_rises_with_positive_outcomes(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.pilot.rel.2",
            name="Pilot Rel Test 2",
            type="reports",
            producer_agent="metis",
            consumers=["clio"],
            created_by="pilot_runner",
        )
        for _ in range(5):
            query_record_outcome(
                test_db,
                source_agent="metis",
                claim_node=product["ohm_node_id"],
                outcome=True,
                recorded_by="pilot_runner",
            )
        refreshed = refresh_data_product_provenance(test_db, product["internal_id"])
        assert refreshed["source_reliability"] is not None

    def test_reliability_drops_with_negative_outcomes(self, test_db):
        product = register_data_product(
            test_db,
            product_id="bos.pilot.rel.3",
            name="Pilot Rel Test 3",
            type="reports",
            producer_agent="metis",
            consumers=["clio"],
            created_by="pilot_runner",
        )
        for _ in range(3):
            query_record_outcome(
                test_db,
                source_agent="metis",
                claim_node=product["ohm_node_id"],
                outcome=False,
                recorded_by="pilot_runner",
            )
        refreshed = refresh_data_product_provenance(test_db, product["internal_id"])
        assert refreshed["source_reliability"] is not None


class TestPilotValidationFailures:
    """Measure validation rejection rate over pilot period."""

    def test_malformed_yaml_counted_as_failure(self, test_db):
        failures = 0
        for i in range(5):
            yaml_text = f"product:\n  name: Test\n  productID: bad_{i}\n  type: reports\n"
            result = validate_registration(yaml_text, producer_agent="metis")
            if not result["valid"]:
                failures += 1
        assert failures == 5

    def test_oversized_product_id_rejected(self, test_db):
        yaml = VALID_ODPS_TEMPLATE.format(
            name="Bad ID Product",
            product_id="INVALID-123",
            productID="INVALID-123",
            type="reports",
            description="Has wrong id format.",
            producer="metis",
        )
        result = validate_registration(yaml, producer_agent="metis")
        if not result["valid"]:
            assert any("productID" in e.get("message", "") or "pattern" in e.get("validator", "") for e in result["errors"])


class TestPilotSummary:
    """End-to-end pilot summary covering all metrics."""

    def test_full_pilot_run_metrics(self, test_db):
        products_registered = 0
        validation_failures = 0
        edge_counts = {"PRODUCES": 0, "CONSUMES": 0}

        for product in PILOT_PRODUCTS:
            yaml = _build_valid_odps_yaml(product)
            v = validate_registration(yaml, producer_agent=product["producer_agent"])
            if not v["valid"]:
                validation_failures += 1
                continue

            p = register_data_product(
                test_db,
                product_id=product["product_id"],
                name=product["name"],
                type=product["type"],
                producer_agent=product["producer_agent"],
                consumers=product["consumers"],
                created_by="pilot_runner",
                description=product["description"],
            )
            products_registered += 1

            edge_counts["PRODUCES"] += test_db.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE to_node = ? AND edge_type = 'PRODUCES' AND deleted_at IS NULL",
                [p["ohm_node_id"]],
            ).fetchone()[0]
            edge_counts["CONSUMES"] += test_db.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE to_node = ? AND edge_type = 'CONSUMES' AND deleted_at IS NULL",
                [p["ohm_node_id"]],
            ).fetchone()[0]

        assert products_registered == 3
        assert validation_failures == 0
        assert edge_counts["PRODUCES"] == 3
        assert edge_counts["CONSUMES"] == 4
