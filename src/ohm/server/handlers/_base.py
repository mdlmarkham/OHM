"""Typed request-handler base for all OHM HTTP handler mixins.

This module declares the shared attributes and helper methods that every
handler mixin relies on. Declaring them on a common base lets mypy stop
treating ``self.current_store``, ``self.body``, etc. as untyped ``Any``
attributes, and removes the bulk of ``attr-defined`` false positives in
``src/ohm/server/``.

The concrete values are still supplied at runtime by the configured
``OhmHandler`` class in ``server.py``.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ohm.schema import SchemaConfig
    from ohm.tenant import TenantManager

    try:
        from ohm.graph.store import OhmStore
    except ImportError:
        OhmStore = Any  # type: ignore[misc,assignment]


class OhmHandlerBase(BaseHTTPRequestHandler):
    """Typed base class for all OHM HTTP handler mixins.

    Do not instantiate directly. The real request handler is built in
    ``ohm.server.server`` by mixing these modules on top of this base.
    """

    # ── Class-level configuration (set by the server factory) ────────────────
    store: Optional["OhmStore"] = None
    tenant_manager: Optional["TenantManager"] = None
    config: dict[str, Any] = {}
    tokens: dict[str, str] = {}
    customer_tokens: dict[str, str] = {}
    roles: dict[str, str] = {}
    no_auth: bool = False
    require_read_auth: bool = False
    schema_config: "SchemaConfig" = None  # type: ignore[assignment]
    multi_tenant: bool = False
    TRUSTED_PROXIES: frozenset[str] = frozenset()
    _write_lock: threading.RLock = threading.RLock()

    # ── Dispatch tables (populated after class body) ────────────────────────
    _GET_EXACT: dict[str, str] = {}
    _GET_PREFIXES: list[tuple[str, str]] = []
    _POST_EXACT: dict[str, str] = {}
    _POST_PREFIXES: list[tuple[str, str]] = []
    _DELETE_PREFIXES: list[tuple[str, str]] = []

    # ── Per-request state (populated during dispatch) ───────────────────────
    body: dict[str, Any] | bytes | None = None
    _resolved_customer_id: Optional[str] = None

    # ── Common helpers used by handler mixins ───────────────────────────────
    # These are implemented on OhmHandler in server.py; declaring their
    # signatures here removes mypy attr-defined errors and documents the
    # contract for each mixin.

    @property
    def current_store(self) -> "OhmStore":
        """Return the active store for the current request."""
        raise NotImplementedError("must be provided by OhmHandler")

    @property
    def _customer_id(self) -> Optional[str]:
        """Return the tenant/customer id for the current request, if any."""
        return None

    def _authenticate(self) -> Optional[str]:
        """Validate the request Authorization header and return agent name."""
        return None

    def _extract_body(self) -> dict[str, Any] | bytes | None:
        """Parse the request body as JSON or return raw bytes."""
        return None

    def _json_response(self, status: int, body: Any) -> None:
        """Serialize ``body`` as JSON and send it with the given HTTP status."""
        raise NotImplementedError("must be provided by OhmHandler")

    def _respond_text(self, status: int, text: str, content_type: str = "text/plain") -> None:
        """Send a plain-text response."""
        raise NotImplementedError("must be provided by OhmHandler")

    def _respond_error(self, status: int, message: str) -> None:
        """Send an error response and finish the request."""
        raise NotImplementedError("must be provided by OhmHandler")

    def _require_admin(self, action: str = "this admin endpoint") -> str:
        """Authenticate and require admin role.

        Returns the authenticated agent name, or raises
        ``PermissionDeniedError`` / ``AuthenticationError``. *action* only
        phrases the error message. Concrete handlers must override this
        stub with the same signature.
        """
        raise NotImplementedError("must be provided by OhmHandler")

    def _run_in_write_lock(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute ``fn`` while holding the instance write lock."""
        return fn(*args, **kwargs)

    def _log_request(self, method: str, path: str, status: int, elapsed_ms: float) -> None:
        """Emit a structured access log entry."""
        pass


__all__ = ["OhmHandlerBase"]
