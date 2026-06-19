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
_LAYER_RE = re.compile(r"^L[0-4]$")

# Customer ID: alphanumeric, underscore, hyphen — NO dots or path separators
_CUSTOMER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")

# ISO timestamp: basic format check
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")


def validate_identifier(value: str, *, name: str = "value") -> str:
    """Validate that *value* is a safe SQL identifier.

    Returns *value* unchanged if valid.

    Raises:
        ValueError: If *value* contains unsafe characters.
    """
    if not value or not _IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid {name}: '{value}' — must contain only alphanumeric characters, underscores, hyphens, and dots")
    return value


def validate_customer_id(value: str) -> str:
    """Validate that *value* is a safe customer_id for filesystem path construction.

    Stricter than validate_identifier: no dots (path traversal via ``..``),
    no slashes, no null bytes. Must start with alphanumeric, 3-64 chars,
    lowercase alphanumeric + underscore + hyphen only.

    Returns *value* unchanged if valid.

    Raises:
        ValueError: If *value* contains unsafe characters or patterns.
    """
    if not value:
        raise ValueError("Invalid customer_id: empty value")
    if "\x00" in value:
        raise ValueError(f"Invalid customer_id: null byte detected in '{value}'")
    if ".." in value:
        raise ValueError(f"Invalid customer_id: path traversal sequence in '{value}'")
    if "/" in value or "\\" in value:
        raise ValueError(f"Invalid customer_id: path separator in '{value}'")
    if not _CUSTOMER_ID_RE.match(value):
        raise ValueError(f"Invalid customer_id: '{value}' — must be 3-64 chars, lowercase alphanumeric, underscore, or hyphen, starting with alphanumeric")
    return value


def validate_layer(value: str) -> str:
    """Validate that *value* is a valid layer (L0-L4)."""
    if not _LAYER_RE.match(value):
        raise ValueError(f"Invalid layer: '{value}' — must be L0, L1, L2, L3, or L4")
    return value


def validate_timestamp(value: str) -> str:
    """Validate that *value* looks like an ISO timestamp."""
    if not _ISO_TS_RE.match(value):
        raise ValueError(f"Invalid timestamp: '{value}' — expected ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    return value


def validate_confidence(value: float) -> float:
    """Validate that *value* is a confidence score in [0, 1]."""
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"Invalid confidence: {value} — must be between 0.0 and 1.0")
    return value


def validate_source_tier(value: str | None) -> str | None:
    """Validate that *value* is a known source tier (ADR-028).

    Returns the value unchanged if it is None or a valid tier. Raises
    ValueError for unknown tier strings.
    """
    if value is None:
        return None
    from ohm.graph.schema import VALID_SOURCE_TIERS

    if value not in VALID_SOURCE_TIERS:
        raise ValueError(
            f"Invalid source_tier: '{value}' — must be one of: {sorted(VALID_SOURCE_TIERS)}"
        )
    return value


def validate_hd_fingerprint(value: bytes | None, *, dimensions: int = 10000) -> bytes | None:
    """Validate that *value* is a bytes-like HD fingerprint of the expected size.

    The expected byte length is ``(dimensions + 7) // 8``.  For the default
    10,000-bit dimension this is 1,250 bytes.

    Returns *value* unchanged if it is None or the correct length.  Raises
    ``ValidationError`` on mismatch.
    """
    if value is None:
        return None
    expected = (dimensions + 7) // 8
    if len(value) != expected:
        from ohm.framework.exceptions import ValidationError

        raise ValidationError(
            f"Invalid hd_fingerprint: expected {expected} bytes ({dimensions} bits), got {len(value)}"
        )
    return value


def enforce_confidence_ceiling(
    confidence: float,
    source_tier: str | None,
) -> None:
    """Enforce the source-tier confidence ceiling (ADR-028).

    If source_tier is None, no ceiling is applied (legacy write paths).
    Otherwise the confidence must not exceed SOURCE_TIER_CEILINGS[tier].
    """
    if source_tier is None:
        return
    from ohm.graph.schema import SOURCE_TIER_CEILINGS

    ceiling = SOURCE_TIER_CEILINGS.get(source_tier)
    if ceiling is None:
        return
    if confidence > ceiling + 1e-9:
        raise ValueError(
            f"Confidence {confidence} exceeds ceiling {ceiling} for source_tier '{source_tier}'"
        )


def validate_depth(value: int, *, max_depth: int = 20) -> int:
    """Validate that *value* is a reasonable traversal depth."""
    if not (1 <= value <= max_depth):
        raise ValueError(f"Invalid depth: {value} — must be between 1 and {max_depth}")
    return value


def validate_pert_triple(
    p05: float | None,
    p50: float | None,
    p95: float | None,
    *,
    name: str = "PERT",
) -> None:
    """Validate PERT three-point estimation values.

    Rules:
    - If any value is provided, p50 must be provided (it's the most likely estimate)
    - All values must be in [0, 1]
    - p05 <= p50 <= p95 (optimistic <= most likely <= pessimistic)

    Raises:
        ValueError: If PERT values are invalid.
    """
    # If no values provided, nothing to validate
    if p05 is None and p50 is None and p95 is None:
        return

    # p50 is required when any PERT value is provided
    if p50 is None:
        raise ValueError(f"Invalid {name}: p50 is required when any PERT value is provided (got p05={p05}, p50={p50}, p95={p95})")

    # All values must be in [0, 1]
    for label, val in [("p05", p05), ("p50", p50), ("p95", p95)]:
        if val is not None and not (0.0 <= val <= 1.0):
            raise ValueError(f"Invalid {name} {label}: {val} — must be between 0.0 and 1.0")

    # Ordering: p05 <= p50 <= p95
    if p05 is not None and p05 > p50:
        raise ValueError(f"Invalid {name}: p05 ({p05}) must be <= p50 ({p50})")
    if p95 is not None and p50 > p95:
        raise ValueError(f"Invalid {name}: p50 ({p50}) must be <= p95 ({p95})")


def normalize_alias(label: str) -> str:
    """Normalize a node label for alias matching.

    Lowercase, strip leading/trailing whitespace, collapse internal
    whitespace to single underscores, remove punctuation (except
    hyphens between words).

    Examples:
        "Hormuz AND-Gate" → "hormuz_and-gate"
        "  Demand  Rationing  " → "demand_rationing"
        "Strait of Hormuz" → "strait_of_hormuz"
    """
    s = label.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hex digest of content for dedup detection."""
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()
