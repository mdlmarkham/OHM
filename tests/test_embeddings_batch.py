"""Test embedding backfill with batch_size and delay_ms controls (OHM-emb fix).

Verifies that:
1. Batch size limits how many nodes are processed per call
2. Remaining count is reported for pagination
3. Delay_ms parameter is accepted without error
4. Re-calling when all embeddings exist returns early with "no work" response
5. Invalid batch_size values are clamped
"""

import json
import http.client
import socket

import pytest


def _ohmd_running(host: str = "127.0.0.1", port: int = 8710) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=0.5)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _ohmd_running(),
    reason="ohmd not running on 127.0.0.1:8710 — integration test requires a live server",
)


def _request(method, path, token="ohm-test-token"):
    """Make HTTP request to ohmd test instance."""
    conn = http.client.HTTPConnection("127.0.0.1", 8710, timeout=30)
    headers = {"Authorization": f"Bearer {token}"}
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    body = json.loads(resp.read().decode())
    conn.close()
    return resp.status, body


def test_embeddings_batch_size():
    """Embedding backfill respects batch_size limit."""
    # Use batch_size=1 to process just one node at a time
    status, body = _request("GET", "/admin/embeddings?batch_size=1&delay_ms=0")
    # Should succeed (either ok or partial)
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert "updated" in body or "remaining" in body, f"Unexpected response: {body}"
    # If there were nodes to process, processed should be <= 1
    if body.get("processed", 0) > 0:
        assert body["processed"] <= 1, f"batch_size=1 should process at most 1 node, got {body['processed']}"


def test_embeddings_default_batch():
    """Default batch_size processes up to 5 nodes."""
    status, body = _request("GET", "/admin/embeddings?delay_ms=0")
    assert status == 200, f"Expected 200, got {status}: {body}"
    # processed should be <= 5 (default batch_size)
    assert body.get("processed", 0) <= 5, f"Default batch should be 5, got {body}"


def test_embeddings_all_done():
    """When all nodes have embeddings, returns 'no work' response."""
    # First, fill all embeddings
    _request("GET", "/admin/embeddings?batch_size=50&delay_ms=0")
    # Then call again — should report 0 remaining
    status, body = _request("GET", "/admin/embeddings")
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert body.get("remaining", -1) == 0, f"Expected 0 remaining, got: {body}"


def test_embeddings_large_batch_clamped():
    """Batch size > 50 is clamped to 50."""
    status, body = _request("GET", "/admin/embeddings?batch_size=999&delay_ms=0")
    assert status == 200, f"Expected 200, got {status}: {body}"
    # Should process at most 50 (clamped)
    assert body.get("processed", 0) <= 50, f"batch_size=999 should be clamped to 50, got {body}"


def test_embeddings_response_fields():
    """Response includes all expected fields."""
    status, body = _request("GET", "/admin/embeddings?batch_size=2&delay_ms=0")
    assert status == 200, f"Expected 200, got {status}: {body}"
    for field in ["status", "updated", "failed", "processed", "total", "remaining", "message"]:
        assert field in body, f"Missing field '{field}' in response: {body}"


if __name__ == "__main__":
    import sys

    print("Testing embedding batch controls...")
    for name, fn in [
        ("batch_size=1", test_embeddings_batch_size),
        ("default_batch", test_embeddings_default_batch),
        ("large_batch_clamped", test_embeddings_large_batch_clamped),
        ("response_fields", test_embeddings_response_fields),
    ]:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            sys.exit(1)
    # Don't run test_embeddings_all_done by default — it's slow
    print("All tests passed!")
