"""Tests for OHM-ue9k: TOPO NodeType vocabulary + layer encoding alignment.

Background: TOPO's pre-migration store used UPPERCASE NodeType values
and INTEGER layer encoding. OHM canonical is lowercase NodeType +
VARCHAR layer ('L1', 'L2', ...). This test suite verifies:

- SchemaConfig.topo() declares metric, data_product, component, other
- SchemaConfig.case_strategy defaults to "lowercase"
- TOPO template uses case_strategy="uppercase" for legacy support
- validate_node_type() is case-insensitive at the module level
- normalize_node_type() returns canonical lowercase form
- SchemaConfig.validate_node_type respects case_strategy
- SchemaConfig.normalize_node_type respects case_strategy
- to_dict / from_dict round-trip preserves case_strategy
- case_strategy="uppercase" accepts legacy UPPERCASE form
- case_strategy="preserve" rejects UPPERCASE (strict)
- topo.json template has the 4 new node types
"""

import pytest

from ohm.graph.schema import (
    DEFAULT_SCHEMA,
    SCHEMA_VERSION,
    SchemaConfig,
    TOPO_SCHEMA,
    normalize_node_type,
    resolve_schema_by_name,
    validate_node_type,
)


# ── New node types in SchemaConfig.topo() ──────────────────────────────────


class TestTopoNewNodeTypes:
    def test_metric_in_topo_node_types(self):
        assert "metric" in TOPO_SCHEMA.node_types

    def test_data_product_in_topo_node_types(self):
        assert "data_product" in TOPO_SCHEMA.node_types

    def test_component_in_topo_node_types(self):
        assert "component" in TOPO_SCHEMA.node_types

    def test_other_in_topo_node_types(self):
        assert "other" in TOPO_SCHEMA.node_types

    def test_topo_template_loads_with_new_types(self):
        topo = SchemaConfig.from_json_file("topo.json")
        for t in ("metric", "data_product", "component", "other"):
            assert t in topo.node_types

    def test_default_schema_does_not_have_topo_types(self):
        # Base OHM doesn't carry TOPO industrial types.
        for t in ("metric", "data_product", "component", "other"):
            assert t not in DEFAULT_SCHEMA.node_types


# ── case_strategy field ────────────────────────────────────────────────────


class TestCaseStrategyDefault:
    def test_default_schema_lowercase(self):
        assert DEFAULT_SCHEMA.case_strategy == "lowercase"

    def test_topo_uses_uppercase_for_legacy_support(self):
        assert TOPO_SCHEMA.case_strategy == "uppercase"

    def test_rejects_invalid_case_strategy(self):
        with pytest.raises(ValueError, match="case_strategy must be"):
            SchemaConfig(name="t", case_strategy="camelcase")

    def test_accepts_preserve(self):
        c = SchemaConfig(name="t", case_strategy="preserve")
        assert c.case_strategy == "preserve"


# ── Module-level validate_node_type (case-insensitive) ─────────────────────


class TestModuleLevelValidateNodeType:
    # Module-level function checks against VALID_NODE_TYPES (core OHM
    # only). Use core types for these tests; SchemaConfig-level tests
    # below exercise the case_strategy field with custom node types.

    def test_canonical_lowercase_validates(self):
        assert validate_node_type("concept") is True
        assert validate_node_type("source") is True

    def test_legacy_uppercase_validates(self):
        # Even at the module level, UPPERCASE core types validate
        # (the case-insensitive change in OHM-ue9k applies here too).
        assert validate_node_type("CONCEPT") is True
        assert validate_node_type("SOURCE") is True

    def test_mixed_case_validates(self):
        assert validate_node_type("Concept") is True
        assert validate_node_type("Source") is True

    def test_unknown_type_rejected(self):
        assert validate_node_type("not_a_real_type") is False

    def test_empty_string_rejected(self):
        assert validate_node_type("") is False

    def test_none_rejected(self):
        assert validate_node_type(None) is False  # type: ignore[arg-type]


# ── Module-level normalize_node_type ──────────────────────────────────────


