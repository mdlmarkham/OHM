"""Tests for gateway profile JSON Schema validation (OHM-912).

Pure unit tests — no fastmcp or ohmd required. The validation module only
depends on ``jsonschema`` (a core dependency), so these tests run in the
default CI matrix without the ``gateway`` extra installed.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from ohm.mcp.profile_validation import (
    RESERVED_TOOL_PREFIXES,
    format_validation_report,
    validate_profiles_file,
    validate_profiles_inline,
    validate_profiles_payload,
)


_VALID_PROFILE = {
    "api_key": "ohm-gw-test",
    "ohm_url": "http://127.0.0.1:8710",
    "ohm_token": "ohm-cu-test",
    "agent_id": "test-agent",
    "tenant_id": "devops",
    "allowed_tools": ["ohm_search", "ohm_get_node"],
    "read_only": False,
}

_VALID_PROFILE_MINIMAL = {
    "api_key": "ohm-gw-min",
    "ohm_url": "http://127.0.0.1:8710",
}


class TestValidateProfilesPayload:
    """Schema-level validation of a parsed profiles payload."""

    def test_valid_single_profile_passes(self):
        result = validate_profiles_payload([_VALID_PROFILE])
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["profile_count"] == 1

    def test_valid_minimal_profile_passes(self):
        """Only api_key and ohm_url are required."""
        result = validate_profiles_payload([_VALID_PROFILE_MINIMAL])
        assert result["valid"] is True
        assert result["profile_count"] == 1

    def test_single_object_payload_normalized_to_list(self):
        """A bare dict (not wrapped in a list) is accepted and normalized."""
        result = validate_profiles_payload(_VALID_PROFILE)
        assert result["valid"] is True
        assert result["profile_count"] == 1

    def test_multiple_valid_profiles_pass(self):
        result = validate_profiles_payload([_VALID_PROFILE, _VALID_PROFILE_MINIMAL])
        assert result["valid"] is True
        assert result["profile_count"] == 2

    def test_missing_required_api_key_fails(self):
        bad = {"ohm_url": "http://127.0.0.1:8710"}
        result = validate_profiles_payload([bad])
        assert result["valid"] is False
        assert len(result["errors"]) >= 1
        err = result["errors"][0]
        assert "api_key" in err["message"]
        assert err["path"].startswith("[0]")

    def test_missing_required_ohm_url_fails(self):
        bad = {"api_key": "k"}
        result = validate_profiles_payload([bad])
        assert result["valid"] is False
        assert any("ohm_url" in e["message"] for e in result["errors"])

    def test_type_mismatch_fails(self):
        """read_only must be a boolean, not a string."""
        bad = {**_VALID_PROFILE, "read_only": "yes"}
        result = validate_profiles_payload([bad])
        assert result["valid"] is False
        assert any(e["validator"] == "type" for e in result["errors"])

    def test_additional_property_rejected(self):
        """The schema uses additionalProperties:false — unknown fields fail."""
        bad = {**_VALID_PROFILE, "unknown_field": "value"}
        result = validate_profiles_payload([bad])
        assert result["valid"] is False
        assert any("additional" in e["message"] or "unknown_field" in e["message"] for e in result["errors"])

    def test_per_profile_indexing_in_errors(self):
        """When two profiles are passed and the second is bad, the error points at [1]."""
        result = validate_profiles_payload([_VALID_PROFILE, {"ohm_url": "http://x"}])
        assert result["valid"] is False
        assert result["errors"][0]["profile_index"] == 1
        assert "[1]" in result["errors"][0]["path"]

    def test_non_object_profile_fails(self):
        """A non-dict entry in the profiles array fails cleanly."""
        result = validate_profiles_payload([_VALID_PROFILE, "not-a-profile"])
        assert result["valid"] is False
        assert any(e.get("profile_index") == 1 for e in result["errors"])

    def test_non_list_non_dict_payload_fails(self):
        """A bare string or number is rejected."""
        result = validate_profiles_payload("not-json-object")
        assert result["valid"] is False
        assert len(result["errors"]) >= 1


class TestReservedNamespaceWarnings:
    """Sidecar namespaces colliding with reserved core prefixes are warned."""

    def test_ohm_prefix_namespace_warns(self):
        profile = {
            **_VALID_PROFILE,
            "sidecars": [{"name": "ops", "type": "sse", "namespace": "ohm_admin", "url": "http://x"}],
        }
        result = validate_profiles_payload([profile])
        # Schema passes (string is valid); warning fires for reserved prefix
        assert result["valid"] is True
        assert len(result["warnings"]) >= 1
        assert any("ohm_" in w["message"] for w in result["warnings"])

    def test_admin_prefix_namespace_warns(self):
        profile = {
            **_VALID_PROFILE,
            "sidecars": [{"name": "ops", "type": "sse", "namespace": "admin_tools", "url": "http://x"}],
        }
        result = validate_profiles_payload([profile])
        assert len(result["warnings"]) >= 1
        assert any("admin_" in w["message"] for w in result["warnings"])

    def test_non_reserved_namespace_no_warning(self):
        profile = {
            **_VALID_PROFILE,
            "sidecars": [{"name": "ops", "type": "sse", "namespace": "trading", "url": "http://x"}],
        }
        result = validate_profiles_payload([profile])
        assert result["warnings"] == []

    def test_reserved_prefixes_frozenset_contents(self):
        assert "ohm_" in RESERVED_TOOL_PREFIXES
        assert "admin_" in RESERVED_TOOL_PREFIXES


class TestValidateProfilesFile:
    """File-loading wrapper — adds file path and line info to errors."""

    def test_valid_file_passes(self, tmp_path):
        path = tmp_path / "profiles.json"
        path.write_text(json.dumps([_VALID_PROFILE]), encoding="utf-8")
        result = validate_profiles_file(str(path))
        assert result["valid"] is True
        assert result["profile_count"] == 1

    def test_missing_file_fails_with_path(self, tmp_path):
        result = validate_profiles_file(str(tmp_path / "nonexistent.json"))
        assert result["valid"] is False
        assert any("not found" in e["message"] for e in result["errors"])
        assert all(e.get("file") for e in result["errors"])

    def test_malformed_json_fails_with_line_column(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{\n  \"api_key\": \"k\",\n  \"ohm_url\":", encoding="utf-8")
        result = validate_profiles_file(str(path))
        assert result["valid"] is False
        err = result["errors"][0]
        assert "malformed JSON" in err["message"]
        assert err.get("line") is not None
        assert err.get("column") is not None
        assert err.get("file") == str(path)

    def test_file_errors_include_file_path(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps([{"ohm_url": "http://x"}]), encoding="utf-8")
        result = validate_profiles_file(str(path))
        assert result["valid"] is False
        assert all(e.get("file") == str(path) for e in result["errors"])


class TestValidateProfilesInline:
    """Inline OHM_GATEWAY_PROFILE env var form."""

    def test_valid_inline_passes(self):
        result = validate_profiles_inline(json.dumps([_VALID_PROFILE]))
        assert result["valid"] is True
        assert result["profile_count"] == 1

    def test_malformed_inline_fails(self):
        result = validate_profiles_inline("{not valid json")
        assert result["valid"] is False
        assert any("OHM_GATEWAY_PROFILE" in e.get("source", "") for e in result["errors"])

    def test_inline_errors_include_source(self):
        result = validate_profiles_inline(json.dumps([{"ohm_url": "http://x"}]))
        assert result["valid"] is False
        assert all(e.get("source") == "OHM_GATEWAY_PROFILE" for e in result["errors"])


class TestFormatValidationReport:
    """Human-readable report formatting."""

    def test_valid_report_says_ok(self):
        result = {"valid": True, "errors": [], "warnings": [], "profile_count": 2}
        report = format_validation_report(result)
        assert "OK" in report or "no errors" in report
        assert "2" in report

    def test_error_report_includes_path_and_message(self):
        result = {
            "valid": False,
            "errors": [{"path": "[0].api_key", "message": "is a required property", "validator": "required", "file": "profiles.json"}],
            "warnings": [],
            "profile_count": 1,
        }
        report = format_validation_report(result)
        assert "profiles.json" in report
        assert "[0].api_key" in report
        assert "is a required property" in report
        assert "fix:" in report.lower()

    def test_warning_report_includes_warning_marker(self):
        result = {
            "valid": True,
            "errors": [],
            "warnings": [{"path": "[0].sidecars[0].namespace", "message": "collides with reserved prefix", "file": "profiles.json"}],
            "profile_count": 1,
        }
        report = format_validation_report(result)
        assert "⚠" in report
        assert "collides" in report

    def test_summary_line_counts_errors_and_warnings(self):
        result = {
            "valid": False,
            "errors": [{"path": "$", "message": "e1"}, {"path": "$", "message": "e2"}],
            "warnings": [{"path": "$", "message": "w1"}],
            "profile_count": 1,
        }
        report = format_validation_report(result)
        assert "2 error" in report
        assert "1 warning" in report


class TestSchemaCoverage:
    """Confirm schema fields match the implemented GatewayProfile dataclass."""

    def test_schema_covers_all_implemented_fields(self):
        """Every field on GatewayProfile must appear in the schema."""
        try:
            from ohm.mcp.gateway import GatewayProfile  # noqa: F401
        except ImportError:
            pytest.skip("fastmcp not installed — gateway module unavailable")
        import dataclasses

        from ohm.mcp.profile_validation import _PROFILE_SCHEMA_PATH

        with open(_PROFILE_SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        schema_fields = set(schema["properties"].keys())
        dataclass_fields = {f.name for f in dataclasses.fields(GatewayProfile)}
        # Every dataclass field must be in the schema
        missing = dataclass_fields - schema_fields
        assert not missing, f"GatewayProfile fields missing from schema: {missing}"