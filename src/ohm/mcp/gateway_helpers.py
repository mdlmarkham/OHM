"""Helpers for the MCP gateway — kept separate from gateway.py so they can
be tested without the ``fastmcp`` dependency (OHM-747).

Contains:
- ``_strip_nulls``: recursively drop None-valued keys from write responses
- ``_deduplicate_nudges``: per-session nudge deduplication
"""

from __future__ import annotations

import time
from typing import Any

# OHM-764: Per-session nudge dedup with TTL and size bounds.
#
# Keyed on a per-session identifier (agent_id + session_id from the MCP
# context). Each session's seen-set expires after _NUDGE_TTL_SECONDS
# and the global dict is capped at _MAX_SESSIONS entries (oldest evicted).
#
# This is still in-process state (not shared across workers), but the
# TTL + size bounds prevent unbounded growth, and the per-session keying
# means a new conversation for the same agent gets a fresh nudge set.
#
# Full multi-worker consistency requires shared storage (Redis, DB) —
# tracked as a follow-up in #758 (middleware).
_NUDGE_TTL_SECONDS = 1800  # 30 minutes
_MAX_SESSIONS = 1000

# (session_key, set[str], last_access_monotonic)
_SESSION_NUDGES_SEEN: dict[str, tuple[set[str], float]] = {}


def _prune_expired() -> None:
    """Evict expired or over-capacity session entries."""
    now = time.monotonic()
    # Evict expired
    expired = [key for key, (_, ts) in _SESSION_NUDGES_SEEN.items() if now - ts > _NUDGE_TTL_SECONDS]
    for key in expired:
        del _SESSION_NUDGES_SEEN[key]
    # Evict oldest if over capacity
    if len(_SESSION_NUDGES_SEEN) > _MAX_SESSIONS:
        sorted_keys = sorted(
            _SESSION_NUDGES_SEEN.keys(),
            key=lambda k: _SESSION_NUDGES_SEEN[k][1],
        )
        excess = len(_SESSION_NUDGES_SEEN) - _MAX_SESSIONS
        for key in sorted_keys[:excess]:
            del _SESSION_NUDGES_SEEN[key]


def _reset_nudge_state() -> None:
    """Clear all nudge dedup state (for testing)."""
    _SESSION_NUDGES_SEEN.clear()


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
    """Strip nudges already shown in this session (OHM-747-3, OHM-764).

    Each nudge type appears at most once per session. The first
    occurrence passes through; subsequent ones are removed.

    Sessions expire after 30 minutes of inactivity and the global
    session dict is capped at 1000 entries to prevent unbounded growth.
    """
    if not isinstance(payload, dict):
        return payload
    nudges = payload.get("nudges")
    if not nudges or not isinstance(nudges, list):
        return payload

    _prune_expired()

    entry = _SESSION_NUDGES_SEEN.get(session_key)
    if entry is None:
        seen: set[str] = set()
    else:
        seen = entry[0]

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

    _SESSION_NUDGES_SEEN[session_key] = (seen, time.monotonic())

    # Prune again after adding — the new entry may have pushed us over capacity
    if len(_SESSION_NUDGES_SEEN) > _MAX_SESSIONS:
        _prune_expired()

    payload = {**payload, "nudges": filtered}
    return payload