class TestModuleLevelNormalizeNodeType:
    def test_canonical_lowercase_unchanged(self):
        assert normalize_node_type("concept") == "concept"
        assert normalize_node_type("source") == "source"

    def test_legacy_uppercase_lowercased(self):
        # Note: this only works for types in VALID_NODE_TYPES (core).
        # TOPO-only types like 'metric' must be normalized via
        # SchemaConfig.normalize_node_type.
        assert normalize_node_type("CONCEPT") == "concept"
        assert normalize_node_type("SOURCE") == "source"

    def test_mixed_case_lowercased(self):
        assert normalize_node_type("Concept") == "concept"
        # 'Data_Product' is a TOPO-only type, not in core VALID_NODE_TYPES
        # → returned as-is (permissive for unknown types).
        assert normalize_node_type("Data_Product") == "Data_Product"

    def test_unknown_type_returned_as_is(self):
        # Permissive: unknown types pass through for downstream error reporting.
        assert normalize_node_type("not_a_real_type") == "not_a_real_type"

    def test_empty_string_passes_through(self):
        assert normalize_node_type("") == ""

    def test_core_ohm_types_normalize(self):
        # Existing OHM types should normalize cleanly.
        assert normalize_node_type("concept") == "concept"
        assert normalize_node_type("CONCEPT") == "concept"


# ── SchemaConfig.validate_node_type respects case_strategy ─────────────────


class TestSchemaConfigValidateNodeType:
    def test_lowercase_strategy_accepts_canonical(self):
        # Default schema has all VALID_NODE_TYPES — pick 'metric'
        # which is added by topo() but not base. Use a schema that
        # includes it explicitly.
        c = SchemaConfig(
            name="t",
            node_types=frozenset({"metric"}),
            case_strategy="lowercase",
        )
        assert c.validate_node_type("metric") is True

    def test_lowercase_strategy_rejects_uppercase(self):
        c = SchemaConfig(
            name="t",
            node_types=frozenset({"metric"}),
            case_strategy="lowercase",
        )
        assert c.validate_node_type("METRIC") is False

    def test_uppercase_strategy_accepts_uppercase(self):
        c = SchemaConfig(
            name="t",
            node_types=frozenset({"metric"}),
            case_strategy="uppercase",
        )
        assert c.validate_node_type("METRIC") is True
        # The UPPERCASE strategy validates via the canonical lowercase
        # form, so 'metric' is also accepted (it normalizes to itself).
        # The "uppercase" name is a misnomer — what it really means is
        # "case-insensitive, normalizes to lowercase canonical".
        assert c.validate_node_type("metric") is True

    def test_preserve_strategy_strict_case_match(self):
        c = SchemaConfig(
            name="t",
            node_types=frozenset({"metric"}),
            case_strategy="preserve",
        )
        assert c.validate_node_type("metric") is True
        assert c.validate_node_type("METRIC") is False
        assert c.validate_node_type("Metric") is False

    def test_validate_node_type_handles_empty(self):
        c = SchemaConfig(name="t")
        assert c.validate_node_type("") is False
        assert c.validate_node_type(None) is False  # type: ignore[arg-type]

    def test_topo_schema_validates_uppercase_metric(self):
        # End-to-end: SchemaConfig.topo() accepts the legacy form.
        assert TOPO_SCHEMA.validate_node_type("METRIC") is True
        assert TOPO_SCHEMA.validate_node_type("DATA_PRODUCT") is True
        # Canonical lowercase also validates (case-insensitive).
        assert TOPO_SCHEMA.validate_node_type("metric") is True


