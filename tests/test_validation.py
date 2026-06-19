"""Tests for the OHM input validation module (SQL injection prevention)."""

import pytest

from ohm.validation import (
    validate_identifier,
    validate_customer_id,
    validate_layer,
    validate_timestamp,
    validate_confidence,
    validate_depth,
    validate_pert_triple,
    validate_source_tier,
    enforce_confidence_ceiling,
)


class TestValidateIdentifier:
    """Tests for validate_identifier — SQL injection prevention."""

    @pytest.mark.parametrize(
        "input,expected",
        [
            ("node_1", "node_1"),
            ("my-node", "my-node"),
            ("schema.table", "schema.table"),
            ("abc123", "abc123"),
            ("_private", "_private"),
            ("my_node.v2-final", "my_node.v2-final"),
        ],
    )
    def test_valid_identifiers(self, input, expected):
        assert validate_identifier(input) == expected

    @pytest.mark.parametrize(
        "input",
        [
            "",
            "has space",
            "'; DROP TABLE ohm_nodes; --",
            "' UNION SELECT * FROM ohm_nodes --",
            "node;drop",
            "node'or",
            'node"or',
            "node()",
            "node=1",
            "node\ninjection",
        ],
    )
    def test_invalid_identifiers(self, input):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier(input)

    def test_custom_name_in_error(self):
        with pytest.raises(ValueError, match="Invalid node_id"):
            validate_identifier("bad id", name="node_id")


class TestValidateCustomerId:
    """Tests for validate_customer_id — path traversal prevention."""

    @pytest.mark.parametrize(
        "input,expected",
        [
            ("acme_hvac", "acme_hvac"),
            ("my-shop-123", "my-shop-123"),
            ("abc", "abc"),
            ("a_b-c-1234", "a_b-c-1234"),
        ],
    )
    def test_valid_customer_ids(self, input, expected):
        assert validate_customer_id(input) == expected

    @pytest.mark.parametrize(
        "input",
        [
            "",
            "..",
            "../etc",
            "..\\windows",
            "../../etc/passwd",
            "acme/../../etc",
            "acme\\..\\..\\etc",
            "acme\0null",
            "/absolute/path",
            "\\absolute\\path",
            "a.b",
            "UPPERCASE",
            "ab",
            "x" * 65,
            "-leading-hyphen",
            "_leading_underscore",
        ],
    )
    def test_invalid_customer_ids_path_traversal(self, input):
        with pytest.raises(ValueError, match="Invalid customer_id"):
            validate_customer_id(input)

    def test_null_byte_detected(self):
        with pytest.raises(ValueError, match="null byte"):
            validate_customer_id("acme\0hvac")

    def test_path_separator_forward(self):
        with pytest.raises(ValueError, match="path separator"):
            validate_customer_id("acme/hvac")

    def test_path_separator_backward(self):
        with pytest.raises(ValueError, match="path separator"):
            validate_customer_id("acme\\hvac")

    def test_traversal_sequence(self):
        with pytest.raises(ValueError, match="path traversal"):
            validate_customer_id("../../etc")

    def test_dot_rejected(self):
        with pytest.raises(ValueError, match="Invalid customer_id"):
            validate_customer_id("acme.hvac")


class TestValidateLayer:
    """Tests for validate_layer."""

    @pytest.mark.parametrize(
        "input,expected",
        [
            ("L1", "L1"),
            ("L2", "L2"),
            ("L3", "L3"),
            ("L4", "L4"),
        ],
    )
    def test_valid_layers(self, input, expected):
        assert validate_layer(input) == expected

    @pytest.mark.parametrize(
        "input",
        [
            "L5",
            "l1",
            "",
            "L1'; DROP TABLE ohm_nodes;--",
        ],
    )
    def test_invalid_layers(self, input):
        with pytest.raises(ValueError, match="Invalid layer"):
            validate_layer(input)


class TestValidateTimestamp:
    """Tests for validate_timestamp."""

    @pytest.mark.parametrize(
        "input,expected",
        [
            ("2026-05-19", "2026-05-19"),
            ("2026-05-19T14:30:00", "2026-05-19T14:30:00"),
            ("2026-05-19 14:30:00", "2026-05-19 14:30:00"),
            ("2026-05-19T14:30:00.123", "2026-05-19T14:30:00.123"),
            ("2026-05-19T14:30:00Z", "2026-05-19T14:30:00Z"),
            ("2026-05-19T14:30:00+05:30", "2026-05-19T14:30:00+05:30"),
            ("2026-05-19T14:30:00+0530", "2026-05-19T14:30:00+0530"),
        ],
    )
    def test_valid_timestamps(self, input, expected):
        assert validate_timestamp(input) == expected

    @pytest.mark.parametrize(
        "input",
        [
            "",
            "not-a-date",
            "2026-05-19'; DROP TABLE ohm_nodes;--",
            "2026-05",
        ],
    )
    def test_invalid_timestamps(self, input):
        with pytest.raises(ValueError, match="Invalid timestamp"):
            validate_timestamp(input)


