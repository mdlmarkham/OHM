from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ohm.exceptions import ValidationError

_SCHEMA_PATH = Path(__file__).parent / "data" / "odps_v4.1_schema.json"
BOS_VISIBILITY = frozenset({"private", "organisation"})


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _parse_yaml(text: str) -> dict[str, Any]:
    doc = yaml.safe_load(text)
    if doc is None:
        raise ValueError("ODPS document is empty")
    if not isinstance(doc, dict):
        raise ValueError(f"ODPS document must be a mapping, got {type(doc).__name__}")
    return doc


def _err(path: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"path": path, "message": message, **extra}


def validate_odps(document: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Validate against the canonical ODPS v4.1 JSON schema.

    Returns ``{"valid", "errors", "compliance_level"}``. Never raises on
    schema/YAML failures; raises ``ValidationError`` only on missing/corrupt
    vendored schema (deployment fault).
    """
    if not _SCHEMA_PATH.exists():
        raise ValidationError(f"ODPS schema not found at {_SCHEMA_PATH}")
    if isinstance(document, (bytes, bytearray)):
        document = document.decode("utf-8")
    if isinstance(document, str):
        try:
            doc = _parse_yaml(document)
        except yaml.YAMLError as e:
            return {"valid": False, "errors": [_err("$", f"Malformed YAML: {e}")], "compliance_level": None}
        except ValueError as e:
            return {"valid": False, "errors": [_err("$", str(e))], "compliance_level": None}
    else:
        doc = document

    v = _validator()
    errors = [
        _err(
            ".".join(str(p) for p in e.absolute_path) or "$",
            e.message,
            validator=e.validator,
            schema_path=".".join(str(p) for p in e.schema_path) or "$",
        )
        for e in sorted(v.iter_errors(doc), key=lambda x: list(x.absolute_path))
    ]
    return {
        "valid": not errors,
        "errors": errors,
        "compliance_level": _compliance_level(doc) if not errors else None,
    }


def validate_bos(document: str | bytes | dict[str, Any], *, producer_agent: str | None) -> dict[str, Any]:
    """BOS-specific constraints layered on top of ODPS (ADR-027).

    Checks:
      - ``producer_agent`` is required (non-empty)
      - ``visibility`` must be ``private`` or ``organisation``
      - ``dataAccess[].format`` and ``dataAccess[].specification`` must be ``MCP``
    """
    if isinstance(document, (bytes, bytearray)):
        document = document.decode("utf-8")
    doc = _parse_yaml(document) if isinstance(document, str) else document
    errors: list[dict[str, Any]] = []

    if not producer_agent or not str(producer_agent).strip():
        errors.append(_err("producer_agent", "producer_agent is required for BOS registration"))

    details = (doc.get("product") or {}).get("details") or {}
    for lang, block in details.items():
        vis = block.get("visibility")
        if vis and vis not in BOS_VISIBILITY:
            errors.append(
                _err(
                    f"product.details.{lang}.visibility",
                    f"visibility must be one of {sorted(BOS_VISIBILITY)} for BOS, got '{vis}'",
                    validator="bos-visibility",
                )
            )

    for i, item in enumerate((doc.get("product") or {}).get("dataAccess") or []):
        for fld in ("format", "specification"):
            val = item.get(fld)
            if val is not None and val != "MCP":
                errors.append(
                    _err(
                        f"product.dataAccess[{i}].{fld}",
                        f"BOS requires {fld} 'MCP', got '{val}'",
                        validator=f"bos-{fld}",
                    )
                )

    return {"valid": not errors, "errors": errors}


def validate_registration(
    document: str | bytes | dict[str, Any], *, producer_agent: str | None
) -> dict[str, Any]:
    """Full registration gate: ODPS schema + BOS constraints.

    Returns ``{"valid", "errors", "odps_valid", "bos_valid", "compliance_level"}``.
    Raises ``ValidationError`` only on deployment faults (missing schema).
    """
    odps = validate_odps(document)
    bos = validate_bos(document, producer_agent=producer_agent)
    errs = odps["errors"] + bos["errors"]
    return {
        "valid": not errs,
        "errors": errs,
        "odps_valid": odps["valid"],
        "bos_valid": bos["valid"],
        "compliance_level": odps["compliance_level"],
    }


def _compliance_level(doc: dict[str, Any]) -> str:
    blocks = (
        "contract",
        "SLA",
        "dataQuality",
        "pricingPlans",
        "license",
        "dataAccess",
        "dataHolder",
        "paymentGateways",
        "productStrategy",
    )
    present = sum(1 for b in blocks if (doc.get("product") or {}).get(b))
    if present <= 2:
        return "minimal"
    if present <= 5:
        return "basic"
    if present <= 7:
        return "substantial"
    return "full"
