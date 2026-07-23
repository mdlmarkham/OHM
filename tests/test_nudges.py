"""Tests for OHM-973: tool-call syntax leakage detection in node content.

When a malformed/truncated tool call is persisted verbatim into a node's
``content`` field, the write response should carry an advisory warning nudge
(``content_tool_call_leak``) so the caller can review and clean the content.
Detection lives in ``ohm.server.nudges._detect_tool_call_leak`` and is wired
into ``generate_nudges`` for ``action="node"``.
"""

from __future__ import annotations

import pytest

from ohm.server.nudges import _detect_tool_call_leak, generate_nudges


# ── Unit tests: _detect_tool_call_leak ──────────────────────────────────────


class TestDetectToolCallLeak:
    def test_detects_parameter_name_fragment(self):
        nudge = _detect_tool_call_leak('Some decision text <parameter name="create_only">false"')
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"
        assert nudge["severity"] == "warning"
        assert "tool-call" in nudge["message"]
        assert "snippet" in nudge["data"]

    def test_detects_parameter_name_fragment_without_trailing_quote(self):
        nudge = _detect_tool_call_leak('<parameter name="create_only">false')
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"

    def test_detects_tool_calls_json_envelope(self):
        nudge = _detect_tool_call_leak('Calling {"tool_calls": [{"id": "1", "function":')
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"

    def test_detects_tool_name_json_envelope(self):
        nudge = _detect_tool_call_leak('{"tool_name": "create_node", "arguments":')
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"

    def test_detects_trailing_unbalanced_quote(self):
        nudge = _detect_tool_call_leak('Normal text ending in a stray "')
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"

    def test_detects_trailing_orphan_open_brace(self):
        nudge = _detect_tool_call_leak("some truncated json {")
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"

    def test_detects_trailing_unbalanced_close_brace(self):
        nudge = _detect_tool_call_leak('truncated payload {"a": 1}}')
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"

    def test_detects_trailing_bare_literal_after_gt(self):
        nudge = _detect_tool_call_leak('<parameter name="x">false')
        assert nudge is not None
        assert nudge["type"] == "content_tool_call_leak"

    def test_no_false_positive_normal_prose(self):
        assert _detect_tool_call_leak("The AND→OR refactor enables cheaper retries. See ADR-0001.") is None

    def test_no_false_positive_json_midstream(self):
        assert _detect_tool_call_leak('Config: {"name": "foo", "threshold": 0.5} applied here.') is None

    def test_no_false_positive_generic_name_key(self):
        assert _detect_tool_call_leak('{"name": "widget", "version": 2} is the config.') is None

    def test_no_false_positive_code_snippet(self):
        assert _detect_tool_call_leak('```python\nx = {"a": 1}\n```') is None

    def test_no_false_positive_balanced_json_at_end(self):
        assert _detect_tool_call_leak('Config: {"a": 1}') is None

    def test_no_false_positive_closing_quote_on_word(self):
        assert _detect_tool_call_leak('He said "hello."') is None

    def test_no_false_positive_prose_ending_in_false(self):
        assert _detect_tool_call_leak("This statement is false.") is None

    def test_handles_none_content(self):
        assert _detect_tool_call_leak(None) is None

    def test_handles_empty_content(self):
        assert _detect_tool_call_leak("") is None

    def test_handles_whitespace_only_content(self):
        assert _detect_tool_call_leak("   \n  ") is None

    def test_handles_non_string_content(self):
        assert _detect_tool_call_leak({"not": "a string"}) is None
        assert _detect_tool_call_leak(12345) is None

    def test_snippet_is_truncated_to_last_80_chars(self):
        long_prefix = "x" * 200
        suffix = ' <parameter name="y">true"'
        content = long_prefix + suffix
        nudge = _detect_tool_call_leak(content)
        assert nudge is not None
        snippet = nudge["data"]["snippet"]
        assert snippet == content[-80:]
        assert len(snippet) == 80
        assert snippet.endswith(suffix)

    def test_snippet_is_full_content_when_short(self):
        content = '<parameter name="z">null'
        nudge = _detect_tool_call_leak(content)
        assert nudge is not None
        assert nudge["data"]["snippet"] == content


