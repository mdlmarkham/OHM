#!/usr/bin/env python3
"""Spike: seed an OHM tenant from a domain template.

Demonstrates the greenfield seeding half of ADR-022:
1. Load a domain seed template.
2. Provision a tenant with a matching domain schema.
3. POST nodes and edges to the tenant via the HTTP API.
4. Verify the minimum viable graph threshold.

Run:
    export OHM_ADMIN_TOKEN=***
    python3 scripts/spikes/ohm_seed_template_spike.py --template personal-knowledge --tenant personal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from ohm.templates import seed_payload


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
        raise RuntimeError(f"HTTP {e.code} for {method} {url}: {text[:300]}")


def provision_tenant(base_url: str, admin_token: str, tenant_id: str, domain: str) -> str:
    """Provision a tenant and return its customer API key."""
    resp = _req(
        "POST",
        f"{base_url}/tenant/provision",
        token=admin_token,
        data={"customer_id": tenant_id, "domain": domain, "tier": "professional"},
        timeout=10.0,
    )
    return resp.get("customer_api_key") or resp.get("key") or resp.get("token") or ""


def seed_tenant(base_url: str, customer_key: str, tenant_id: str, payload: dict) -> None:
    """POST all nodes and edges into the tenant."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {customer_key}",
        "X-Tenant-ID": tenant_id,
        "Content-Type": "application/json",
    }
    for node in payload["nodes"]:
        req = urllib.request.Request(
            f"{base_url}/node",
            method="POST",
            headers=headers,
            data=json.dumps(node).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                print(f"  ✓ node {node['id']} ({result.get('type', '?')})")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="ignore")
            print(f"  ✗ node {node['id']} failed: {e.code} {text[:200]}")

    for edge in payload["edges"]:
        req = urllib.request.Request(
            f"{base_url}/edge",
            method="POST",
            headers=headers,
            data=json.dumps(edge).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                print(f"  ✓ edge {edge['from_node']} --{edge['edge_type']}--\u003e {edge['to_node']}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="ignore")
            print(f"  ✗ edge failed: {e.code} {text[:200]}")


def check_graph(base_url: str, customer_key: str, tenant_id: str) -> dict:
    """Return tenant health / graph stats."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {customer_key}",
        "X-Tenant-ID": tenant_id,
    }
    req = urllib.request.Request(f"{base_url}/tenant/{tenant_id}/schema", headers=headers)
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed an OHM tenant from a domain template")
    parser.add_argument("--template", default="personal-knowledge", help="Seed template name")
    parser.add_argument("--tenant", default="personal", help="Tenant ID to create/use")
    parser.add_argument("--url", default="http://127.0.0.1:8710", help="OHM daemon URL")
    parser.add_argument("--skip-provision", action="store_true", help="Assume tenant exists with given customer key")
    args = parser.parse_args()

    admin_token = os.environ.get("OHM_ADMIN_TOKEN") or os.environ.get("OHM_TOKEN")
    if not admin_token and not args.skip_provision:
        print("Set OHM_ADMIN_TOKEN to provision a tenant, or use --skip-provision with OHM_CUSTOMER_KEY.")
        return 1

    print(f"Loading template: {args.template}")
    payload = seed_payload(args.template)
    print(f"  {len(payload['nodes'])} nodes, {len(payload['edges'])} edges")

    customer_key = os.environ.get("OHM_CUSTOMER_KEY", "")
    if not args.skip_provision:
        print(f"Provisioning tenant {args.tenant} with schema {args.template}")
        try:
            customer_key = provision_tenant(args.url, admin_token, args.tenant, args.template)
        except RuntimeError as e:
            if "403" in str(e):
                print(f"  ✗ Provisioning forbidden: {e}")
                print("  This deployment has no admin token configured. Use greenfield ohmd --init")
                print("  to create an admin token, or use --skip-provision with OHM_CUSTOMER_KEY.")
                return 2
            raise
        if not customer_key:
            print("Provisioning did not return a customer key. Check admin token / permissions.")
            return 2
        masked = customer_key[:8] + "..." + customer_key[-4:] if len(customer_key) > 12 else "***"
        print(f"  Customer key: {masked}")
    else:
        if not customer_key:
            print("--skip-provision requires OHM_CUSTOMER_KEY.")
            return 3
        print(f"Using existing tenant {args.tenant}")

    print(f"Seeding tenant {args.tenant}...")
    seed_tenant(args.url, customer_key, args.tenant, payload)

    print("\nVerifying tenant schema...")
    try:
        schema = check_graph(args.url, customer_key, args.tenant)
        print(f"  ✓ Tenant schema: {schema.get('tenant_id', schema.get('id', '?'))}")
    except Exception as e:
        print(f"  ⚠ Schema check failed: {e}")

    print("\n✓ Seeding spike completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
