"""Content encoding helpers for OHM MCP transport.

Provides optional TOON encoding for tool results to reduce token usage
when the consuming agent requests it. JSON remains the default.
"""

from __future__ import annotations

import json
from typing import Any

# TOON is an optional dependency. If unavailable, TOON format requests fall
# back to JSON gracefully.
try:
    from toon import decode as _toon_decode  # type: ignore[import]
    from toon import encode as _toon_encode  # type: ignore[import]

    _TOON_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TOON_AVAILABLE = False
    _toon_encode = None  # type: ignore[assignment]
    _toon_decode = None  # type: ignore[assignment]


DEFAULT_FORMAT = "json"
TOON_MIME_TYPE = "text/toon"
JSON_MIME_TYPE = "application/json"


def _format_from_mime(mime: str | None) -> str:
    """Resolve requested format from an HTTP-style Accept header value."""
    if mime is None:
        return DEFAULT_FORMAT
    mime = mime.lower().split(";")[0].strip()
    if mime in (TOON_MIME_TYPE, "application/toon"):
        return "toon"
    return "json"


def _format_from_argument(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Resolve requested format from MCP tool arguments, removing it from the dict.

    Returns (format, was_explicit). The format is explicit when the caller
    supplied a valid `format` argument; this lets an explicit `json` beat
    a transport-level `Accept: text/toon` header.
    """
    fmt = arguments.pop("format", None) if isinstance(arguments, dict) else None
    if fmt in ("toon", TOON_MIME_TYPE, "application/toon"):
        return "toon", True
    if fmt in ("json", JSON_MIME_TYPE, "application/json"):
        return "json", True
    return DEFAULT_FORMAT, False


def requested_format(arguments: dict[str, Any], accept: str | None = None) -> str:
    """Determine the response format requested by the agent.

    Precedence:
      1. `format` field in tool arguments (e.g. `format: "toon"`).
      2. `Accept` header / MIME type passed from the transport.
      3. Default JSON.
    """
    fmt, explicit = _format_from_argument(arguments)
    if explicit:
        return fmt
    return _format_from_mime(accept)


def encode_payload(data: Any, fmt: str = DEFAULT_FORMAT) -> str:
    """Encode a Python object as JSON or TOON text.

    Falls back to JSON if TOON is unavailable or encoding fails.
    """
    if fmt == "toon" and _TOON_AVAILABLE and _toon_encode is not None:
        try:
            return _toon_encode(data)
        except Exception:
            # TOON is most efficient for uniform arrays of objects; non-uniform
            # or deeply nested payloads may fail. Fall back to JSON silently.
            pass
    return json.dumps(data, indent=2, default=str)


def decode_payload(text: str, fmt: str = DEFAULT_FORMAT) -> Any:
    """Decode JSON or TOON text back to a Python object."""
    if fmt == "toon" and _TOON_AVAILABLE and _toon_decode is not None:
        return _toon_decode(text)
    return json.loads(text)


def format_supported(fmt: str) -> bool:
    """Return True if the requested format is supported by this build."""
    if fmt == "toon":
        return _TOON_AVAILABLE
    return fmt in ("json", "application/json")
