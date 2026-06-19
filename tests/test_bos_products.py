"""First 3 production data products for BOS (OHM-ylx8 / ADR-027).

Defines three ODPS v4.1-compliant data products from BOS agent outputs,
validates them through the registration gate, and registers them in the
catalog via the SDK.

Products:
  1. bos.pnl.monthly — Monthly P&L Statement (producer: hephaestus)
  2. bos.risk.weekly — Weekly Risk Report (producer: clio)
  3. bos.research.digest — Research Digest (producer: metis)
"""

from __future__ import annotations

import pytest

from ohm.bos.odps_validation import validate_registration
import ohm.sdk as ohm


_PNL_YAML = """
schema: https://opendataproducts.org/v4.1/schema/odps.json
version: v4.1
product:
  details:
    en:
      name: Monthly P&L Statement
      productID: bos.pnl.monthly
      visibility: organisation
      status: production
      type: reports
      valueProposition: Monthly profit and loss summary for executive review
      description: Aggregated revenue, costs, and net margin by business unit
      productVersion: v1.0.0
      tags:
        - finance
        - pnl
        - monthly
      categories:
        - financial-reporting
      outputFileFormats:
        - JSON
        - CSV
"""

_RISK_YAML = """
schema: https://opendataproducts.org/v4.1/schema/odps.json
version: v4.1
product:
  details:
    en:
      name: Weekly Risk Report
      productID: bos.risk.weekly
      visibility: organisation
      status: production
      type: reports
      valueProposition: Weekly risk assessment with severity scoring and trend analysis
      description: Top risks ranked by probability × impact with mitigation status
      productVersion: v1.0.0
      tags:
        - risk
        - weekly
        - assessment
      categories:
        - risk-management
      outputFileFormats:
        - JSON
"""

_RESEARCH_YAML = """
schema: https://opendataproducts.org/v4.1/schema/odps.json
version: v4.1
product:
  details:
    en:
      name: Research Digest
      productID: bos.research.digest
      visibility: organisation
      status: production
      type: reports
      valueProposition: Synthesized research findings with source citations and confidence scores
      description: Curated research summaries from multiple sources with OHM provenance tracking
      productVersion: v1.0.0
      tags:
        - research
        - synthesis
        - digest
      categories:
        - research-intelligence
      outputFileFormats:
        - JSON
        - plain text
"""

_PRODUCTS = [
    ("bos.pnl.monthly", "Monthly P&L Statement", "reports", "hephaestus", _PNL_YAML),
    ("bos.risk.weekly", "Weekly Risk Report", "reports", "clio", _RISK_YAML),
    ("bos.research.digest", "Research Digest", "reports", "metis", _RESEARCH_YAML),
]


class TestProductValidation:
    """All 3 products must pass the ODPS + BOS registration gate."""

    @pytest.mark.parametrize("product_id,name,ptype,producer,yaml", _PRODUCTS)
    def test_product_passes_registration_gate(self, product_id, name, ptype, producer, yaml):
        result = validate_registration(yaml, producer_agent=producer)
        assert result["valid"] is True, f"{product_id} failed: {result['errors']}"
        assert result["odps_valid"] is True
        assert result["bos_valid"] is True
        assert result["compliance_level"] is not None

    @pytest.mark.parametrize("product_id,name,ptype,producer,yaml", _PRODUCTS)
    def test_product_visibility_is_organisation(self, product_id, name, ptype, producer, yaml):
        import yaml as yaml_lib

        doc = yaml_lib.safe_load(yaml)
        vis = doc["product"]["details"]["en"]["visibility"]
        assert vis == "organisation", f"{product_id} visibility should be 'organisation', got '{vis}'"


class TestProductRegistration:
    """Register all 3 products in the catalog and verify they're discoverable."""

    def test_register_all_three_products(self):
        with ohm.connect(":memory:", actor="test") as graph:
            registered = []
            for product_id, name, ptype, producer, yaml_doc in _PRODUCTS:
                product = graph.register_data_product(
                    product_id,
                    name,
                    ptype,
                    producer_agent=producer,
                    status="production",
                    visibility="organisation",
                    odps_yaml=yaml_doc.strip(),
                )
                registered.append(product)
                assert product["product_id"] == product_id
                assert product["producer_agent"] == producer
                assert product["status"] == "production"

            assert len(registered) == 3

            all_products = graph.list_data_products()
            assert len(all_products) == 3

    def test_products_filterable_by_producer(self):
        with ohm.connect(":memory:", actor="test") as graph:
            for product_id, name, ptype, producer, yaml_doc in _PRODUCTS:
                graph.register_data_product(
                    product_id, name, ptype,
                    producer_agent=producer, status="production",
                    visibility="organisation",
                )

            hephaestus_products = graph.list_data_products(producer_agent="hephaestus")
            assert len(hephaestus_products) == 1
            assert hephaestus_products[0]["product_id"] == "bos.pnl.monthly"

            clio_products = graph.list_data_products(producer_agent="clio")
            assert len(clio_products) == 1
            assert clio_products[0]["product_id"] == "bos.risk.weekly"

            metis_products = graph.list_data_products(producer_agent="metis")
            assert len(metis_products) == 1
            assert metis_products[0]["product_id"] == "bos.research.digest"

    def test_products_filterable_by_type(self):
        with ohm.connect(":memory:", actor="test") as graph:
            for product_id, name, ptype, producer, yaml_doc in _PRODUCTS:
                graph.register_data_product(
                    product_id, name, ptype,
                    producer_agent=producer, status="production",
                    visibility="organisation",
                )

            reports = graph.list_data_products(type="reports")
            assert len(reports) == 3

    def test_product_retrievable_by_odps_id(self):
        from ohm.queries import get_data_product_by_odps_id

        with ohm.connect(":memory:", actor="test") as graph:
            for product_id, name, ptype, producer, yaml_doc in _PRODUCTS:
                graph.register_data_product(
                    product_id, name, ptype,
                    producer_agent=producer, status="production",
                    visibility="organisation",
                )

            for product_id, _, _, _, _ in _PRODUCTS:
                product = get_data_product_by_odps_id(graph._conn, product_id)
                assert product is not None
                assert product["product_id"] == product_id

    def test_product_upsert_preserves_count(self):
        """Re-registering the same product updates it rather than duplicating."""
        with ohm.connect(":memory:", actor="test") as graph:
            graph.register_data_product(
                "bos.pnl.monthly", "Monthly P&L v1", "reports",
                producer_agent="hephaestus", status="draft",
                visibility="organisation",
            )
            graph.register_data_product(
                "bos.pnl.monthly", "Monthly P&L v2", "reports",
                producer_agent="hephaestus", status="production",
                visibility="organisation",
            )
            products = graph.list_data_products()
            assert len(products) == 1
            assert products[0]["name"] == "Monthly P&L v2"
            assert products[0]["status"] == "production"
