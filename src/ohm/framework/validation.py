"""Input validation and sanitization for OHM.

All user-provided values that go into SQL queries must pass through
these validators. DuckDB supports parameterized queries for VALUES
but not for identifiers in CTE anchor clauses, so we validate
identifiers before interpolation.
"""

from __future__ import annotations

import ipaddress
import re

# Identifiers: alphanumeric, underscore, hyphen, dot (for compound IDs)
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# NAT64 well-known prefix — the low 32 bits embed an IPv4 address.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

# Layer values: exactly L1, L2, L3, L4
_LAYER_RE = re.compile(r"^L[0-4]$")

# Customer ID: alphanumeric, underscore, hyphen — NO dots or path separators
_CUSTOMER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")

# Backup ID: same safety rules as customer_id (no traversal, no path separators)
_BACKUP_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,127}$")

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


def validate_table_name(value: str, *, name: str = "table") -> str:
    """Validate that *value* is a safe bare SQL table name for interpolation.

    Unlike :func:`validate_identifier`, this rejects dots, hyphens, and
    other characters that could break out of a table-name context in an
    f-string (e.g. ``foo.bar`` or ``sys.tables``). Only a bare SQL
    identifier (``[A-Za-z_][A-Za-z0-9_]*``) is accepted.

    Returns *value* unchanged if valid; raises ``ValueError`` otherwise.
    """
    if not value or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        raise ValueError(f"Invalid {name}: '{value}' — must be a bare SQL identifier (letters, digits, underscores; must start with a letter or underscore)")
    return value


def _validate_path_safe_id(value: str, name: str, regex: re.Pattern, max_chars: int) -> str:
    """Common validator for filesystem-safe identifiers."""
    if not value:
        raise ValueError(f"Invalid {name}: empty value")
    if "\x00" in value:
        raise ValueError(f"Invalid {name}: null byte detected in '{value}'")
    if ".." in value:
        raise ValueError(f"Invalid {name}: path traversal sequence in '{value}'")
    if "/" in value or "\\" in value:
        raise ValueError(f"Invalid {name}: path separator in '{value}'")
    if not regex.match(value):
        raise ValueError(f"Invalid {name}: '{value}' — must be 3-{max_chars} chars, alphanumeric/underscore/hyphen, starting with alphanumeric")
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
    return _validate_path_safe_id(value, "customer_id", _CUSTOMER_ID_RE, 64)


def validate_backup_id(value: str) -> str:
    """Validate that *value* is a safe backup_id for filesystem path construction.

    Same rules as validate_customer_id but allows uppercase and up to 128 chars.
    Backup IDs are often timestamps or user-provided labels.

    Returns *value* unchanged if valid.

    Raises:
        ValueError: If *value* contains unsafe characters or patterns.
    """
    return _validate_path_safe_id(value, "backup_id", _BACKUP_ID_RE, 128)


