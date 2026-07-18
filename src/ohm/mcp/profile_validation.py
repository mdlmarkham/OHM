"""Gateway profile JSON Schema validation (OHM-912).

Kept separate from ``gateway.py`` so it can be imported and exercised
without the optional ``fastmcp``/``httpx`` dependencies — only the core
``jsonschema`` dependency (already required by ``ohm``) is needed. This
makes ``ohm-gateway --validate-config`` work on any install, and lets unit
tests run in the default CI matrix.

The validation surface mirrors ``src/ohm/bos/odps_validation.py``: a
vendored JSON Schema file, an ``lru_cache`` validator factory, and a
``validate_gateway_profiles`` function that returns
``{"valid", "errors", "warnings"}`` rather than raising on user input.
``ValidationError`` is raised only for deployment faults (missing schema
file).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from ohm.exceptions import ValidationError

_SCHEMA_DIR = Path(__file__).parent / "schemas"
_PROFILE_SCHEMA_PATH = _SCHEMA_DIR / "gateway-profile.schema.json"
_SIDECAR_SCHEMA_PATH = _SCHEMA_DIR / "sidecar-manifest.schema.json"

# Tool-name prefixes that sidecar namespaces must not collide with.
# Adding a sidecar with one of these as its namespace would shadow core
# tools and break the gateway contract.
RESERVED_TOOL_PREFIXES: frozenset[str] = frozenset({"ohm_", "admin_"})


@lru_cache(maxsize=1)
def _registry() -> Registry:
    """Build a referencing Registry that resolves the sidecar schema $ref."""
    profile_schema = json.loads(_PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
    sidecar_schema = json.loads(_SIDECAR_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Registry().with_resource(
        "sidecar-manifest.schema.json",
        Resource.from_contents(sidecar_schema),
    ).with_resource(
        profile_schema["$id"],
        Resource.from_contents(profile_schema),
    ).with_resource(
        sidecar_schema["$id"],
        Resource.from_contents(sidecar_schema),
    )


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(_PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def _err(path: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"path": path, "message": message, **extra}


def _check_reserved_namespaces(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sidecar namespaces must not collide with reserved core prefixes."""
    warnings: list[dict[str, Any]] = []
    for i, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            continue
        sidecars = profile.get("sidecars") or []
        for j, sidecar in enumerate(sidecars):
            ns = sidecar.get("namespace") or ""
            for prefix in RESERVED_TOOL_PREFIXES:
                if ns == prefix or ns.startswith(prefix):
                    warnings.append(
                        _err(
                            f"[{i}].sidecars[{j}].namespace",
                            f"sidecar namespace '{ns}' collides with reserved core prefix '{prefix}' — choose a different namespace (e.g. 'ops_{ns}')",
                            validator="reserved-namespace",
                            profile_index=i,
                        )
                    )
    return warnings


def _normalize_raw(raw: Any) -> list[dict[str, Any]]:
    """Normalize the parsed JSON into a list of profile dicts."""
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return raw
    raise ValueError(f"gateway profiles must be an array or a single object, got {type(raw).__name__}")


def validate_profiles_payload(raw: Any) -> dict[str, Any]:
    """Validate a parsed profiles payload against the gateway-profile schema.

    Args:
        raw: Either a list of profile dicts or a single profile dict.

    Returns:
        ``{"valid": bool, "errors": list, "warnings": list, "profile_count": int}``.
        Never raises on schema failures — only raises ``ValidationError`` if
        the vendored schema file itself is missing/corrupt (deployment fault).
    """
    if not _PROFILE_SCHEMA_PATH.exists():
        raise ValidationError(f"Gateway profile schema not found at {_PROFILE_SCHEMA_PATH}")

    try:
        profiles = _normalize_raw(raw)
    except ValueError as e:
        return {
            "valid": False,
            "errors": [_err("$", str(e))],
            "warnings": [],
            "profile_count": 0,
        }

    v = _validator()
    errors: list[dict[str, Any]] = []
    for idx, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            errors.append(
                _err(
                    f"[{idx}]",
                    f"profile must be an object, got {type(profile).__name__}",
                    validator="type",
                    profile_index=idx,
                )
            )
            continue
        for e in sorted(v.iter_errors(profile), key=lambda x: list(x.absolute_path)):
            path = ".".join(str(p) for p in e.absolute_path) or "$"
            path = f"[{idx}].{path}" if path != "$" else f"[{idx}]"
            errors.append(
                _err(
                    path,
                    e.message,
                    validator=e.validator,
                    schema_path=".".join(str(p) for p in e.schema_path) or "$",
                    profile_index=idx,
                )
            )

    warnings = _check_reserved_namespaces(profiles)

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "profile_count": len(profiles),
    }


