"""Input validation and sanitization for OHM.

All user-provided values that go into SQL queries must pass through
these validators. DuckDB supports parameterized queries for VALUES
but not for identifiers in CTE anchor clauses, so we validate
identifiers before interpolation.
"""

from __future__ import annotations

import re

# Identifiers: alphanumeric, underscore, hyphen, dot (for compound IDs)
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# Layer values: exactly L1, L2, L3, L4
_LAYER_RE = re.compile(r"^L[1-4]$")

# ISO timestamp: basic format check
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")


def validate_identifier(value: str, *, name: str = "value") -> str:
    """Validate that *value* is a safe SQL identifier.

    Returns *value* unchanged if valid.

    Raises:
        ValueError: If *value* contains unsafe characters.
    """
    if not value or not _IDENTIFIER_RE.match(value):
        raise ValueError(
            f"Invalid {name}: '{value}' — must contain only "
            f"alphanumeric characters, underscores, hyphens, and dots"
        )
    return value


def validate_layer(value: str) -> str:
    """Validate that *value* is a valid layer (L1-L4)."""
    if not _LAYER_RE.match(value):
        raise ValueError(
            f"Invalid layer: '{value}' — must be L1, L2, L3, or L4"
        )
    return value


def validate_timestamp(value: str) -> str:
    """Validate that *value* looks like an ISO timestamp."""
    if not _ISO_TS_RE.match(value):
        raise ValueError(
            f"Invalid timestamp: '{value}' — expected ISO format "
            f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)"
        )
    return value


def validate_confidence(value: float) -> float:
    """Validate that *value* is a confidence score in [0, 1]."""
    if not (0.0 <= value <= 1.0):
        raise ValueError(
            f"Invalid confidence: {value} — must be between 0.0 and 1.0"
        )
    return value


def validate_depth(value: int, *, max_depth: int = 20) -> int:
    """Validate that *value* is a reasonable traversal depth."""
    if not (1 <= value <= max_depth):
        raise ValueError(
            f"Invalid depth: {value} — must be between 1 and {max_depth}"
        )
    return value