def canonicalize_ip(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Collapse IPv4-mapped and NAT64 IPv6 addresses to their embedded IPv4.

    ``ipaddress`` membership tests silently return ``False`` across address
    families, so an IPv4-mapped literal like ``::ffff:169.254.169.254`` would
    slip past an IPv4-only SSRF blocklist even though the OS routes it to the
    mapped IPv4 address. SSRF guards must canonicalize each resolved address
    with this helper before testing it against their network blocklists.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return mapped
        if ip in _NAT64_PREFIX:
            return ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip


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
        raise ValueError(f"Invalid source_tier: '{value}' — must be one of: {sorted(VALID_SOURCE_TIERS)}")
    return value


def validate_data_origin(value: str | None) -> str | None:
    """Validate that *value* is a known data origin (ADR-033).

    Returns the value unchanged if it is None or a valid origin. Raises
    ValueError for unknown origin strings.
    """
    if value is None:
        return None
    from ohm.graph.schema import VALID_DATA_ORIGINS

    if value not in VALID_DATA_ORIGINS:
        raise ValueError(f"Invalid data_origin: '{value}' — must be one of: {sorted(VALID_DATA_ORIGINS)}")
    return value


def validate_emerging_concept_status(value: str | None) -> str | None:
    """Validate that *value* is a known emerging concept status."""
    if value is None:
        return None
    from ohm.graph.schema import VALID_EMERGING_CONCEPT_STATUSES

    if value not in VALID_EMERGING_CONCEPT_STATUSES:
        raise ValueError(f"Invalid emerging_concept_status: '{value}' — must be one of: {sorted(VALID_EMERGING_CONCEPT_STATUSES)}")
    return value


def validate_task_outcome(value: str | None) -> str | None:
    """Validate that *value* is a known task outcome (OHM-f5iq).

    Accepts TRUE / FALSE / AMBIGUOUS (case-insensitive) or None.
    Returns the canonical uppercase form or None.
    """
    if value is None:
        return None
    from ohm.graph.schema import VALID_TASK_OUTCOMES

    normalized = str(value).upper()
    if normalized not in VALID_TASK_OUTCOMES:
        raise ValueError(f"Invalid task outcome: '{value}' — must be one of: {sorted(VALID_TASK_OUTCOMES)}")
    return normalized


def validate_signing_algorithm(value: str | None) -> str | None:
    """Validate that *value* is a known signing algorithm."""
    if value is None:
        return None
    from ohm.graph.schema import VALID_SIGNING_ALGORITHMS

    if value not in VALID_SIGNING_ALGORITHMS:
        raise ValueError(f"Invalid signing_algorithm: '{value}' — must be one of: {sorted(VALID_SIGNING_ALGORITHMS)}")
    return value


def validate_suggestion_type(value: str | None) -> str | None:
    """Validate that *value* is a known suggestion type."""
    if value is None:
        return None
    from ohm.graph.schema import VALID_SUGGESTION_TYPES

    if value not in VALID_SUGGESTION_TYPES:
        raise ValueError(f"Invalid suggestion_type: '{value}' — must be one of: {sorted(VALID_SUGGESTION_TYPES)}")
    return value


def validate_suggestion_status(value: str | None) -> str | None:
    """Validate that *value* is a known suggestion status."""
    if value is None:
        return None
    from ohm.graph.schema import VALID_SUGGESTION_STATUSES

    if value not in VALID_SUGGESTION_STATUSES:
        raise ValueError(f"Invalid suggestion_status: '{value}' — must be one of: {sorted(VALID_SUGGESTION_STATUSES)}")
    return value


def validate_read_scope(value: dict | None) -> dict | None:
    """Validate that *value* is a well-formed read scope definition."""
    if value is None:
        return None
    from ohm.graph.schema import VALID_READ_SCOPE_DIMENSIONS

    if not isinstance(value, dict):
        raise ValueError(f"Invalid read_scope: expected dict, got {type(value).__name__}")
    invalid_keys = set(value.keys()) - VALID_READ_SCOPE_DIMENSIONS
    if invalid_keys:
        raise ValueError(f"Invalid read_scope keys: {sorted(invalid_keys)} — must be subset of: {sorted(VALID_READ_SCOPE_DIMENSIONS)}")
    for k, v in value.items():
        if not isinstance(v, list) or not all(isinstance(item, str) for item in v):
            raise ValueError(f"Invalid read_scope value for '{k}': expected list of strings")
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

        raise ValidationError(f"Invalid hd_fingerprint: expected {expected} bytes ({dimensions} bits), got {len(value)}")
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
        raise ValueError(f"Confidence {confidence} exceeds ceiling {ceiling} for source_tier '{source_tier}'")


def validate_task_status(value: str | None) -> str | None:
    """Validate that *value* is a known task status (OHM-sbtz.2).

    Valid statuses: open, in_progress, blocked, review, done, cancelled,
    proposed, committed, active, completed, failed, partial, superseded.
    Returns the value unchanged if valid. Raises ValueError for unknown values.
    None is accepted (for non-task nodes).
    """
    if value is None:
        return None
    _VALID_TASK_STATUSES = {
        "open", "in_progress", "blocked", "review", "done", "cancelled",
        "proposed", "committed", "active", "completed", "failed", "partial", "superseded",
    }
    if value not in _VALID_TASK_STATUSES:
        raise ValueError(f"Invalid task_status: '{value}' — must be one of: {sorted(_VALID_TASK_STATUSES)}")
    return value


def validate_assigned_to(value: str | None) -> str | None:
    """Validate that *value* is a plausible agent name (OHM-sbtz.2).

    Agent names must be non-empty alphanumeric strings (underscores and
    hyphens allowed). This is a format check, not an existence check
    against the agent registry (which may not be available at the store
    layer).

    Returns the value unchanged if valid. Raises ValueError for invalid values.
    None is accepted (for unassigned tasks).
    """
    if value is None:
        return None
    if not value or not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$", value):
        raise ValueError(f"Invalid assigned_to: '{value}' — must be a non-empty alphanumeric string (underscores and hyphens allowed), starting with a letter, max 64 chars")
    return value


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