class TestValidateConfidence:
    """Tests for validate_confidence."""

    @pytest.mark.parametrize(
        "input,expected",
        [
            (0.0, 0.0),
            (1.0, 1.0),
            (0.5, 0.5),
            (0.94, 0.94),
        ],
    )
    def test_valid_confidences(self, input, expected):
        assert validate_confidence(input) == expected

    @pytest.mark.parametrize(
        "input",
        [
            -0.1,
            1.1,
            -100.0,
            100.0,
        ],
    )
    def test_invalid_confidences(self, input):
        with pytest.raises(ValueError, match="Invalid confidence"):
            validate_confidence(input)


class TestValidateDepth:
    """Tests for validate_depth."""

    @pytest.mark.parametrize(
        "input,expected",
        [
            (1, 1),
            (5, 5),
            (20, 20),
        ],
    )
    def test_valid_depths(self, input, expected):
        assert validate_depth(input) == expected

    @pytest.mark.parametrize(
        "input",
        [
            0,
            -1,
            21,
        ],
    )
    def test_invalid_depths(self, input):
        with pytest.raises(ValueError, match="Invalid depth"):
            validate_depth(input)

    def test_custom_max_depth(self):
        assert validate_depth(50, max_depth=50) == 50

    def test_custom_max_depth_rejects(self):
        with pytest.raises(ValueError, match="Invalid depth"):
            validate_depth(51, max_depth=50)


class TestValidatePERTTriple:
    """Tests for validate_pert_triple — PERT three-point estimation validation."""

    @pytest.mark.parametrize(
        "p05,p50,p95",
        [
            pytest.param(None, None, None, id="all_none"),
            pytest.param(0.2, 0.5, 0.8, id="symmetric"),
            pytest.param(0.5, 0.5, 0.5, id="equal"),
            pytest.param(None, 0.5, None, id="p50_only"),
            pytest.param(0.2, 0.5, None, id="p05_p50_only"),
            pytest.param(None, 0.5, 0.8, id="p50_p95_only"),
            pytest.param(0.0, 0.5, 1.0, id="boundary_values"),
            pytest.param(0.0, 0.0, 0.0, id="all_zeros"),
            pytest.param(1.0, 1.0, 1.0, id="all_ones"),
        ],
    )
    def test_valid_pert_triples(self, p05, p50, p95):
        validate_pert_triple(p05, p50, p95)

    @pytest.mark.parametrize(
        "p05,p50,p95,match",
        [
            pytest.param(0.6, 0.5, 0.9, "p05.*must be <= p50", id="p05_gt_p50"),
            pytest.param(0.2, 0.8, 0.5, "p50.*must be <= p95", id="p50_gt_p95"),
            pytest.param(0.2, None, 0.8, "p50 is required", id="p50_missing"),
            pytest.param(-0.1, 0.5, 0.8, "p05.*must be between", id="p05_out_of_range"),
            pytest.param(0.2, 1.5, 0.8, "p50.*must be between", id="p50_out_of_range"),
            pytest.param(0.2, 0.5, 1.5, "p95.*must be between", id="p95_out_of_range"),
        ],
    )
    def test_invalid_pert_triples(self, p05, p50, p95, match):
        with pytest.raises(ValueError, match=match):
            validate_pert_triple(p05, p50, p95)

    def test_custom_name_in_error(self):
        with pytest.raises(ValueError, match="confidence PERT"):
            validate_pert_triple(0.6, 0.5, 0.9, name="confidence PERT")


class TestValidateSourceTier:
    """Tests for validate_source_tier (ADR-028)."""

    @pytest.mark.parametrize(
        "value",
        ["raw", "unverified", "preliminary", "official", "verified"],
    )
    def test_valid_tiers(self, value):
        assert validate_source_tier(value) == value

    def test_none_passes_through(self):
        assert validate_source_tier(None) is None

    @pytest.mark.parametrize(
        "value",
        ["unknown", "primary", "PRIMARY", "Verified", "", "raw ", " raw"],
    )
    def test_invalid_tiers_raise(self, value):
        with pytest.raises(ValueError, match="Invalid source_tier"):
            validate_source_tier(value)


class TestEnforceConfidenceCeiling:
    """Tests for enforce_confidence_ceiling (ADR-028)."""

    @pytest.mark.parametrize(
        "tier,ceiling",
        [
            ("raw", 0.3),
            ("unverified", 0.5),
            ("preliminary", 0.7),
            ("official", 0.9),
            ("verified", 1.0),
        ],
    )
    def test_at_ceiling_passes(self, tier, ceiling):
        enforce_confidence_ceiling(ceiling, tier)

    def test_above_ceiling_raises(self):
        with pytest.raises(ValueError, match="exceeds ceiling"):
            enforce_confidence_ceiling(0.5, "raw")

    def test_none_tier_skips_check(self):
        enforce_confidence_ceiling(1.0, None)
        enforce_confidence_ceiling(0.0, None)

    def test_just_above_ceiling_tolerance(self):
        enforce_confidence_ceiling(0.3 + 1e-12, "raw")
