"""Shared typed base for Graph mixins (mirrors OhmHandlerBase server-side)."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class GraphMixinBase:
    """Typed base class for all Graph mixins. Do not instantiate directly."""
    _conn: "DuckDBPyConnection"
    actor: str
    token: str | None
    tenant_id: str | None
    _signing_key: bytes | None
