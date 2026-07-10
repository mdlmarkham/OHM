"""Regression tests for SDK X-Tenant-ID behavior (ADR-043)."""

import json
from unittest import mock

import pytest

from ohm.framework.sdk import connect_http


def _capture_request_headers(graph):
    """Run graph.stats() and return the urllib Request object's headers."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["url"] = req.full_url

        class FakeResp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        return FakeResp()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        graph.stats()
    return captured["headers"]


class TestTenantHeader:
    """Customer API keys must not send X-Tenant-ID; agent tokens should."""

    def test_customer_token_omits_tenant_header(self):
        graph = connect_http(
            "http://127.0.0.1:8710",
            actor="metis",
            token="ohm-cust-devops-abc123",
            tenant_id="devops",
        )
        headers = _capture_request_headers(graph)
        assert headers["authorization"] == "Bearer ohm-cust-devops-abc123"
        assert "x-tenant-id" not in headers

    def test_agent_token_includes_tenant_header(self):
        graph = connect_http(
            "http://127.0.0.1:8710",
            actor="metis",
            token="agent-token-for-devops",
            tenant_id="devops",
        )
        headers = _capture_request_headers(graph)
        assert headers["authorization"] == "Bearer agent-token-for-devops"
        assert headers.get("x-tenant-id") == "devops"

    def test_explicit_customer_token_type_omits_header(self):
        graph = connect_http(
            "http://127.0.0.1:8710",
            actor="metis",
            token="not-a-prefix-but-still-customer",
            tenant_id="devops",
            token_type="customer",
        )
        headers = _capture_request_headers(graph)
        assert "x-tenant-id" not in headers

    def test_explicit_agent_token_type_includes_header(self):
        graph = connect_http(
            "http://127.0.0.1:8710",
            actor="metis",
            token="not-a-prefix-but-agent",
            tenant_id="devops",
            token_type="agent",
        )
        headers = _capture_request_headers(graph)
        assert headers.get("x-tenant-id") == "devops"
