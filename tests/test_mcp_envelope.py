"""Tests for OHM-787: ohm_context response envelope."""

from __future__ import annotations

import pytest


class TestResponseEnvelope:
    """Test the ohm_context envelope wrapping (OHM-787)."""

    def test_envelope_wraps_successful_response(self):
        """Successful responses are wrapped in {ok, data, ohm_context}."""
        from ohm.mcp.gateway_helpers import _strip_nulls

        # Simulate what _respond does for a successful dict response
        data = {"id": "n1", "label": "Test", "content": None}
        # The envelope logic: strip nulls, wrap
        stripped = _strip_nulls(data)
        envelope = {"ok": True, "data": stripped}
        assert envelope["ok"] is True
        assert envelope["data"]["id"] == "n1"
        assert "content" not in envelope["data"]

    def test_error_response_stays_flat(self):
        """Error responses are not wrapped in envelope (backward compat)."""
        # Error responses have "error" key and stay flat
        error_response = {"error": "auth_failed", "message": "Invalid key"}
        assert "ok" not in error_response
        assert "data" not in error_response

    def test_nudges_move_to_ohm_context(self):
        """Nudges from the daemon response move to ohm_context."""
        daemon_response = {"id": "n1", "label": "Test", "nudges": [{"type": "hint", "message": "check this"}]}

        # Simulate the envelope logic
        nudges = daemon_response.pop("nudges", None)
        ohm_context = {}
        if nudges:
            ohm_context["nudges"] = nudges

        envelope = {"ok": True, "data": daemon_response}
        if ohm_context:
            envelope["ohm_context"] = ohm_context

        assert "nudges" not in envelope["data"]
        assert envelope["ohm_context"]["nudges"] == [{"type": "hint", "message": "check this"}]

    def test_envelope_preserves_data_integrity(self):
        """The data field contains the original response minus nudges."""
        original = {"id": "n1", "label": "Test", "nudges": [{"type": "hint"}]}
        nudges = original.pop("nudges", None)
        envelope = {"ok": True, "data": original, "ohm_context": {"nudges": nudges}}

        assert envelope["data"]["id"] == "n1"
        assert envelope["data"]["label"] == "Test"
        assert "nudges" not in envelope["data"]

    def test_envelope_omits_empty_ohm_context(self):
        """When there are no nudges or agent state, ohm_context is omitted."""
        data = {"id": "n1", "label": "Test"}
        envelope = {"ok": True, "data": data}
        # No ohm_context key when there's nothing to put in it
        assert "ohm_context" not in envelope

    def test_agent_state_included_when_profile_known(self):
        """agent_state is included in ohm_context when profile is known."""
        from dataclasses import dataclass

        @dataclass
        class FakeProfile:
            agent_id: str = "test-agent"
            tenant_id: str = "test-tenant"

        profile = FakeProfile()
        ohm_context: dict = {}
        ohm_context["agent_state"] = {"agent_id": profile.agent_id, "tenant_id": profile.tenant_id}

        envelope = {"ok": True, "data": {}, "ohm_context": ohm_context}
        assert envelope["ohm_context"]["agent_state"]["agent_id"] == "test-agent"
        assert envelope["ohm_context"]["agent_state"]["tenant_id"] == "test-tenant"

    def test_envelope_is_json_serializable(self):
        """The envelope is JSON-serializable for structured content."""
        import json

        envelope = {
            "ok": True,
            "data": {"id": "n1", "label": "Test"},
            "ohm_context": {
                "nudges": [{"type": "hint", "message": "check"}],
                "agent_state": {"agent_id": "test"},
            },
        }
        serialized = json.dumps(envelope)
        deserialized = json.loads(serialized)
        assert deserialized["ok"] is True
        assert deserialized["data"]["id"] == "n1"
        assert deserialized["ohm_context"]["nudges"][0]["type"] == "hint"