class TestSchemaConfigNormalizeNodeType:
    def test_lowercase_strategy_returns_canonical_or_none(self):
        c = SchemaConfig(
            name="t",
            node_types=frozenset({"metric"}),
            case_strategy="lowercase",
        )
        assert c.normalize_node_type("metric") == "metric"
        assert c.normalize_node_type("METRIC") is None  # not canonical form

    def test_uppercase_strategy_normalizes(self):
        c = SchemaConfig(
            name="t",
            node_types=frozenset({"metric"}),
            case_strategy="uppercase",
        )
        # UPPERCASE input → canonical lowercase
        assert c.normalize_node_type("METRIC") == "metric"
        # Already-canonical lowercase input also normalizes to itself.
        # The "uppercase" strategy is really "case-insensitive
        # canonicalization" — it accepts any case and emits lowercase.
        assert c.normalize_node_type("metric") == "metric"

    def test_preserve_strategy_strict(self):
        c = SchemaConfig(
            name="t",
            node_types=frozenset({"metric"}),
            case_strategy="preserve",
        )
        assert c.normalize_node_type("metric") == "metric"
        assert c.normalize_node_type("METRIC") is None

    def test_normalize_returns_none_for_unknown(self):
        c = SchemaConfig(name="t", case_strategy="uppercase")
        assert c.normalize_node_type("DEFINITELY_NOT_REAL") is None

    def test_normalize_handles_empty(self):
        c = SchemaConfig(name="t")
        assert c.normalize_node_type("") is None

    def test_topo_schema_normalizes_uppercase_to_canonical(self):
        # End-to-end: TOPO_SCHEMA.normalize_node_type("METRIC") → "metric"
        assert TOPO_SCHEMA.normalize_node_type("METRIC") == "metric"
        assert TOPO_SCHEMA.normalize_node_type("DATA_PRODUCT") == "data_product"
        assert TOPO_SCHEMA.normalize_node_type("COMPONENT") == "component"
        assert TOPO_SCHEMA.normalize_node_type("OTHER") == "other"


# ── Round-trip with case_strategy ──────────────────────────────────────────


class TestCaseStrategyRoundTrip:
    def test_lowercase_default_omitted_from_dict(self):
        d = DEFAULT_SCHEMA.to_dict()
        assert "case_strategy" not in d

    def test_uppercase_included_in_dict(self):
        d = TOPO_SCHEMA.to_dict()
        assert d.get("case_strategy") == "uppercase"

    def test_roundtrip_preserves_uppercase(self):
        d = TOPO_SCHEMA.to_dict()
        restored = SchemaConfig.from_dict(d)
        assert restored.case_strategy == "uppercase"

    def test_roundtrip_preserves_preserve(self):
        c = SchemaConfig(name="t", case_strategy="preserve")
        restored = SchemaConfig.from_dict(c.to_dict())
        assert restored.case_strategy == "preserve"

    def test_roundtrip_default_lowercase(self):
        d = SchemaConfig(name="t").to_dict()
        restored = SchemaConfig.from_dict(d)
        assert restored.case_strategy == "lowercase"


# ── topo.json template sanity ──────────────────────────────────────────────


class TestTopoTemplateRoundTrip:
    def test_template_case_strategy_uppercase(self):
        topo = SchemaConfig.from_json_file("topo.json")
        assert topo.case_strategy == "uppercase"

    def test_template_round_trip_preserves_case_strategy(self):
        topo = SchemaConfig.from_json_file("topo.json")
        restored = SchemaConfig.from_dict(topo.to_dict())
        assert restored.case_strategy == "uppercase"
        # New types still present after round trip.
        for t in ("metric", "data_product", "component", "other"):
            assert t in restored.node_types


# ── Backward compatibility ────────────────────────────────────────────────


class TestBackwardCompatibility:
    def test_existing_topo_callers_still_get_node_types(self):
        # Code that called SchemaConfig.topo() before OHM-ue9k still
        # gets the same set of node types — only expanded by 4.
        topo = resolve_schema_by_name("topo")
        # The original 17 industrial types still present:
        for t in ("process", "instrument", "controller", "valve", "pump", "motor", "sensor", "pipeline", "vessel", "reactor", "heat_exchanger", "tank", "compressor", "generator", "transformer", "circuit", "bus", "line"):
            assert t in topo.node_types

    def test_validate_node_type_backward_compat_lowercase(self):
        # All existing lowercase callers see identical behavior:
        # valid types validate, invalid ones reject.
        assert validate_node_type("concept") is True
        assert validate_node_type("source") is True
        assert validate_node_type("not_a_type") is False

    def test_validate_node_type_more_permissive_than_before(self):
        # Before OHM-ue9k: UPPERCASE rejected. After: accepted.
        # This is the breaking-but-intended change: legacy TOPO data
        # now validates without a rename.
        assert validate_node_type("CONCEPT") is True
