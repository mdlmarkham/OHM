"""End-to-end MCP integration test for the local daemon deployment (OHM-yzyk.1.4).

This test:
  1. Spawns a real ohmd subprocess in multi-tenant mode.
  2. Provisions a tenant and obtains a customer API key.
  3. Loads an ohm-mcp JSON config with the customer key, allowed_tools, and read_only.
  4. Calls the MCP server's list_tools() and call_tool() handlers directly,
     which make real HTTP requests to the running ohmd.

This proves the documented Copilot -> ohm-mcp -> ohmd -> tenant path works.

Note: This test exercises the source-tree ohm.mcp.server. If a stale installed
ohm package or .pyc cache is present, list_tools() may return the unfiltered
legacy tool list. Always run with PYTHONPATH pointing at the source tree.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

# This module exercises the MCP server, which requires the optional `mcp`
# package (installed via the `gateway`/`science` extras, not the base test
# install). Skip the whole module when it is absent so CI jobs that install
# only `.[dev,bayesian,markov]` skip these tests instead of erroring on the
# in-test `from ohm.mcp.server import ...` import.
pytest.importorskip("mcp")

# Spawns a real ohmd subprocess and makes real HTTP requests — same category
# as test_server.py / test_customer_auth.py, and excluded from the parallel
# "fast" pool for the same reason: it needs the process/port to itself.
pytestmark = pytest.mark.integration


def _wait_for_health(base_url: str, deadline_s: float = 30.0) -> bool:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{base_url}/health", method="GET"),
                timeout=1.0,
            ):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def _req(method: str, base_url: str, path: str, body: dict | None = None, token: str = "") -> tuple[int, dict]:
    url = f"{base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        status = resp.status
        raw = resp.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return status, payload


@pytest.fixture
def ohmd(tmp_path):
    """Spawn a multi-tenant ohmd subprocess and yield (base_url, admin_token, db_path)."""
    port = 18710  # high port to avoid collisions
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "ohm_mcp_e2e.duckdb"
    config_path = tmp_path / "ohmd.json"
    admin_token = "admin-secret-ohmmcp"
    token_hash = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()
    config = {
        "tokens": {"admin": {"hash": token_hash, "role": "admin"}},
        "host": "127.0.0.1",
        "port": port,
        "db_path": str(db_path),
        "multi_tenant": True,
    }
    config_path.write_text(json.dumps(config))

    stdout_log = tmp_path / "ohmd.stdout.log"
    stderr_log = tmp_path / "ohmd.stderr.log"
    stdout_fh = stdout_log.open("wb")
    stderr_fh = stderr_log.open("wb")

    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "ohm.server", "--config", str(config_path)],
        stdout=stdout_fh,
        stderr=stderr_fh,
        cwd=str(repo_root),
    )
    try:
        if not _wait_for_health(base_url, deadline_s=30.0):
            try:
                err = stderr_log.read_bytes()[-4000:].decode(errors="replace")
            except Exception:
                err = "<could not read stderr log>"
            pytest.fail(f"ohmd did not become healthy on {base_url}\n{err}")
        yield base_url, admin_token, db_path
    finally:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
        try:
            stdout_fh.close()
        except Exception:
            pass
        try:
            stderr_fh.close()
        except Exception:
            pass


def _provision_tenant(base_url: str, admin_token: str, customer_id: str, domain: str) -> str:
    status, data = _req(
        "POST",
        base_url,
        "/tenant/provision",
        body={"customer_id": customer_id, "domain": domain},
        token=admin_token,
    )
    assert status == 201, f"provision failed: {data}"
    token = data["token"]
    assert token.startswith("twai_live_")
    return token


def _load_mcp_config(base_url: str, token: str, tenant_id: str, domain: str) -> None:
    from ohm.mcp.config import load_config_file

    cfg = {
        "ohm_url": base_url,
        "token": token,
        "agent_id": "test-mcp-e2e",
        "tenant_id": tenant_id,
        "token_type": "customer",
        "domain_config": f"{domain}.json",
        "allowed_tools": ["ohm_stats"],
        "read_only": True,
        "transport": "stdio",
    }
    import tempfile

    cfg_path = Path(tempfile.gettempdir()) / f"mcp-e2e-{tenant_id}.json"
    cfg_path.write_text(json.dumps(cfg))
    load_config_file(str(cfg_path))


@pytest.mark.anyio
async def test_mcp_e2e_stats_and_read_only(ohmd):
    """Full Copilot -> ohm-mcp -> ohmd -> tenant integration test."""
    base_url, admin_token, _db_path = ohmd
    tenant_id = "acme_devops"
    domain = "ohm"  # use base ohm domain so /schema validation passes

    customer_token = _provision_tenant(base_url, admin_token, tenant_id, domain)
    _load_mcp_config(base_url, customer_token, tenant_id, domain)

    from ohm.mcp.server import call_tool

    # call ohm_stats — force json explicitly; DEFAULT_FORMAT is "toon" whenever
    # python-toon is installed (e.g. via the `gateway` extra this module ships
    # under), so asserting json.loads() below requires opting out of that default.
    result = await call_tool("ohm_stats", {"format": "json"})
    assert not result.isError, f"ohm_stats call errored: {result.content}"
    assert len(result.content) > 0
    first_text = result.content[0].text
    stats = json.loads(first_text)
    assert any(k in stats for k in ("total_nodes", "total_edges")), f"unexpected stats: {stats}"


@pytest.mark.anyio
async def test_mcp_e2e_read_only_blocks_writes(ohmd):
    """read_only=True blocks write-tier tools before contacting OHM."""
    base_url, admin_token, _db_path = ohmd
    tenant_id = "acme_devops2"
    domain = "ohm"

    customer_token = _provision_tenant(base_url, admin_token, tenant_id, domain)
    _load_mcp_config(base_url, customer_token, tenant_id, domain)

    from ohm.mcp.server import call_tool

    result = await call_tool("ohm_create_node", {"id": "test-node", "label": "Test", "type": "concept"})
    assert result.isError, "read_only did not block ohm_create_node"
    first_text = result.content[0].text
    payload = json.loads(first_text)
    assert payload.get("error") in ("tool_blocked", "tool_not_allowed"), f"unexpected error: {payload}"


@pytest.mark.anyio
async def test_mcp_e2e_tools_list_filtered(ohmd):
    """list_tools should respect allowed_tools and read_only."""
    base_url, admin_token, _db_path = ohmd
    tenant_id = "acme_devops3"
    domain = "ohm"

    customer_token = _provision_tenant(base_url, admin_token, tenant_id, domain)
    _load_mcp_config(base_url, customer_token, tenant_id, domain)

    from ohm.mcp.server import list_tools

    tools = await list_tools()
    tool_names = {t.name for t in tools}
    assert "ohm_stats" in tool_names, f"ohm_stats not in tools: {tool_names}"
    assert "ohm_create_node" not in tool_names, f"read_only/allowed_tools filtering failed: {tool_names}"


@pytest.mark.anyio
async def test_mcp_e2e_verify_dump_tools(ohmd):
    """--dump-tools and --verify should work without starting stdio transport."""
    base_url, admin_token, _db_path = ohmd
    tenant_id = "acme_devops4"
    domain = "ohm"

    customer_token = _provision_tenant(base_url, admin_token, tenant_id, domain)
    _load_mcp_config(base_url, customer_token, tenant_id, domain)

    from ohm.mcp.server import _dump_tools, _run_verify

    dump = await _dump_tools()
    assert dump["read_only"] is True
    assert "ohm_stats" in {t["name"] for t in dump["tools"]}
    assert all(t["allowed"] == (t["name"] == "ohm_stats") for t in dump["tools"])

    verify = await _run_verify()
    assert verify["health"] is not None
    assert verify["health"].get("status") == "ok"
    assert verify["write_probe"]["ok"] is False or verify["write_probe"]["reason"] is not None
    assert not verify["errors"]
    assert any(t == "ohm_stats" for t in verify["tools"])


@pytest.mark.anyio
async def test_mcp_e2e_shared_tool_registry(ohmd):
    """The transport-agnostic tool registry exposes the same schemas as the server."""
    from ohm.mcp.tools import all_tools
    from ohm.mcp.server import list_tools

    registry_tools = {t.name for t in all_tools()}
    server_tools = {t.name for t in await list_tools()}
    assert "ohm_inference" in registry_tools
    assert "ohm_create_node" in registry_tools
    # The server may filter based on allowed_tools, but the registry contains
    # all known tools.
    assert server_tools.issubset(registry_tools)
    assert len(registry_tools) >= 22


@pytest.mark.anyio
async def test_mcp_e2e_inference_tools(ohmd):
    """New inference tools (ohm_inference, ohm_intervene, ohm_voi, ohm_refute, ohm_discover) work end-to-end."""
    base_url, admin_token, _db_path = ohmd
    tenant_id = "acme_inference"
    domain = "ohm"

    customer_token = _provision_tenant(base_url, admin_token, tenant_id, domain)
    _load_mcp_config(base_url, customer_token, tenant_id, domain)

    from ohm.mcp.config import config as mcp_config

    mcp_config["allowed_tools"] = ["*"]
    mcp_config["read_only"] = False

    from ohm.mcp.server import call_tool

    # Seed a tiny causal graph
    _req("POST", base_url, "/node", body={"id": "cause_node", "label": "Cause", "node_type": "concept"}, token=customer_token)
    _req("POST", base_url, "/node", body={"id": "effect_node", "label": "Effect", "node_type": "concept"}, token=customer_token)
    _req(
        "POST",
        base_url,
        "/edge",
        body={"from": "cause_node", "to": "effect_node", "type": "CAUSES", "layer": "L3"},
        token=customer_token,
    )
    _req("POST", base_url, "/observe/cause_node", body={"obs_type": "measurement", "value": 0.8}, token=customer_token)
    _req("POST", base_url, "/observe/effect_node", body={"obs_type": "measurement", "value": 0.7}, token=customer_token)
    # Add additional observations so ohm_discover has enough samples
    for _ in range(4):
        _req("POST", base_url, "/observe/cause_node", body={"obs_type": "measurement", "value": 0.75}, token=customer_token)
        _req("POST", base_url, "/observe/effect_node", body={"obs_type": "measurement", "value": 0.72}, token=customer_token)

    for tool, args in [
        ("ohm_inference", {"target": "effect_node", "evidence": "cause_node:1", "format": "json"}),
        ("ohm_intervene", {"target": "effect_node", "state": 1, "format": "json"}),
        ("ohm_voi", {"decision": "effect_node", "format": "json"}),
        ("ohm_refute", {"cause": "cause_node", "effect": "effect_node", "format": "json"}),
        ("ohm_discover", {"nodes": "cause_node,effect_node", "method": "pc", "format": "json"}),
    ]:
        result = await call_tool(tool, args)
        assert not result.isError, f"{tool} failed: {result.content}"
        payload = json.loads(result.content[0].text)
        assert "error" not in payload, f"{tool} returned error payload: {payload}"
