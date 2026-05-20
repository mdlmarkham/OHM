"""Tests for the OHM input validation module (SQL injection prevention)."""

import pytest

from ohm.validation import (
    validate_identifier,
    validate_layer,
    validate_timestamp,
    validate_confidence,
    validate_depth,
    validate_pert_triple,
)


class TestValidateIdentifier:
    """Tests for validate_identifier — SQL injection prevention."""

    def test_valid_simple(self):
        assert validate_identifier("node_1") == "node_1"

    def test_valid_with_hyphen(self):
        assert validate_identifier("my-node") == "my-node"

    def test_valid_with_dot(self):
        assert validate_identifier("schema.table") == "schema.table"

    def test_valid_alphanumeric(self):
        assert validate_identifier("abc123") == "abc123"

    def test_valid_underscore(self):
        assert validate_identifier("_private") == "_private"

    def test_valid_complex(self):
        assert validate_identifier("my_node.v2-final") == "my_node.v2-final"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("has space")

    def test_rejects_sql_injection_drop(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("'; DROP TABLE ohm_nodes; --")

    def test_rejects_sql_injection_union(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("' UNION SELECT * FROM ohm_nodes --")

    def test_rejects_semicolon(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("node;drop")

    def test_rejects_single_quote(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("node'or")

    def test_rejects_double_quote(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier('node"or')

    def test_rejects_parentheses(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("node()")

    def test_rejects_equals(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("node=1")

    def test_rejects_newline(self):
        with pytest.raises(ValueError, match="Invalid value"):
            validate_identifier("node\ninjection")

    def test_custom_name_in_error(self):
        with pytest.raises(ValueError, match="Invalid node_id"):
            validate_identifier("bad id", name="node_id")


class TestValidateLayer:
    """Tests for validate_layer."""

    def test_valid_l1(self):
        assert validate_layer("L1") == "L1"

    def test_valid_l2(self):
        assert validate_layer("L2") == "L2"

    def test_valid_l3(self):
        assert validate_layer("L3") == "L3"

    def test_valid_l4(self):
        assert validate_layer("L4") == "L4"

    def test_rejects_l0(self):
        with pytest.raises(ValueError, match="Invalid layer"):
            validate_layer("L0")

    def test_rejects_l5(self):
        with pytest.raises(ValueError, match="Invalid layer"):
            validate_layer("L5")

    def test_rejects_lowercase(self):
        with pytest.raises(ValueError, match="Invalid layer"):
            validate_layer("l1")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid layer"):
            validate_layer("")

    def test_rejects_sql_injection(self):
        with pytest.raises(ValueError, match="Invalid layer"):
            validate_layer("L1'; DROP TABLE ohm_nodes;--")


class TestValidateTimestamp:
    """Tests for validate_timestamp."""

    def test_valid_date_only(self):
        assert validate_timestamp("2026-05-19") == "2026-05-19"

    def test_valid_datetime_with_t(self):
        assert validate_timestamp("2026-05-19T14:30:00") == "2026-05-19T14:30:00"

    def test_valid_datetime_with_space(self):
        assert validate_timestamp("2026-05-19 14:30:00") == "2026-05-19 14:30:00"

    def test_valid_datetime_with_millis(self):
        assert validate_timestamp("2026-05-19T14:30:00.123") == "2026-05-19T14:30:00.123"

    def test_valid_datetime_with_z(self):
        assert validate_timestamp("2026-05-19T14:30:00Z") == "2026-05-19T14:30:00Z"

    def test_valid_datetime_with_offset(self):
        assert validate_timestamp("2026-05-19T14:30:00+05:30") == "2026-05-19T14:30:00+05:30"

    def test_valid_datetime_with_offset_no_colon(self):
        assert validate_timestamp("2026-05-19T14:30:00+0530") == "2026-05-19T14:30:00+0530"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid timestamp"):
            validate_timestamp("")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError, match="Invalid timestamp"):
            validate_timestamp("not-a-date")

    def test_rejects_sql_injection(self):
        with pytest.raises(ValueError, match="Invalid timestamp"):
            validate_timestamp("2026-05-19'; DROP TABLE ohm_nodes;--")

    def test_rejects_partial_date(self):
        with pytest.raises(ValueError, match="Invalid timestamp"):
            validate_timestamp("2026-05")


class TestValidateConfidence:
    """Tests for validate_confidence."""

    def test_valid_zero(self):
        assert validate_confidence(0.0) == 0.0

    def test_valid_one(self):
        assert validate_confidence(1.0) == 1.0

    def test_valid_half(self):
        assert validate_confidence(0.5) == 0.5

    def test_valid_high(self):
        assert validate_confidence(0.94) == 0.94

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="Invalid confidence"):
            validate_confidence(-0.1)

    def test_rejects_above_one(self):
        with pytest.raises(ValueError, match="Invalid confidence"):
            validate_confidence(1.1)

    def test_rejects_large_negative(self):
        with pytest.raises(ValueError, match="Invalid confidence"):
            validate_confidence(-100.0)

    def test_rejects_large_positive(self):
        with pytest.raises(ValueError, match="Invalid confidence"):
            validate_confidence(100.0)


class TestValidateDepth:
    """Tests for validate_depth."""

    def test_valid_one(self):
        assert validate_depth(1) == 1

    def test_valid_five(self):
        assert validate_depth(5) == 5

    def test_valid_twenty(self):
        assert validate_depth(20) == 20

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="Invalid depth"):
            validate_depth(0)

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="Invalid depth"):
            validate_depth(-1)

    def test_rejects_above_max(self):
        with pytest.raises(ValueError, match="Invalid depth"):
            validate_depth(21)

    def test_custom_max_depth(self):
        assert validate_depth(50, max_depth=50) == 50

    def test_custom_max_depth_rejects(self):
        with pytest.raises(ValueError, match="Invalid depth"):
            validate_depth(51, max_depth=50)
