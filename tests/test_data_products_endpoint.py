"""Tests for the BOS ODPS data product catalog HTTP endpoints (OHM-xdtl).

Verifies:
  - GET  /data-products — list products with optional filters
  - GET  /data-products/{id} — fetch a single product
  - POST /data-products — register a product (with optional ODPS YAML validation)
  - Validation errors return HTTP 422
"""

from __future__ import annotations

import json
import pytest


VALID_ODPS_YAML = """
version: "4.1"
schema: "https://opendataproducts.org/v4.1/schema/odps.json"
product:
  name: BOS Test Product
  productID: bos.test.http.1
  type: reports
  description: A test product registered via HTTP.
  visibility: private
  status: draft
  language: en
  valueProposition: Validates the HTTP endpoint.
  producer:
    name: hephaestus
    contactEmail: hephaestus@bos.local
  outputPorts:
    - type: API
      format: json
      accessURL: https://bos.local/api/test
      authentication: token
"""

INVALID_ODPS_YAML = """
product:
  name: ""
  productID: not_valid_format
  type: not_a_real_type
  description: ""
"""


@pytest.mark.xdist_group("server")
class TestDataProductListEndpoint:
    def test_list_empty(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/data-products")
        assert status == 200
        assert data["count"] == 0
        assert data["products"] == []

    def test_list_after_register(self, test_server):
        port, store = test_server
        store.register_data_product(
            product_id="bos.http.list.1",
            name="List Test 1",
            type="reports",
            producer_agent="metis",
            agent_name="metis",
        )
        store.register_data_product(
            product_id="bos.http.list.2",
            name="List Test 2",
            type="datasets",
            producer_agent="clio",
            agent_name="clio",
        )
        status, data = _request("GET", port, "/data-products")
        assert status == 200
        assert data["count"] == 2

    def test_list_filter_by_producer(self, test_server):
        port, store = test_server
        store.register_data_product(
            product_id="bos.http.filt.1",
            name="A",
            type="reports",
            producer_agent="metis",
            agent_name="metis",
        )
        store.register_data_product(
            product_id="bos.http.filt.2",
            name="B",
            type="reports",
            producer_agent="clio",
            agent_name="clio",
        )
        status, data = _request("GET", port, "/data-products?producer_agent=metis")
        assert status == 200
        assert data["count"] == 1
        assert data["products"][0]["product_id"] == "bos.http.filt.1"

    def test_list_filter_by_type(self, test_server):
        port, store = test_server
        store.register_data_product(
            product_id="bos.http.type.1",
            name="R",
            type="reports",
            producer_agent="metis",
            agent_name="metis",
        )
        store.register_data_product(
            product_id="bos.http.type.2",
            name="D",
            type="datasets",
            producer_agent="clio",
            agent_name="clio",
        )
        status, data = _request("GET", port, "/data-products?type=datasets")
        assert status == 200
        assert data["count"] == 1


@pytest.mark.xdist_group("server")
class TestDataProductGetEndpoint:
    def test_get_existing(self, test_server):
        port, store = test_server
        product = store.register_data_product(
            product_id="bos.http.get.1",
            name="Get Test",
            type="reports",
            producer_agent="metis",
            agent_name="metis",
        )
        status, data = _request("GET", port, f"/data-products/{product['internal_id']}")
        assert status == 200
        assert data["internal_id"] == product["internal_id"]
        assert data["name"] == "Get Test"

    def test_get_missing_returns_404(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/data-products/nonexistent")
        assert status == 404
        assert data["error"] == "not_found"


@pytest.mark.xdist_group("server")
class TestDataProductRegisterEndpoint:
    def test_register_minimal(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/data-products",
            body={
                "product_id": "bos.http.reg.1",
                "name": "Register Test",
                "type": "reports",
                "producer_agent": "metis",
            },
        )
        assert status == 201
        assert data["product_id"] == "bos.http.reg.1"
        assert data["ohm_node_id"] is not None

    def test_register_with_odps_yaml(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/data-products",
            body={
                "product_id": "bos.http.yaml.1",
                "name": "YAML Register",
                "type": "reports",
                "producer_agent": "hephaestus",
                "odps_yaml": VALID_ODPS_YAML,
            },
        )
        assert status == 201
        assert data["compliance_level"] is not None

    def test_register_invalid_odps_returns_422(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/data-products",
            body={
                "product_id": "bos.http.bad.1",
                "name": "Bad YAML",
                "type": "reports",
                "producer_agent": "metis",
                "odps_yaml": INVALID_ODPS_YAML,
            },
        )
        assert status == 422
        assert data["error"] == "validation_failed"
        assert "errors" in data

    def test_register_with_consumers_creates_edges(self, test_server):
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/data-products",
            body={
                "product_id": "bos.http.cons.1",
                "name": "Consumer Test",
                "type": "reports",
                "producer_agent": "metis",
                "consumers": ["clio", "hephaestus"],
            },
        )
        assert status == 201
        consumes = store.conn.execute(
            "SELECT from_node FROM ohm_edges WHERE to_node = ? AND edge_type = 'CONSUMES' AND deleted_at IS NULL",
            [data["ohm_node_id"]],
        ).fetchall()
        assert len(consumes) == 2


def _request(method, port, path, body=None):
    """Local HTTP helper (mirrors conftest._request signature)."""
    from http.client import HTTPConnection

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
