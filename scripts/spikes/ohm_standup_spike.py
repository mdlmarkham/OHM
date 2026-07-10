#!/usr/bin/env python3
"""Spike: `ohm standup` connect-to-existing-daemon path.

This script demonstrates the smallest useful slice of ADR-022:
1. Discover local OHM instances.
2. Probe health and pick one.
3. Authenticate and list tenants.
4. Let the user select a tenant.
5. Emit an MCP config for that tenant.
6. Verify the config via `ohm-mcp --config <path> tools/list`.

Run:
    export OHM_ADMIN_TOKEN=...
    python3 scripts/spikes/ohm_standup_spike.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _req(method: str, url: str, token: str | None = None, data: dict | None = None, timeout: float = 5.0) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} for {method} {url}: {text[:200]}")


def discover_instances() -> list[dict]:
    """Scan well-known locations and probe for OHM instances."""
    candidates = ["http://127.0.0.1:8710"]
    env_url = os.environ.get("OHM_URL")
    if env_url and env_url not in candidates:
        candidates.append(env_url)

    # Also check ~/.ohm/ohmd.json for a configured URL
    ohmd_json = Path.home() / ".ohm" / "ohmd.json"
    if ohmd_json.exists():
        try:
            cfg = json.loads(ohmd_json.read_text())
            url = cfg.get("url") or cfg.get("public_url")
            if url and url not in candidates:
                candidates.append(url)
        except Exception:
            pass

    live: list[dict] = []
    for url in candidates:
        try:
            health = _req("GET", f"{url}/health", timeout=1.5)
            live.append({"url": url, "health": health})
        except Exception as e:
            print(f"  ✗ {url} unreachable ({e})")
    return live


def prompt_choice(options: list[dict], label_key: str = "url") -> dict:
    if not options:
        raise RuntimeError("No options to choose from.")
    if len(options) == 1:
        print(f"Auto-selected: {options[0].get(label_key, options[0])}")
        return options[0]
    for i, opt in enumerate(options, 1):
        print(f"  [{i}] {opt.get(label_key, opt)}")
    while True:
        choice = input("Select: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print("Invalid selection.")


def list_tenants(base_url: str, token: str) -> list[dict]:
    """Try admin tenant list; fall back to asking user for a tenant ID."""
    try:
        resp = _req("GET", f"{base_url}/tenants", token=token, timeout=5.0)
        if isinstance(resp, list):
            return resp
        return resp.get("tenants", []) or []
    except RuntimeError as e:
        if "403" in str(e):
            print("  Admin /tenants is forbidden with this token.")
            print("  Provide a tenant ID + customer API key instead.")
            return []
        raise


def prompt_tenant_id() -> str:
    return os.environ.get("OHM_TENANT_ID", "").strip() or input("Tenant ID: ").strip()


def prompt_customer_key() -> str:
    return os.environ.get("OHM_CUSTOMER_KEY", "").strip() or input("Customer API key for tenant: ").strip()


def get_tenant_schema(base_url: str, token: str, tenant_id: str) -> dict | None:
    try:
        return _req(
            "GET",
            f"{base_url}/tenant/{tenant_id}/schema",
            token=token,
            timeout=5.0,
        )
    except RuntimeError as e:
        if "401" in str(e) or "403" in str(e):
            print(f"  ⚠ Could not fetch schema with provided key ({e}).")
            return None
        raise


def get_or_create_customer_key(base_url: str, admin_token: str, tenant_id: str) -> str:
    """Best effort: provision or rotate a customer-scoped API key."""
    # First, try to provision a fresh customer key.
    try:
        resp = _req(
            "POST",
            f"{base_url}/admin/tenant/{tenant_id}/rotate-key",
            token=admin_token,
            timeout=5.0,
        )
        key = resp.get("customer_api_key") or resp.get("key") or resp.get("token")
        if key:
            return key
    except Exception as e:
        print(f"  rotate-key failed ({e}); falling back to admin token")
    # Fallback: use admin token and rely on X-Tenant-ID (admin-only).
    return admin_token


def write_mcp_config(base_url: str, tenant_id: str, customer_key: str, agent_id: str = "standup-spike") -> Path:
    config = {
        "transport": "stdio",
        "ohm_url": base_url,
        "tenant_id": tenant_id,
        "token": customer_key,
        "agent_id": agent_id,
        "allowed_tools": [
            "ohm_search",
            "ohm_get_node",
            "ohm_neighborhood",
            "ohm_observe",
            "ohm_create_node",
            "ohm_create_edge",
        ],
        "read_only": False,
    }
    out_dir = Path.home() / ".config" / "ohm"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"mcp-{tenant_id}.json"
    out_path.write_text(json.dumps(config, indent=2) + "\n")
    return out_path


def verify_mcp_config(config_path: Path) -> bool:
    """Verify that ohm-mcp can list tools with the generated config."""
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    try:
        result = subprocess.run(
            ["ohm-mcp", "--config", str(config_path)],
            input=payload,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"  ohm-mcp exited {result.returncode}")
            print(f"  stderr: {result.stderr.decode('utf-8', errors='ignore')[:300]}")
            return False
        out = result.stdout.decode("utf-8", errors="ignore")
        parsed = json.loads(out.splitlines()[0])
        tools = parsed.get("result", {}).get("tools", [])
        print(f"  ✓ MCP tools/list returned {len(tools)} tools")
        return True
    except FileNotFoundError:
        print("  ✗ ohm-mcp not found in PATH")
        return False
    except Exception as e:
        print(f"  ✗ MCP verification failed: {e}")
        return False


def main() -> int:
    print("OHM standup spike — connect to existing daemon")

    token = os.environ.get("OHM_ADMIN_TOKEN") or os.environ.get("OHM_TOKEN")
    if not token:
        print("Set OHM_ADMIN_TOKEN or OHM_TOKEN.")
        return 1

    print("\n1. Discovering OHM instances...")
    instances = discover_instances()
    if not instances:
        print("No reachable OHM daemon found. For greenfield mode, ADR-022 proposes `ohm standup --greenfield`.")
        return 2
    instance = prompt_choice(instances, label_key="url")
    base_url = instance["url"]
    print(f"  ✓ Using {base_url}")

    print("\n2. Listing tenants...")
    tenants = list_tenants(base_url, token)
    if not tenants:
        tenant_id = prompt_tenant_id()
        customer_key = prompt_customer_key()
    else:
        # Normalize tenant shape
        tenant_opts = []
        for t in tenants:
            tid = t.get("customer_id") or t.get("id") or t.get("tenant_id")
            if tid:
                tenant_opts.append({"tenant_id": tid, **t})
        tenant = prompt_choice(tenant_opts, label_key="tenant_id")
        tenant_id = tenant["tenant_id"]

        print("\n3. Generating customer-scoped MCP key...")
        customer_key = get_or_create_customer_key(base_url, token, tenant_id)

    print(f"\n4. Fetching schema for tenant {tenant_id}...")
    schema = get_tenant_schema(base_url, customer_key, tenant_id)
    if schema:
        print(f"  ✓ Schema returned: {schema.get('tenant_id', schema.get('id', '?'))}")
    else:
        print("  ⚠ Skipping schema verification (auth). Config will still be written.")

    if customer_key:
        print("  ✓ Token configured (masked for security)")
    else:
        print("  ✗ No token available")

    print("\n5. Writing MCP config...")
    config_path = write_mcp_config(base_url, tenant_id, customer_key)
    print(f"  ✓ Wrote {config_path}")

    print("\n6. Verifying MCP sidecar...")
    if not verify_mcp_config(config_path):
        print("  ⚠ Verification skipped/failed. The MCP config was still written.")

    print("\n✓ Standup connect path completed (config emitted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
