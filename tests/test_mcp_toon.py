"""Tests for optional TOON encoding in the OHM MCP server."""

from __future__ import annotations

import json
import pytest

from ohm.mcp.encoding import (
    DEFAULT_FORMAT,
    TOON_MIME_TYPE,
    decode_payload,
    encode_payload,
    format_supported,
    requested_format,
)


def sample_payload():
    return {
        "nodes": [
            {"id": "n1", "label": "A", "type": "concept", "confidence": 0.8},
            {"id": "n2", "label": "B", "type": "source", "confidence": 0.9},
        ],
        "count": 2,
    }


def test_requested_format_defaults_to_json():
    assert requested_format({}) == DEFAULT_FORMAT
    assert requested_format({"q": "foo"}) == DEFAULT_FORMAT


def test_requested_format_from_argument():
    assert requested_format({"format": "toon"}) == "toon"
    assert requested_format({"format": TOON_MIME_TYPE}) == "toon"
    # format is consumed (popped) so it is not forwarded to OHM
    args = {"q": "foo", "format": "toon"}
    assert requested_format(args) == "toon"
    assert "format" not in args


def test_requested_format_from_accept_header():
    assert requested_format({}, accept=TOON_MIME_TYPE) == "toon"
    assert requested_format({}, accept="application/toon") == "toon"
    assert requested_format({}, accept="application/json") == DEFAULT_FORMAT
    assert requested_format({}, accept="text/plain") == DEFAULT_FORMAT


def test_requested_format_argument_beats_accept():
    args = {"format": "json"}
    assert requested_format(args, accept=TOON_MIME_TYPE) == DEFAULT_FORMAT


def test_encode_payload_json():
    payload = sample_payload()
    text = encode_payload(payload, "json")
    assert json.loads(text) == payload


def test_encode_payload_toon_roundtrip():
    payload = sample_payload()
    text = encode_payload(payload, "toon")
    # TOON text should be more compact than JSON for this uniform array
    json_text = json.dumps(payload, indent=2)
    assert len(text) < len(json_text)
    assert decode_payload(text, "toon") == payload


def test_format_supported():
    assert format_supported("json") is True
    assert format_supported("toon") is True
    assert format_supported("xml") is False


def test_encode_payload_falls_back_to_json_for_toon_if_unavailable(monkeypatch):
    # Simulate TOON library missing
    monkeypatch.setattr("ohm.mcp.encoding._TOON_AVAILABLE", False)
    monkeypatch.setattr("ohm.mcp.encoding._toon_encode", None)
    payload = sample_payload()
    text = encode_payload(payload, "toon")
    assert json.loads(text) == payload


def test_decode_payload_falls_back_to_json_for_toon_if_unavailable(monkeypatch):
    monkeypatch.setattr("ohm.mcp.encoding._TOON_AVAILABLE", False)
    monkeypatch.setattr("ohm.mcp.encoding._toon_decode", None)
    payload = sample_payload()
    json_text = json.dumps(payload)
    assert decode_payload(json_text, "toon") == payload


def test_text_content_uses_format():
    from ohm.mcp.server import _text

    payload = sample_payload()
    json_content = _text(payload, "json")
    toon_content = _text(payload, "toon")
    assert json.loads(json_content[0].text) == payload
    assert decode_payload(toon_content[0].text, "toon") == payload