def validate_profiles_file(path: str) -> dict[str, Any]:
    """Load and validate a gateway-profiles JSON file.

    Args:
        path: Filesystem path to the JSON file.

    Returns:
        Same shape as ``validate_profiles_payload``, with each error enriched
        with the source ``file`` path for actionable CLI output.
    """
    if not os.path.exists(path):
        return {
            "valid": False,
            "errors": [_err("$", f"file not found: {path}", file=path)],
            "warnings": [],
            "profile_count": 0,
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        return {
            "valid": False,
            "errors": [_err("$", f"malformed JSON at line {e.lineno} column {e.colno}: {e.msg}", file=path, line=e.lineno, column=e.colno)],
            "warnings": [],
            "profile_count": 0,
        }

    result = validate_profiles_payload(raw)
    # Attach the file path to each error so the CLI can report it
    for err in result["errors"]:
        err.setdefault("file", path)
    for warn in result["warnings"]:
        warn.setdefault("file", path)
    return result


def validate_profiles_inline(inline_json: str) -> dict[str, Any]:
    """Validate the inline ``OHM_GATEWAY_PROFILE`` env var form."""
    try:
        raw = json.loads(inline_json)
    except json.JSONDecodeError as e:
        return {
            "valid": False,
            "errors": [_err("$", f"malformed JSON in OHM_GATEWAY_PROFILE at line {e.lineno} column {e.colno}: {e.msg}", source="OHM_GATEWAY_PROFILE", line=e.lineno, column=e.colno)],
            "warnings": [],
            "profile_count": 0,
        }
    result = validate_profiles_payload(raw)
    for err in result["errors"]:
        err.setdefault("source", "OHM_GATEWAY_PROFILE")
    for warn in result["warnings"]:
        warn.setdefault("source", "OHM_GATEWAY_PROFILE")
    return result


def format_validation_report(result: dict[str, Any]) -> str:
    """Format a validation result as a human-readable, actionable string.

    Mirrors the illustrative CLI output in issue #912: file:line, plain
    language, and a suggested fix where applicable.
    """
    lines: list[str] = []
    errors = result.get("errors", [])
    warnings = result.get("warnings", [])
    for err in errors:
        loc = err.get("file") or err.get("source") or "<profiles>"
        path = err.get("path", "$")
        msg = err.get("message", "")
        line = err.get("line")
        col = err.get("column")
        position = f":{line}:{col}" if line is not None else ""
        lines.append(f"✗ {loc}{position} — {path}: {msg}")
        # Suggested fixes for common mistakes
        if err.get("validator") == "required":
            lines.append(f"    fix: add the missing required field to this profile")
        elif err.get("validator") == "type":
            lines.append(f"    fix: check the field type against the schema (expected {err.get('schema_path', '?')})")
        elif err.get("validator") == "format" and "uri" in msg.lower():
            lines.append(f"    fix: ohm_url must be a full URL including scheme (e.g. http://127.0.0.1:8710)")
        elif err.get("validator") == "reserved-namespace":
            lines.append(f"    fix: choose a namespace that does not start with {sorted(RESERVED_TOOL_PREFIXES)}")
    for warn in warnings:
        loc = warn.get("file") or warn.get("source") or "<profiles>"
        path = warn.get("path", "$")
        msg = warn.get("message", "")
        lines.append(f"⚠ {loc} — {path}: {msg}")
    summary = f"{len(errors)} error(s), {len(warnings)} warning(s)"
    if not errors and not warnings:
        summary = f"{result.get('profile_count', 0)} profile(s) OK — no errors or warnings"
    lines.append(summary)
    return "\n".join(lines)


__all__ = [
    "RESERVED_TOOL_PREFIXES",
    "validate_profiles_payload",
    "validate_profiles_file",
    "validate_profiles_inline",
    "format_validation_report",
]