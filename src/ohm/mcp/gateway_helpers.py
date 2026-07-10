"""Helpers for the MCP gateway — kept separate from gateway.py so they can
be tested without the ``fastmcp`` dependency (OHM-747).

Contains:
- ``_strip_nulls``: recursively drop None-valued keys from write responses
- ``_deduplicate_nudges``: per-session nudge deduplication
"""

from __future__ import annotations

from typing import Any

# Per-session nudge dedup — tracks which batch_suggestion types have already
# been shown to a given agent/session.
_SESSION_NUDGES_SEEN: dict[str, set[str]] = {}


def _strip_nulls(obj: Any) -> Any:
    """Recursively drop keys whose value is None from dicts.

    OHM-747-2: Write responses include many null-valued keys (optional
    fields the caller didn't set). Stripping them reduces token usage
    for MCP clients (especially LLM agents parsing tool responses).
    """
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(item) for item in obj]
    return obj


def _deduplicate_nudges(session_key: str, payload: Any) -> Any:
    """Strip batch_suggestion nudges already shown this session (OHM-747-3).

    Each nudge type appears at most once per session. The first occurrence
    passes through; subsequent ones are removed.
    """
    if not isinstance(payload, dict):
        return payload
    nudges = payload.get("nudges")
    if not nudges or not isinstance(nudges, list):
        return payload
    seen = _SESSION_NUDGES_SEEN.setdefault(session_key, set())
    filtered = []
    for nudge in nudges:
        if not isinstance(nudge, dict):
            filtered.append(nudge)
            continue
        nudge_type = nudge.get("type", nudge.get("nudge_type", ""))
        if nudge_type and nudge_type in seen:
            continue
        if nudge_type:
            seen.add(nudge_type)
        filtered.append(nudge)
    payload = {**payload, "nudges": filtered}
    return payload
