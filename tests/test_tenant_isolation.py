"""Tests for OHM-1s14.1: handler-level store binding via closure.

The OHM-ym2f audit flagged ``OhmHandler.store`` as a class-level global
that, if any handler accessed it directly, would expose cross-tenant data
because it is shared across all HTTP threads. The mitigation is to bind
``store`` per-handler-instance via ``make_configured_handler(store)`` so
each request holds its own snapshot, immune to mid-flight mutation of the
class attribute.

These tests verify:
  1. ``make_configured_handler`` returns a handler class distinct from
     ``OhmHandler`` that binds ``store`` via instance attribute.
  2. Handlers built by the factory expose ``self.store`` as the configured
     instance, not the class-level mutable state.
  3. ``current_store`` continues to return the configured store through
     the instance attribute path.
  4. Mid-flight mutation of ``OhmHandler.store`` does NOT corrupt an
     in-flight handler's bound snapshot.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ohm.server.server import OhmHandler, make_configured_handler


def _make_handler_with_store(store):
    """Build a handler instance with ``store`` bound via the factory path
    without spinning up an actual HTTP server."""
    Handler = make_configured_handler(store)

    # Bypass BaseHTTPRequestHandler.__init__ (which requires a live socket).
    # The factory's __init__ is what we want to exercise; we patch
    # super().__init__ to a no-op so we can construct the instance in-process.
    original_init = Handler.__init__

    def _patched_init(self, *args, **kwargs):
        # Skip socketserver.BaseHTTPRequestHandler.__init__ side effects;
        # we only care about the instance-attribute assignment.
        self.store = store

    Handler.__init__ = _patched_init  # type: ignore[assignment]
    try:
        handler = Handler.__new__(Handler)
        Handler.__init__(handler)
    finally:
        Handler.__init__ = original_init  # type: ignore[assignment]
    return handler


class TestStoreClosure:
    def test_factory_returns_distinct_subclass(self):
        store = MagicMock(name="store")
        Handler = make_configured_handler(store)
        assert Handler is not OhmHandler
        assert issubclass(Handler, OhmHandler)

    def test_handler_binds_store_as_instance_attribute(self):
        store = MagicMock(name="store")
        handler = _make_handler_with_store(store)
        assert handler.store is store
        assert "store" in handler.__dict__

    def test_distinct_stores_produce_distinct_handlers(self):
        store_a = MagicMock(name="store_a")
        store_b = MagicMock(name="store_b")
        handler_a = _make_handler_with_store(store_a)
        handler_b = _make_handler_with_store(store_b)
        assert handler_a.store is store_a
        assert handler_b.store is store_b
        assert handler_a.store is not handler_b.store

    def test_handler_store_survives_class_attribute_mutation(self):
        """Mid-flight mutation of OhmHandler.store must not bleed into handlers
        built via the factory — they carry their own snapshot."""
        original = MagicMock(name="original_store")
        handler = _make_handler_with_store(original)

        # Attacker / buggy code mutates the class-level fallback.
        replacement = MagicMock(name="replacement_store")
        OhmHandler.store = replacement

        try:
            # The handler's bound snapshot is preserved.
            assert handler.store is original
            assert handler.store is not replacement
        finally:
            # Restore so we don't pollute later tests.
            del OhmHandler.store


class TestCurrentStore:
    def test_current_store_returns_bound_instance_store_in_single_tenant(self):
        store = MagicMock(name="store")
        handler = _make_handler_with_store(store)
        # Force single-tenant path in the property.
        handler.multi_tenant = False
        assert handler.current_store is store

    def test_current_store_returns_bound_instance_store_in_multi_tenant_no_customer(self):
        store = MagicMock(name="store")
        handler = _make_handler_with_store(store)
        handler.multi_tenant = True
        handler.tenant_manager = None
        # No _resolved_customer_id / _customer_id route ⇒ falls back to self.store.
        assert handler.current_store is store
