"""Tests for ODPS v4.1 validation adapter (OHM-ux7z / ADR-027).

Tests the three-layer validation:
  1. ODPS schema compliance (canonical JSON Schema draft 2020-12)
  2. BOS-specific constraints (producer_agent, visibility, MCP access)
  3. Combined registration gate
"""

from __future__ import annotations

import pytest

from ohm.bos.odps_validation import validate_odps, validate_bos, validate_registration


_VALID_ODPS = """
schema: https://opendataproducts.org/v4.1/schema/odps.json
version: v4.1
product:
  details:
    en:
      name: Monthly P&L Report
      productID: bos.pnl.monthly
      visibility: private
      status: production
      type: reports
      valueProposition: Monthly profit and loss summary for BOS agents
"""

_MISSING_REQUIRED = """
schema: https://opendataproducts.org/v4.1/schema/odps.json
version: v4.1
product:
  details:
    en:
      name: Missing fields
"""

_BAD_VISIBILITY = """
schema: https://opendataproducts.org/v4.1/schema/odps.json
version: v4.1
product:
  details:
    en:
      name: Public product
      productID: bos.public
      visibility: public
      status: production
      type: reports
"""

_BAD_ACCESS_FORMAT = """
schema: https://opendataproducts.org/v4.1/schema/odps.json
version: v4.1
product:
  details:
    en:
      name: Non-MCP access
      productID: bos.api
      visibility: private
      status: production
      type: reports
  dataAccess:
    - format: JSON
      specification: OAS
"""


class TestValidateODPS:
    def test_valid_document_passes(self):
        result = validate_odps(_VALID_ODPS)
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["compliance_level"] is not None

    def test_missing_required_fields_fails(self):
        result = validate_odps(_MISSING_REQUIRED)
        assert result["valid"] is False
        assert len(result["errors"]) > 0
        error_msgs = " ".join(e["message"] for e in result["errors"])
        assert "productID" in error_msgs
        assert "visibility" in error_msgs
        assert "status" in error_msgs
        assert "type" in error_msgs

    def test_malformed_yaml_fails(self):
        result = validate_odps("not: [valid: yaml:")
        assert result["valid"] is False
        assert "Malformed YAML" in result["errors"][0]["message"]

    def test_empty_document_fails(self):
        result = validate_odps("")
        assert result["valid"] is False
        assert "empty" in result["errors"][0]["message"].lower()

    def test_non_mapping_fails(self):
        result = validate_odps("just a string")
        assert result["valid"] is False
        assert "mapping" in result["errors"][0]["message"].lower()

    def test_dict_input_accepted(self):
        import yaml

        doc = yaml.safe_load(_VALID_ODPS)
        result = validate_odps(doc)
        assert result["valid"] is True


class TestValidateBOS:
    def test_valid_bos_document_passes(self):
        result = validate_bos(_VALID_ODPS, producer_agent="hephaestus")
        assert result["valid"] is True
        assert result["errors"] == []

    def test_missing_producer_agent_fails(self):
        result = validate_bos(_VALID_ODPS, producer_agent=None)
        assert result["valid"] is False
        assert any("producer_agent" in e["path"] for e in result["errors"])

    def test_empty_producer_agent_fails(self):
        result = validate_bos(_VALID_ODPS, producer_agent="  ")
        assert result["valid"] is False
        assert any("producer_agent" in e["path"] for e in result["errors"])

    def test_public_visibility_rejected(self):
        result = validate_bos(_BAD_VISIBILITY, producer_agent="metis")
        assert result["valid"] is False
        assert any("visibility" in e["path"] for e in result["errors"])

    def test_non_mcp_access_format_rejected(self):
        result = validate_bos(_BAD_ACCESS_FORMAT, producer_agent="metis")
        assert result["valid"] is False
        assert any("format" in e["path"] for e in result["errors"])
        assert any("specification" in e["path"] for e in result["errors"])


class TestValidateRegistration:
    def test_valid_document_with_producer_passes(self):
        result = validate_registration(_VALID_ODPS, producer_agent="hephaestus")
        assert result["valid"] is True
        assert result["odps_valid"] is True
        assert result["bos_valid"] is True
        assert result["compliance_level"] is not None

    def test_valid_odps_but_missing_producer_fails(self):
        result = validate_registration(_VALID_ODPS, producer_agent=None)
        assert result["valid"] is False
        assert result["odps_valid"] is True
        assert result["bos_valid"] is False

    def test_invalid_odps_fails_regardless_of_producer(self):
        result = validate_registration(_MISSING_REQUIRED, producer_agent="hephaestus")
        assert result["valid"] is False
        assert result["odps_valid"] is False

    def test_both_odps_and_bos_failures_collected(self):
        result = validate_registration(_BAD_VISIBILITY, producer_agent=None)
        assert result["valid"] is False
        assert result["odps_valid"] is True  # public is valid ODPS, just not BOS
        assert result["bos_valid"] is False
        # Both BOS errors collected: missing producer + bad visibility
        bos_errors = [e for e in result["errors"] if e.get("validator", "").startswith("bos") or "producer_agent" in e.get("path", "")]
        assert len(bos_errors) >= 2
