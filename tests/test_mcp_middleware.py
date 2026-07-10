"""Tests for OHM-758: FastMCP middleware components."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from ohm.mcp.gateway_helpers import _strip_nulls, _deduplicate_nudges, _reset_nudge_state
from ohm.mcp.middleware import FormatMiddleware, AuditMiddleware


class TestFormatMiddleware:
    """Test FormatMiddleware applies null-stripping and nudge dedup."""

    @pytest.mark.asyncio
    async def test_strips_nulls_from_dict_result(self):
        """Dict results get null-stripped."""
        mw = FormatMiddleware(session_key="test")

        async def call_next(ctx):
            return {"id": "n1", "label": "Test", "content": None, "url": None}

        result = await mw.on_call_tool(context=None, call_next=call_next)
        assert "content" not in result
        assert "url" not in result
        assert result["id"] == "n1"

    @pytest.mark.asyncio
    async def test_deduplicates_nudges(self):
        """Nudges are deduplicated per session."""
        _reset_nudge_state()
        mw = FormatMiddleware(session_key="dedup-test")

        async def call_next_1(ctx):
            return {"nudges": [{"type": "batch_suggestion"}]}

        result1 = await mw.on_call_tool(context=None, call_next=call_next_1)
        assert len(result1["nudges"]) == 1

        async def call_next_2(ctx):
            return {"nudges": [{"type": "batch_suggestion"}]}

        result2 = await mw.on_call_tool(context=None, call_next=call_next_2)
        assert len(result2["nudges"]) == 0

    @pytest.mark.asyncio
    async def test_string_results_pass_through(self):
        """String (TOON) results pass through unchanged."""
        mw = FormatMiddleware(session_key="test")

        async def call_next(ctx):
            return "some toon-encoded text"

        result = await mw.on_call_tool(context=None, call_next=call_next)
        assert result == "some toon-encoded text"


class TestAuditMiddleware:
    """Test AuditMiddleware writes audit records."""

    @pytest.mark.asyncio
    async def test_audit_record_written(self):
        """Audit record is written to the configured path."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            audit_path = f.name

        try:
            mw = AuditMiddleware(audit_path=audit_path)

            async def call_next(ctx):
                return {"status": "ok"}

            await mw.on_call_tool(context=None, call_next=call_next)

            with open(audit_path) as f:
                records = [json.loads(line) for line in f if line.strip()]

            assert len(records) == 1
            assert "timestamp" in records[0]
            assert "latency_ms" in records[0]
        finally:
            os.unlink(audit_path)

    @pytest.mark.asyncio
    async def test_no_audit_path_skips_writing(self):
        """When no audit path is configured, writing is skipped."""
        mw = AuditMiddleware(audit_path=None)

        async def call_next(ctx):
            return {"status": "ok"}

        result = await mw.on_call_tool(context=None, call_next=call_next)
        assert result == {"status": "ok"}