# ── Unit tests: generate_nudges wiring ──────────────────────────────────────


class TestGenerateNudgesWiring:
    def test_node_write_with_leak_appends_warning_nudge(self):
        node = {
            "id": "n1",
            "label": "Bad content",
            "type": "concept",
            "content": 'Decision <parameter name="create_only">false"',
        }
        nudges = generate_nudges("node", node_id="n1", node=node)
        leaks = [n for n in nudges if n["type"] == "content_tool_call_leak"]
        assert len(leaks) == 1
        assert leaks[0]["severity"] == "warning"

    def test_node_write_with_clean_content_no_leak_nudge(self):
        node = {
            "id": "n2",
            "label": "Good content",
            "type": "concept",
            "content": "The AND→OR refactor enables cheaper retries.",
        }
        nudges = generate_nudges("node", node_id="n2", node=node)
        leaks = [n for n in nudges if n["type"] == "content_tool_call_leak"]
        assert leaks == []

    def test_edge_action_does_not_run_leak_detector(self):
        # Leaked content only matters for node writes; edges don't carry content.
        nudges = generate_nudges("edge", edge_type="CAUSES")
        leaks = [n for n in nudges if n["type"] == "content_tool_call_leak"]
        assert leaks == []

    def test_node_with_none_content_no_leak_nudge(self):
        node = {"id": "n3", "label": "No content", "type": "concept", "content": None}
        nudges = generate_nudges("node", node_id="n3", node=node)
        leaks = [n for n in nudges if n["type"] == "content_tool_call_leak"]
        assert leaks == []

    def test_node_without_node_kwarg_no_crash(self):
        nudges = generate_nudges("node", node_id="n4")
        leaks = [n for n in nudges if n["type"] == "content_tool_call_leak"]
        assert leaks == []


# ── Integration tests: HTTP POST /node surfaces the nudge ────────────────────


class TestHttpPostNodeLeakNudge:
    def test_post_node_with_leaked_content_surfaces_nudge(self, test_server):
        from tests.conftest import _request

        port, _store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "leak_node_1",
                "label": "Leaked",
                "type": "concept",
                "content": 'Some decision text <parameter name="create_only">false"',
            },
        )
        assert status == 201, f"expected 201, got {status}: {data}"
        nudges = data.get("nudges", []) if isinstance(data, dict) else []
        leaks = [n for n in nudges if n.get("type") == "content_tool_call_leak"]
        assert leaks, f"expected content_tool_call_leak nudge, got: {[n.get('type') for n in nudges]}"
        assert leaks[0]["severity"] == "warning"

    def test_post_node_with_clean_content_no_leak_nudge(self, test_server):
        from tests.conftest import _request

        port, _store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "clean_node_1",
                "label": "Clean",
                "type": "concept",
                "content": "The AND→OR refactor enables cheaper retries. See ADR-0001.",
            },
        )
        assert status == 201, f"expected 201, got {status}: {data}"
        nudges = data.get("nudges", []) if isinstance(data, dict) else []
        leaks = [n for n in nudges if n.get("type") == "content_tool_call_leak"]
        assert leaks == [], f"unexpected content_tool_call_leak nudge on clean content: {leaks}"

    def test_post_node_with_trailing_stray_quote_surfaces_nudge(self, test_server):
        from tests.conftest import _request

        port, _store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "leak_node_2",
                "label": "Stray quote",
                "type": "concept",
                "content": 'Normal text ending in a stray "',
            },
        )
        assert status == 201, f"expected 201, got {status}: {data}"
        nudges = data.get("nudges", []) if isinstance(data, dict) else []
        leaks = [n for n in nudges if n.get("type") == "content_tool_call_leak"]
        assert leaks, f"expected content_tool_call_leak nudge for trailing quote, got: {[n.get('type') for n in nudges]}"
